"""Publish run results only when they match an immutable dataset contract.

The writer is an explicitly injected ``PublishHandler`` for ``RunService``. It
validates normalized records against the registered schema and meanings, then
uses the caller's fenced SQLAlchemy connection for publication. It never opens
an engine, performs network I/O, or infers quality from the presence of rows.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol

from opendata.data.capture_selection import (
    INSTRUMENT_CAPTURE_FILTER_FIELDS,
    InstrumentCaptureSelectionError,
)
from opendata.data.capture_selection import (
    selection_profile as instrument_capture_selection_profile,
)
from opendata.data.capture_selection import (
    validate_selector_pair as validate_instrument_capture_pair,
)
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    DatasetContractRegistryError,
    canonical_sha256,
)
from opendata.data.market_symbol_selection import (
    MARKET_SYMBOL_FILTER_FIELDS,
    MarketSymbolSelectionError,
)
from opendata.data.market_symbol_selection import (
    selection_profile as market_symbol_selection_profile,
)
from opendata.data.market_symbol_selection import (
    validate_selector_pair as validate_market_symbol_pair,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract
from opendata.pipeline.publication_store import (
    PublishedRecordInput,
    publish_in_connection,
)
from opendata.pipeline.run_service import NormalizedRecord, PublicationReceipt

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection

    from opendata.pipeline.run_identity import RunRequest, TaskDefinition
    from opendata.pipeline.run_service import NormalizedBatch


class _DatasetContractResolver(Protocol):  # pragma: no cover
    """Narrow contract-registry surface required by the publication writer."""

    def resolve(
        self, dataset_id: str, schema_version: str, semantic_version: str
    ) -> DatasetContract:
        """Resolve the exact registered dataset contract or raise."""
        ...


class ContractPublicationError(ValueError):
    """Raised when run output conflicts with its registered data contract."""


_SelectionProfile = Literal["dataset", "instrument_capture_v1", "market_symbol_v1"]


class ValidatedPublicationWriter:
    """Validate and publish one batch using the supplied active connection."""

    def __init__(self, contract_registry: DatasetContractRegistry) -> None:
        """Bind the explicit immutable registry used to authorize publications."""
        if not isinstance(contract_registry, DatasetContractRegistry):
            raise TypeError("contract_registry must be a DatasetContractRegistry")
        self._contract_registry: _DatasetContractResolver = contract_registry

    def __call__(
        self,
        connection: Connection,
        task: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        idempotency_key: str,
    ) -> PublicationReceipt:
        """Validate source, schema, temporal, quality, and unit semantics, then write.

        ``connection`` is owned by the run ledger's fenced transaction. This
        callback uses it for bounded publication SQL only; provider I/O and
        normalization must already have completed in ``RunService``.
        """
        del request  # run parameters are not a substitute for the registered data contract
        contract = self._resolve_contract(task, batch)
        contract_hash = canonical_sha256(contract)
        self._validate_task_source(task, contract)
        self._validate_batch_semantics(batch, contract)
        quality = _quality_summary(batch.quality)
        if quality["status"] == "rejected":
            raise ContractPublicationError("rejected quality result cannot be published")
        provenance = _provenance_summary(batch.provenance, contract, contract_hash)
        records = self._validate_records(batch, contract)

        publication = publish_in_connection(
            connection,
            dataset_id=contract.dataset_id,
            source_id=contract.source.source_id,
            schema_version=contract.schema_version,
            semantic_version=contract.semantic_version,
            unit="per-field",
            adjustment_basis=contract.adjustment_basis,
            quality=quality,
            provenance=provenance,
            idempotency_key=idempotency_key,
            records=records,
            watermark=batch.watermark,
        )
        return PublicationReceipt(
            publication_id=publication.publication_id,
            dataset_id=publication.dataset_id,
            record_count=publication.record_count,
        )

    def _resolve_contract(self, task: TaskDefinition, batch: NormalizedBatch) -> DatasetContract:
        try:
            contract = self._contract_registry.resolve(
                task.dataset_id,
                batch.schema_version,
                batch.semantic_version,
            )
        except DatasetContractRegistryError as exc:
            raise ContractPublicationError("dataset contract is not registered") from exc
        if not isinstance(contract, DatasetContract):
            raise ContractPublicationError("registry returned an invalid dataset contract")
        if (
            contract.dataset_id != task.dataset_id
            or contract.schema_version != batch.schema_version
            or contract.semantic_version != batch.semantic_version
        ):
            raise ContractPublicationError("resolved contract identity does not match run")
        if _unknown_label(contract.schema_version) or _unknown_label(contract.semantic_version):
            raise ContractPublicationError("contract version identity is unknown")
        if _unknown_label(contract.source.source) or _unknown_label(contract.source.source_id):
            raise ContractPublicationError("contract source identity is unknown")
        return contract

    @staticmethod
    def _validate_task_source(task: TaskDefinition, contract: DatasetContract) -> None:
        if task.provider != contract.source.source:
            raise ContractPublicationError("task provider does not match contract source")

    @staticmethod
    def _validate_batch_semantics(batch: NormalizedBatch, contract: DatasetContract) -> None:
        if batch.schema_version != contract.schema_version:
            raise ContractPublicationError("schema version does not match contract")
        if batch.semantic_version != contract.semantic_version:
            raise ContractPublicationError("semantic version does not match contract")
        if batch.unit != "per-field":
            raise ContractPublicationError("publication unit must use contract per-field units")
        if _unknown_label(contract.adjustment_basis):
            raise ContractPublicationError("contract adjustment basis is unknown")
        if batch.adjustment_basis != contract.adjustment_basis:
            raise ContractPublicationError("adjustment basis does not match contract")

        time_field_name = contract.time_field
        if contract.temporal_semantics != "none" and time_field_name is None:
            raise ContractPublicationError("temporal contract has no time field")
        for field in contract.fields:
            if field.data_type in {"integer", "number"}:
                _require_declared_unit(contract.field_units.get(field.name), field.name)
                adjustment = contract.field_adjustments.get(field.name, contract.adjustment_basis)
                if _unknown_label(adjustment):
                    raise ContractPublicationError(
                        f"numeric field {field.name} has an unknown adjustment basis"
                    )
        if time_field_name is not None:
            field_by_name = {field.name: field for field in contract.fields}
            time_field = field_by_name[time_field_name]
            if contract.temporal_semantics == "business_date" and time_field.data_type != "date":
                raise ContractPublicationError("business-date time field is not a date")
            if (
                contract.temporal_semantics in {"event_time", "observation_time"}
                and time_field.data_type != "datetime"
            ):
                raise ContractPublicationError("temporal time field is not a timestamp")

    def _validate_records(
        self, batch: NormalizedBatch, contract: DatasetContract
    ) -> tuple[PublishedRecordInput, ...]:
        profile = _publication_selection_profile(contract)
        records: list[PublishedRecordInput] = []
        seen_business_keys: set[str] = set()
        for record in batch.records:
            validated = _validate_record_for_profile(record, contract, profile)
            if validated.business_key in seen_business_keys:
                raise ContractPublicationError("batch contains duplicate business keys") from None
            seen_business_keys.add(validated.business_key)
            records.append(validated)
        return tuple(records)


def _validate_record(record: NormalizedRecord, contract: DatasetContract) -> PublishedRecordInput:
    """Module-level adapter retained for the writer's bounded callback path."""
    return _validate_record_for_profile(
        record,
        contract,
        _publication_selection_profile(contract),
    )


def _publication_selection_profile(contract: DatasetContract) -> _SelectionProfile:
    """Resolve both reserved profiles and reject malformed or conflicting contracts."""
    try:
        capture_profile = instrument_capture_selection_profile(contract)
        market_profile = market_symbol_selection_profile(contract)
    except (InstrumentCaptureSelectionError, MarketSymbolSelectionError):
        raise ContractPublicationError("dataset selection profile is invalid") from None

    if capture_profile == "instrument_capture_v1" and market_profile == "market_symbol_v1":
        raise ContractPublicationError("dataset selection profiles conflict")
    if capture_profile not in ("dataset", "instrument_capture_v1"):
        raise ContractPublicationError("dataset selection profile is invalid")
    if market_profile not in ("dataset", "market_symbol_v1"):
        raise ContractPublicationError("dataset selection profile is invalid")
    if market_profile == "market_symbol_v1":
        if contract.filter_fields != MARKET_SYMBOL_FILTER_FIELDS:
            raise ContractPublicationError("dataset selection profiles conflict")
        return "market_symbol_v1"
    if capture_profile == "instrument_capture_v1":
        if contract.filter_fields != INSTRUMENT_CAPTURE_FILTER_FIELDS:
            raise ContractPublicationError("dataset selection profiles conflict")
        return "instrument_capture_v1"
    return "dataset"


def _validate_record_for_profile(
    record: NormalizedRecord,
    contract: DatasetContract,
    profile: _SelectionProfile,
) -> PublishedRecordInput:
    """Validate one normalized row under an already checked contract profile."""
    if not isinstance(record, NormalizedRecord):
        raise ContractPublicationError("normalized record has an invalid type")
    field_by_name = {field.name: field for field in contract.fields}
    if not isinstance(record.payload, Mapping):
        raise ContractPublicationError("record payload must be an object")
    payload = dict(record.payload)
    if set(payload) != set(field_by_name):
        raise ContractPublicationError("record fields do not exactly match contract")
    canonical_payload: dict[str, object] = {}
    for name, field in field_by_name.items():
        canonical_payload[name] = _validate_field_value(name, payload[name], field)

    expected_key = _contract_business_key(contract, canonical_payload)
    if record.business_key != expected_key:
        raise ContractPublicationError("record business key does not match contract fields")
    business_date, event_at, observed_at = _contract_time_values(
        canonical_payload,
        record,
        contract,
    )
    source_published_at = _source_available_at(canonical_payload, record, contract)
    capture_selection = None
    market_symbol_selection = None
    if profile == "instrument_capture_v1":
        try:
            capture_selection = validate_instrument_capture_pair(
                instrument_id=canonical_payload.get("instrument_id"),
                capture_id=canonical_payload.get("capture_id"),
            )
        except InstrumentCaptureSelectionError:
            raise ContractPublicationError("record selector values are invalid") from None
        if capture_selection is None:
            raise ContractPublicationError("record selector values are required")
    elif profile == "market_symbol_v1":
        try:
            market_symbol_selection = validate_market_symbol_pair(
                market=canonical_payload.get("market"),
                symbol=canonical_payload.get("symbol"),
            )
        except MarketSymbolSelectionError:
            raise ContractPublicationError("record selector values are invalid") from None
        if market_symbol_selection is None:
            raise ContractPublicationError("record selector values are required")
    return PublishedRecordInput(
        business_key=expected_key,
        payload=canonical_payload,
        business_date=business_date,
        event_at=event_at,
        observed_at=observed_at,
        source_published_at=source_published_at,
        available_at=record.available_at,
        instrument_id=(capture_selection.instrument_id if capture_selection is not None else None),
        capture_id=capture_selection.capture_id if capture_selection is not None else None,
        market=market_symbol_selection.market if market_symbol_selection is not None else None,
        symbol=market_symbol_selection.symbol if market_symbol_selection is not None else None,
    )


def _validate_field_value(name: str, value: object, field: FieldContract) -> object:
    data_type = field.data_type
    nullable = field.nullable
    if value is None:
        if not nullable:
            raise ContractPublicationError(f"required field {name} is null")
        return None
    if data_type == "string":
        if not isinstance(value, str):
            raise ContractPublicationError(f"field {name} must be a string")
        return value
    if data_type == "integer":
        if type(value) is not int:
            raise ContractPublicationError(f"field {name} must be an integer")
        return value
    if data_type == "number":
        if type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)):
            raise ContractPublicationError(f"field {name} must be a finite number")
        return value
    if data_type == "boolean":
        if type(value) is not bool:
            raise ContractPublicationError(f"field {name} must be a boolean")
        return value
    if data_type == "date":
        if not isinstance(value, str):
            raise ContractPublicationError(f"field {name} must be an ISO date string")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ContractPublicationError(f"field {name} has an invalid ISO date") from exc
        if parsed.isoformat() != value:
            raise ContractPublicationError(f"field {name} must use canonical ISO date form")
        return value
    if data_type == "datetime":
        if not isinstance(value, str):
            raise ContractPublicationError(f"field {name} must be an ISO timestamp string")
        _parse_aware_datetime(value, name)
        return value
    if data_type == "object":
        if not isinstance(value, Mapping):
            raise ContractPublicationError(f"field {name} must be an object")
        return dict(value)
    if data_type == "array":
        if type(value) is not list:
            raise ContractPublicationError(f"field {name} must be an array")
        return list(value)
    raise ContractPublicationError(f"field {name} has an unsupported contract type")


def _contract_business_key(contract: DatasetContract, payload: Mapping[str, object]) -> str:
    """Encode the ordered contract key tuple as a canonical JSON array."""
    values = [payload[name] for name in contract.business_key]
    if any(value is None or value == "" for value in values):
        raise ContractPublicationError("business key fields must be non-empty")
    for index, name in enumerate(contract.business_key):
        field_type = next(field.data_type for field in contract.fields if field.name == name)
        if field_type == "datetime":
            values[index] = (
                _parse_aware_datetime(str(values[index]), name)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _contract_time_values(
    payload: Mapping[str, object],
    record: NormalizedRecord,
    contract: DatasetContract,
) -> tuple[date | None, datetime | None, datetime | None]:
    semantics = contract.temporal_semantics
    if semantics == "business_date":
        if contract.time_field is None:
            raise ContractPublicationError("business-date contract has no time field")
        field_value = payload[contract.time_field]
        expected = date.fromisoformat(str(field_value))
        if record.business_date != expected:
            raise ContractPublicationError("row business date does not match contract time field")
        if record.event_at is not None:
            raise ContractPublicationError("business-date contract cannot infer an event time")
        return expected, None, record.observed_at
    if semantics == "event_time":
        if contract.time_field is None:
            raise ContractPublicationError("event-time contract has no time field")
        expected = _parse_aware_datetime(str(payload[contract.time_field]), contract.time_field)
        if not _same_instant(record.event_at, expected):
            raise ContractPublicationError("row event time does not match contract time field")
        if record.business_date is not None:
            raise ContractPublicationError("event-time contract cannot infer a business date")
        return None, expected, record.observed_at
    if semantics == "observation_time":
        if contract.time_field is None:
            raise ContractPublicationError("observation-time contract has no time field")
        expected = _parse_aware_datetime(str(payload[contract.time_field]), contract.time_field)
        if not _same_instant(record.observed_at, expected):
            raise ContractPublicationError(
                "row observation time does not match contract time field"
            )
        if record.business_date is not None or record.event_at is not None:
            raise ContractPublicationError("snapshot time cannot be relabeled as event history")
        return None, None, expected
    if record.business_date is not None or record.event_at is not None:
        raise ContractPublicationError("non-temporal contract cannot carry an inferred time key")
    return None, None, record.observed_at


def _source_available_at(
    payload: Mapping[str, object], record: NormalizedRecord, contract: DatasetContract
) -> datetime | None:
    field_name = contract.source_available_at_field
    if field_name is None:
        if record.source_published_at is not None:
            raise ContractPublicationError("source publication time has no declared contract field")
        return None
    field_value = payload[field_name]
    if field_value is None:
        if record.source_published_at is not None:
            raise ContractPublicationError(
                "source publication time contradicts null contract field"
            )
        return None
    expected = _parse_aware_datetime(str(field_value), field_name)
    if not _same_instant(record.source_published_at, expected):
        raise ContractPublicationError("source publication time does not match contract field")
    return expected


def _parse_aware_datetime(value: str, field: str) -> datetime:
    try:
        value = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContractPublicationError(f"field {field} has an invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractPublicationError(f"field {field} timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _same_instant(actual: datetime | None, expected: datetime) -> bool:
    return (
        actual is not None
        and actual.tzinfo is not None
        and actual.utcoffset() is not None
        and actual.astimezone(timezone.utc) == expected
    )


def _quality_summary(value: Mapping[str, object]) -> dict[str, object]:
    """Persist a strict quality status without treating records as evidence."""
    if not value:
        return {"status": "unknown", "report_id": None, "issue_count": 0}
    if set(value) != {"status", "report_id", "issue_count"}:
        raise ContractPublicationError("quality must contain status, report_id, and issue_count")
    status = value["status"]
    report_id = value["report_id"]
    issue_count = value["issue_count"]
    if not isinstance(status, str) or status not in {"passed", "warning", "rejected", "unknown"}:
        raise ContractPublicationError("quality status is invalid")
    if report_id is not None and (
        not isinstance(report_id, str)
        or not report_id.strip()
        or len(report_id) > 128
        or any(ord(char) < 32 for char in report_id)
    ):
        raise ContractPublicationError("quality report id is invalid")
    if isinstance(issue_count, bool) or not isinstance(issue_count, int) or issue_count < 0:
        raise ContractPublicationError("quality issue count must be a non-negative integer")
    if status != "unknown" and report_id is None:
        raise ContractPublicationError("verified quality status requires a report id")
    if status == "passed" and issue_count != 0:
        raise ContractPublicationError("passed quality report cannot contain issues")
    if status == "rejected" and issue_count == 0:
        raise ContractPublicationError("rejected quality report must identify an issue")
    return {"status": status, "report_id": report_id, "issue_count": issue_count}


def _provenance_summary(
    value: Mapping[str, object], contract: DatasetContract, contract_hash: str
) -> dict[str, object]:
    """Preserve lineage while reserving source and contract identity fields."""
    provenance = dict(value)
    if "contract_sha256" in provenance:
        raise ContractPublicationError("caller cannot redefine contract_sha256")
    for field, expected in (
        ("source", contract.source.source),
        ("source_id", contract.source.source_id),
    ):
        if field in provenance and provenance[field] != expected:
            raise ContractPublicationError(f"provenance {field} conflicts with contract")
    if "provider" in provenance and provenance["provider"] != contract.source.source:
        raise ContractPublicationError("provenance provider conflicts with contract source")
    provenance["source"] = contract.source.source
    provenance["source_id"] = contract.source.source_id
    provenance["contract_sha256"] = contract_hash
    return provenance


def _require_declared_unit(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or _unknown_label(value):
        raise ContractPublicationError(f"numeric field {field_name} has no verified unit")


def _unknown_label(value: str) -> bool:
    return value.strip().casefold() in {"", "unknown", "unspecified", "n/a", "na", "none"}


__all__ = ["ContractPublicationError", "ValidatedPublicationWriter"]
