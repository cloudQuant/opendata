"""Publish reviewed instrument reference data without temporal surrogates.

This module projects an injected, validated instrument registry and an explicit
presentation mapping into the existing publication contract. It does not load
the registry, access a database, inspect the environment, or contact a source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import islice
from typing import TYPE_CHECKING

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.pipeline.publication_store import PublishedRecordInput

if TYPE_CHECKING:
    from pydantic import JsonValue

__all__ = [
    "InstrumentPresentation",
    "build_instrument_reference_contract",
    "build_instrument_reference_records",
]

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_OPAQUE_EVIDENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PROFILE_VERSION = "od.instrument-reference.v1"
_MAX_PUBLICATION_ROWS = 20_000
_MAX_ALIASES_PER_INSTRUMENT = 16
_CN_EXCHANGES = frozenset({"XSHG", "XSHE", "XBSE"})

_REFERENCE_FIELDS: tuple[FieldContract, ...] = (
    FieldContract(name="profile_version", data_type="string"),
    FieldContract(name="registry_version", data_type="string"),
    FieldContract(name="registry_evidence_ref", data_type="string"),
    FieldContract(name="instrument_id", data_type="string"),
    FieldContract(name="asset_type", data_type="string"),
    FieldContract(name="symbol", data_type="string"),
    FieldContract(name="name", data_type="string"),
    FieldContract(name="market", data_type="string"),
    FieldContract(name="exchange", data_type="string"),
    FieldContract(name="lifecycle_version", data_type="string"),
    FieldContract(name="status", data_type="string"),
    FieldContract(name="status_as_of", data_type="date"),
    FieldContract(name="status_evidence_ref", data_type="string"),
    FieldContract(name="known_start_date", data_type="date", nullable=True),
    FieldContract(name="start_boundary_unknown", data_type="boolean"),
    FieldContract(name="start_evidence_ref", data_type="string", nullable=True),
    FieldContract(name="delisted_on", data_type="date", nullable=True),
    FieldContract(name="delist_evidence_ref", data_type="string", nullable=True),
    FieldContract(name="aliases", data_type="array"),
)


@dataclass(frozen=True, slots=True)
class InstrumentPresentation:
    """Explicit AFI-facing presentation for one canonical instrument ID.

    The exchange is supplied as reviewed input and is never inferred from the
    six-digit symbol. This profile currently represents China-listed stocks.
    """

    instrument_id: str
    symbol: str
    name: str
    market: str
    exchange: str

    def __post_init__(self) -> None:
        """Reject missing, malformed, or unsupported presentation fields."""
        _require_identifier(self.instrument_id, "instrument_id")
        if not isinstance(self.symbol, str) or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must be exactly six decimal digits")
        if (
            not isinstance(self.name, str)
            or not self.name
            or self.name != self.name.strip()
            or len(self.name) > 128
            or any(not character.isprintable() for character in self.name)
        ):
            raise ValueError("name must be a bounded printable nonempty string")
        if self.market != "CN":
            raise ValueError("market must be the explicit CN market code")
        if self.exchange not in _CN_EXCHANGES:
            raise ValueError("exchange must be explicitly XSHG or XSHE")


def build_instrument_reference_contract(
    *, dataset_id: str, source_id: str, source: str
) -> DatasetContract:
    """Build the frozen atemporal instrument-reference dataset contract.

    Args:
        dataset_id: Explicit dataset identity for this published registry.
        source_id: Explicit provider/source identity for this registry.
        source: Human-readable source identifier from the reviewed profile.

    Returns:
        A table contract with no time field and no point-in-time guarantee.
    """
    return DatasetContract(
        dataset_id=dataset_id,
        schema_version="1",
        semantic_version="1.0.0",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=_REFERENCE_FIELDS,
        business_key=("instrument_id",),
        ordering=("instrument_id",),
        filter_fields=(),
        time_field=None,
        temporal_semantics="none",
        source_available_at_field=None,
        source=SourceIdentity(source_id=source_id, source=source),
        field_units={},
        field_currencies={},
        currency=None,
        adjustment_basis="not_applicable",
        field_adjustments={},
        calendar_id=None,
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def build_instrument_reference_records(
    *,
    registry: InstrumentReferenceRegistry,
    presentations: Iterable[InstrumentPresentation],
    dataset_id: str,
    source_id: str,
) -> tuple[PublishedRecordInput, ...]:
    """Project a complete scoped registry into null-date publication records.

    Every lifecycle needs exactly one explicit presentation. Aliases remain
    attached to their canonical lifecycle, and output order is stable by the
    canonical JSON-array business key expected by the publication store.

    Args:
        registry: Fully constructed and validated instrument registry.
        presentations: Explicit AFI presentation rows, one per registry row.
        dataset_id: Exact dataset scope to publish.
        source_id: Exact provider/source scope to publish.

    Returns:
        Complete replacement records with no fabricated time or availability.
    """
    _require_identifier(dataset_id, "dataset_id")
    _require_identifier(source_id, "source_id")
    if not isinstance(registry, InstrumentReferenceRegistry):
        raise ValueError("registry must be an InstrumentReferenceRegistry")
    if len(registry.lifecycles) > _MAX_PUBLICATION_ROWS:
        raise ValueError(f"registry exceeds the {_MAX_PUBLICATION_ROWS:,}-row profile limit")
    if any(
        row.dataset_id != dataset_id or row.source_id != source_id for row in registry.lifecycles
    ):
        raise ValueError("every lifecycle must match the requested dataset and source scope")
    if any(row.dataset_id != dataset_id or row.source_id != source_id for row in registry.aliases):
        raise ValueError("every alias must match the requested dataset and source scope")

    _validate_evidence_reference(registry.evidence_ref, "registry evidence_ref")
    lifecycle_ids = {row.instrument_id for row in registry.lifecycles}
    aliases_by_instrument: dict[str, list[InstrumentAlias]] = {}
    for alias in registry.aliases:
        _validate_evidence_reference(alias.evidence_ref, "alias evidence_ref")
        if alias.instrument_id not in lifecycle_ids:
            raise ValueError("alias must target a lifecycle in the scoped registry")
        rows = aliases_by_instrument.setdefault(alias.instrument_id, [])
        rows.append(alias)
        if len(rows) > _MAX_ALIASES_PER_INSTRUMENT:
            raise ValueError(
                f"an instrument cannot publish more than {_MAX_ALIASES_PER_INSTRUMENT} aliases"
            )

    presentation_rows = _bounded_presentations(presentations)
    presentation_by_id: dict[str, InstrumentPresentation] = {}
    symbol_to_id: dict[str, str] = {}
    for presentation in presentation_rows:
        if not isinstance(presentation, InstrumentPresentation):
            raise ValueError("presentations must contain only InstrumentPresentation values")
        if presentation.instrument_id in presentation_by_id:
            raise ValueError("presentation instrument_id values must be unique")
        if presentation.symbol in symbol_to_id:
            raise ValueError("presentation symbols must be unique")
        presentation_by_id[presentation.instrument_id] = presentation
        symbol_to_id[presentation.symbol] = presentation.instrument_id
    if set(presentation_by_id) != lifecycle_ids:
        raise ValueError("presentations must cover exactly the scoped registry lifecycles")

    for symbol, instrument_id in symbol_to_id.items():
        if symbol in lifecycle_ids and symbol != instrument_id:
            raise ValueError("presentation symbol is ambiguous with another canonical ID")
    for alias in registry.aliases:
        symbol_target = symbol_to_id.get(alias.alias)
        if symbol_target is not None and symbol_target != alias.instrument_id:
            raise ValueError("alias conflicts with another instrument presentation symbol")

    records: list[PublishedRecordInput] = []
    for lifecycle in registry.lifecycles:
        _validate_lifecycle_evidence(lifecycle)
        if not isinstance(lifecycle.status, LifecycleStatus):
            raise ValueError("lifecycle status must be normalized by InstrumentLifecycle")
        presentation = presentation_by_id[lifecycle.instrument_id]
        alias_payloads: list[JsonValue] = [
            _alias_payload(alias)
            for alias in sorted(
                aliases_by_instrument.get(lifecycle.instrument_id, []),
                key=_alias_sort_key,
            )
        ]
        payload: dict[str, JsonValue] = {
            "profile_version": _PROFILE_VERSION,
            "registry_version": registry.registry_version,
            "registry_evidence_ref": registry.evidence_ref,
            "instrument_id": lifecycle.instrument_id,
            "asset_type": "stock",
            "symbol": presentation.symbol,
            "name": presentation.name,
            "market": presentation.market,
            "exchange": presentation.exchange,
            "lifecycle_version": lifecycle.lifecycle_version,
            "status": lifecycle.status.value,
            "status_as_of": lifecycle.status_as_of.isoformat(),
            "status_evidence_ref": lifecycle.status_evidence_ref,
            "known_start_date": _date_json(lifecycle.known_start_date),
            "start_boundary_unknown": lifecycle.start_boundary_unknown,
            "start_evidence_ref": lifecycle.start_evidence_ref,
            "delisted_on": _date_json(lifecycle.delisted_on),
            "delist_evidence_ref": lifecycle.delist_evidence_ref,
            "aliases": alias_payloads,
        }
        records.append(
            PublishedRecordInput(
                business_key=_business_key(lifecycle.instrument_id),
                payload=payload,
                business_date=None,
                observed_at=None,
                source_published_at=None,
                available_at=None,
                event_at=None,
            )
        )
    records.sort(key=lambda record: record.business_key)
    return tuple(records)


def _require_identifier(value: str, name: str) -> None:
    """Require one stable ASCII identifier without normalizing it."""
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable ASCII identifier")


def _validate_evidence_reference(value: str, name: str) -> None:
    """Require an opaque bounded ASCII evidence ID rather than a URL or prose."""
    if not isinstance(value, str) or _OPAQUE_EVIDENCE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be an opaque ASCII evidence identifier")


def _validate_lifecycle_evidence(lifecycle: InstrumentLifecycle) -> None:
    """Validate every optional and required evidence ID before serialization."""
    _validate_evidence_reference(lifecycle.status_evidence_ref, "status_evidence_ref")
    if lifecycle.start_evidence_ref is not None:
        _validate_evidence_reference(lifecycle.start_evidence_ref, "start_evidence_ref")
    if lifecycle.delist_evidence_ref is not None:
        _validate_evidence_reference(lifecycle.delist_evidence_ref, "delist_evidence_ref")


def _bounded_presentations(
    presentations: Iterable[InstrumentPresentation],
) -> tuple[InstrumentPresentation, ...]:
    """Materialize at most one record beyond the publication profile limit."""
    if isinstance(presentations, (str, bytes, bytearray, Mapping)) or not isinstance(
        presentations, Iterable
    ):
        raise ValueError("presentations must be a bounded iterable")
    try:
        rows = tuple(islice(iter(presentations), _MAX_PUBLICATION_ROWS + 1))
    except TypeError as exc:
        raise ValueError("presentations must be iterable") from exc
    if len(rows) > _MAX_PUBLICATION_ROWS:
        raise ValueError(f"presentations exceed the {_MAX_PUBLICATION_ROWS:,}-row profile limit")
    return rows


def _alias_payload(alias: InstrumentAlias) -> dict[str, JsonValue]:
    """Serialize one alias without losing its inclusive validity boundaries."""
    return {
        "alias": alias.alias,
        "alias_version": alias.alias_version,
        "evidence_ref": alias.evidence_ref,
        "valid_from": _date_json(alias.valid_from),
        "valid_through": _date_json(alias.valid_through),
    }


def _alias_sort_key(alias: InstrumentAlias) -> tuple[str, str, date, date, str]:
    """Return a deterministic ordering for alias evidence rows."""
    return (
        alias.alias,
        alias.alias_version,
        alias.valid_from or date.min,
        alias.valid_through or date.max,
        alias.evidence_ref,
    )


def _business_key(instrument_id: str) -> str:
    """Encode the single ID with the publication store's canonical JSON form."""
    return json.dumps([instrument_id], ensure_ascii=False, separators=(",", ":"))


def _date_json(value: date | None) -> str | None:
    """Encode a date as ISO text while preserving unknown boundaries as null."""
    return value.isoformat() if value is not None else None
