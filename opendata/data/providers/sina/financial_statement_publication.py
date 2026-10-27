"""Build source-native Sina FZB publication batches from captured responses."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, NoReturn, TypeAlias
from zoneinfo import ZoneInfo

from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_SINA_FZB_REPORTS,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    MAX_SINA_FZB_TOTAL_ROWS,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_parser import (
    _parse_sina_fzb_response,
    parse_sina_fzb_response,
)
from opendata.pipeline.run_service import (
    MAX_RESULT_BYTES,
    MAX_RESULT_RECORDS,
    NormalizedBatch,
    NormalizedRecord,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

__all__ = [
    "SinaFzbPublicationError",
    "build_sina_fzb_observation_contract",
    "serialize_sina_fzb_response",
]

_PublicationErrorCode: TypeAlias = Literal["invalid_input", "invalid_response", "limit_exceeded"]

_PUBLICATION_ERROR_MESSAGES: dict[_PublicationErrorCode, str] = {
    "invalid_input": "Sina FZB publication input is invalid.",
    "invalid_response": "Sina FZB publication response is invalid.",
    "limit_exceeded": "Sina FZB publication exceeded a downstream limit.",
}

_CAPTURE_ID = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UTC = timezone.utc

# Frozen downstream limits mirrored here so this pure producer does not import
# the SQLAlchemy-backed store module or depend on its private validators.
_MAX_STORE_BUSINESS_KEY_LENGTH = 512
_MAX_STORE_RECORD_BYTES = 256 * 1024
_MAX_STORE_BATCH_BYTES = 64 * 1024 * 1024
_MAX_STORE_METADATA_BYTES = 256 * 1024
_MAX_STORE_NODES = 200_000
_MAX_STORE_DEPTH = 16
_MAX_STORE_CONTAINER_ITEMS = 10_000
_MAX_STORE_KEY_LENGTH = 256
_MAX_STORE_TEXT_LENGTH = 256 * 1024

# RunService exports only its public byte/record constants. These match its
# configured remaining JSON limits and keep this boundary independently pure.
_MAX_RUN_RESULT_DEPTH = 8
_MAX_RUN_RESULT_NODES = 1_000_000
_MAX_RUN_RESULT_TEXT = 4_096
_RUN_SECRET_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "token",
)
_BEARER_VALUE = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_COMMON_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})$"
)

_HISTORY_LIMITATION = (
    "This capture requests page 1 with num=1000; upstream retention and issuer-specific "
    "historical coverage boundaries are unknown."
)


class SinaFzbPublicationError(ValueError):
    """Sanitized failure raised while building a Sina FZB publication batch."""

    def __init__(self, code: _PublicationErrorCode) -> None:
        """Initialize with a stable code and fixed safe message.

        Args:
            code: Stable publication error code.

        Raises:
            ValueError: If the error code is unsupported.
        """
        message = _PUBLICATION_ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina FZB publication error code.")
        self._code = code
        super().__init__(message)

    @property
    def code(self) -> _PublicationErrorCode:
        """Return the stable error code without exposing source input."""
        return self._code


def build_sina_fzb_observation_contract() -> DatasetContract:
    """Return the immutable source-native FZB report observation contract."""
    fields = (
        FieldContract(name="instrument_id", data_type="string"),
        FieldContract(name="capture_id", data_type="string"),
        FieldContract(name="statement_code", data_type="string"),
        FieldContract(name="report_period", data_type="date"),
        FieldContract(name="source_paper_code", data_type="string"),
        FieldContract(name="published_on", data_type="date"),
        FieldContract(name="publication_precision", data_type="string"),
        FieldContract(name="source_update_epoch_seconds", data_type="integer"),
        FieldContract(name="source_updated_at", data_type="datetime"),
        FieldContract(name="source_updated_at_timezone", data_type="string"),
        FieldContract(name="retrieved_at_utc", data_type="datetime"),
        FieldContract(name="currency", data_type="string"),
        FieldContract(name="period_metadata", data_type="object"),
        FieldContract(name="report_metadata", data_type="object"),
        FieldContract(name="entries", data_type="array"),
    )
    return DatasetContract(
        dataset_id="sina_fzb_report_observations",
        schema_version="source-native-report-v1",
        semantic_version="sina-fzb-source-native-v1",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=fields,
        business_key=("instrument_id", "capture_id", "statement_code", "report_period"),
        ordering=("instrument_id", "capture_id", "statement_code", "report_period"),
        filter_fields=("instrument_id", "capture_id"),
        time_field=None,
        temporal_semantics="none",
        source_available_at_field=None,
        source=SourceIdentity(source="sina", source_id="sina-fzb"),
        field_units={"source_update_epoch_seconds": "unix_seconds"},
        field_currencies={},
        currency=None,
        adjustment_basis="not_applicable",
        field_adjustments={},
        calendar_id=None,
        timezone="UTC",
        pit_policy="unsupported",
    )


def serialize_sina_fzb_response(
    body: bytes,
    *,
    query: SinaFzbQuery,
    retrieved_at_utc: datetime,
    capture_id: str,
) -> NormalizedBatch:
    """Parse and serialize one complete, bounded Sina FZB observation capture.

    Args:
        body: Exact response bytes returned by the separately governed fetcher.
        query: Canonical exchange-qualified Sina query used for the response.
        retrieved_at_utc: Explicit retrieval instant with a zero UTC offset.
        capture_id: Lowercase SHA-256-shaped local run/capture identity.

    Returns:
        One source-native report record per parsed report period.

    Raises:
        SinaFzbPublicationError: If input, response identity, or downstream
            bounds are invalid. The exception text is sanitized.
    """
    return _serialize_sina_fzb_response(
        body,
        query=query,
        retrieved_at_utc=retrieved_at_utc,
        capture_id=capture_id,
        checkpoint=None,
    )


def _serialize_sina_fzb_response(
    body: bytes,
    *,
    query: SinaFzbQuery,
    retrieved_at_utc: datetime,
    capture_id: str,
    checkpoint: Callable[[], None] | None,
) -> NormalizedBatch:
    """Build a publication batch while yielding to the owning run lease."""
    normalized_retrieved_at = _validate_inputs(body, query, retrieved_at_utc, capture_id)
    try:
        if checkpoint is None:
            # Keep the established public parser seam used by serializer callers/tests.
            reports = parse_sina_fzb_response(body, query=query)
        else:
            reports = _parse_sina_fzb_response(body, query=query, checkpoint=checkpoint)
    except SinaFzbWireError as exc:
        code: _PublicationErrorCode = (
            "limit_exceeded" if exc.code == "limit_exceeded" else "invalid_response"
        )
        raise SinaFzbPublicationError(code) from exc

    retrieved_text = _utc_iso(normalized_retrieved_at)
    paper_code = _paper_code(query.symbol)
    report_payloads: list[dict[str, JsonValue]] = []
    total_occurrences = 0
    for report in reports:
        if checkpoint is not None:
            checkpoint()
        payload, occurrence_count = _report_payload(
            report,
            symbol=query.symbol,
            paper_code=paper_code,
            capture_id=capture_id,
            retrieved_at_text=retrieved_text,
            checkpoint=checkpoint,
        )
        report_payloads.append(payload)
        total_occurrences += occurrence_count
    if len(report_payloads) > MAX_SINA_FZB_REPORTS or total_occurrences > MAX_SINA_FZB_TOTAL_ROWS:
        _raise_publication("limit_exceeded")

    parsed_snapshot_sha256 = _parsed_snapshot_digest(
        symbol=query.symbol,
        retrieved_at_text=retrieved_text,
        payloads=report_payloads,
        checkpoint=checkpoint,
    )
    records_list: list[NormalizedRecord] = []
    for payload in report_payloads:
        if checkpoint is not None:
            checkpoint()
        records_list.append(
            NormalizedRecord(
                business_key=_business_key(
                    symbol=query.symbol,
                    capture_id=capture_id,
                    report_period=payload["report_period"],
                ),
                payload=payload,
                business_date=None,
                event_at=None,
                observed_at=normalized_retrieved_at,
                source_published_at=None,
                available_at=None,
            )
        )
    records = tuple(records_list)
    quality: dict[str, JsonValue] = {
        "status": "unknown",
        "report_id": None,
        "issue_count": 0,
    }
    provenance: dict[str, JsonValue] = {
        "representation": "derived_from_parsed_response",
        "capture_id": capture_id,
        "parsed_snapshot_sha256": parsed_snapshot_sha256,
        "retrieved_at_utc": retrieved_text,
        "parser_contract": "sina-fzb-wire-v1",
        "coverage_status": "unknown",
        "history_limitation": _HISTORY_LIMITATION,
    }
    batch = NormalizedBatch(
        records=records,
        schema_version="source-native-report-v1",
        semantic_version="sina-fzb-source-native-v1",
        unit="per-field",
        adjustment_basis="not_applicable",
        quality=quality,
        provenance=provenance,
        watermark=None,
    )
    _preflight_batch(batch, checkpoint=checkpoint)
    return batch


def _validate_inputs(
    body: object,
    query: object,
    retrieved_at_utc: object,
    capture_id: object,
) -> datetime:
    """Validate inputs and normalize retrieval time once to builtin UTC."""
    if type(body) is not bytes:
        _raise_publication("invalid_input")
    if type(query) is not SinaFzbQuery or type(query.symbol) is not str:
        _raise_publication("invalid_input")
    if _SYMBOL.fullmatch(query.symbol) is None:
        _raise_publication("invalid_input")
    if type(capture_id) is not str or _CAPTURE_ID.fullmatch(capture_id) is None:
        _raise_publication("invalid_input")
    if type(retrieved_at_utc) is not datetime:
        _raise_publication("invalid_input")
    if retrieved_at_utc.tzinfo is None:
        _raise_publication("invalid_input")
    try:
        offset = retrieved_at_utc.utcoffset()
    except Exception:
        raise SinaFzbPublicationError("invalid_input") from None
    if type(offset) is not timedelta or offset != timedelta(0):
        _raise_publication("invalid_input")
    try:
        normalized = retrieved_at_utc.astimezone(_UTC)
    except Exception:
        raise SinaFzbPublicationError("invalid_input") from None
    if type(normalized) is not datetime or normalized.tzinfo is not _UTC:
        _raise_publication("invalid_input")
    return normalized


def _report_payload(
    report: SinaFzbReport,
    *,
    symbol: str,
    paper_code: str,
    capture_id: str,
    retrieved_at_text: str,
    checkpoint: Callable[[], None] | None = None,
) -> tuple[dict[str, JsonValue], int]:
    """Create one flat report payload while retaining every typed source value."""
    if type(report) is not SinaFzbReport:
        _raise_publication("invalid_response")
    if (
        type(report.symbol) is not str
        or report.symbol != symbol
        or type(report.source_paper_code) is not str
        or report.source_paper_code != paper_code
        or type(report.statement_code) is not str
        or report.statement_code != "fzb"
    ):
        _raise_publication("invalid_response")
    if (
        type(report.report_period) is not date
        or type(report.published_on) is not date
        or type(report.publication_precision) is not str
        or report.publication_precision != "date"
        or type(report.currency) is not str
        or report.currency == ""
    ):
        _raise_publication("invalid_response")
    source_updated_at = _validate_source_update(report)
    period_metadata = _metadata_object(report.period_metadata, checkpoint=checkpoint)
    report_metadata = _metadata_object(report.report_metadata, checkpoint=checkpoint)
    entries, occurrence_count = _entries_payload(report.entries, checkpoint=checkpoint)
    payload: dict[str, JsonValue] = {
        "instrument_id": symbol,
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": report.report_period.isoformat(),
        "source_paper_code": paper_code,
        "published_on": report.published_on.isoformat(),
        "publication_precision": "date",
        "source_update_epoch_seconds": report.source_update_epoch_seconds,
        "source_updated_at": source_updated_at.isoformat(),
        "source_updated_at_timezone": "Asia/Shanghai",
        "retrieved_at_utc": retrieved_at_text,
        "currency": report.currency,
        "period_metadata": period_metadata,
        "report_metadata": report_metadata,
        "entries": entries,
    }
    return payload, occurrence_count


def _validate_source_update(report: SinaFzbReport) -> datetime:
    """Require parser source time to remain the Shanghai rendering of its epoch."""
    epoch = report.source_update_epoch_seconds
    value = report.source_updated_at
    if type(epoch) is not int or epoch <= 0 or type(value) is not datetime:
        _raise_publication("invalid_response")
    if getattr(value.tzinfo, "key", None) != "Asia/Shanghai":
        _raise_publication("invalid_response")
    try:
        offset = value.utcoffset()
        expected = datetime.fromtimestamp(epoch, tz=_SHANGHAI)
    except (OSError, OverflowError, ValueError):
        _raise_publication("invalid_response")
    if offset != timedelta(hours=8) or value != expected or value.fold != expected.fold:
        _raise_publication("invalid_response")
    return value


def _metadata_object(
    value: object, *, checkpoint: Callable[[], None] | None = None
) -> dict[str, JsonValue]:
    """Convert parser metadata pairs to an object only after strict validation."""
    if type(value) is not tuple:
        _raise_publication("invalid_response")
    result: dict[str, JsonValue] = {}
    previous_key: str | None = None
    for pair in value:
        if checkpoint is not None:
            checkpoint()
        if type(pair) is not tuple or len(pair) != 2:
            _raise_publication("invalid_response")
        key, scalar = pair
        if type(key) is not str or key == "" or (previous_key is not None and key <= previous_key):
            _raise_publication("invalid_response")
        result[key] = _json_scalar(scalar)
        previous_key = key
    return result


def _json_scalar(value: object) -> JsonValue:
    """Accept one unchanged strict JSON scalar from a parser metadata tuple."""
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _raise_publication("invalid_response")
        return value
    if type(value) is str:
        return value
    _raise_publication("invalid_response")


def _entries_payload(
    entries_value: object, *, checkpoint: Callable[[], None] | None = None
) -> tuple[list[JsonValue], int]:
    """Serialize parser entries in source order and verify every source ordinal."""
    if type(entries_value) is not tuple:
        _raise_publication("invalid_response")
    if len(entries_value) > MAX_SINA_FZB_ROWS_PER_REPORT:
        _raise_publication("limit_exceeded")
    output: list[JsonValue] = []
    ordinals: list[int] = []
    previous_first_ordinal = -1
    for entry in entries_value:
        if checkpoint is not None:
            checkpoint()
        if type(entry) is SinaFzbItem:
            entry_payload, entry_ordinals = _item_payload(entry, checkpoint=checkpoint)
        elif type(entry) is SinaFzbGroupMarker:
            entry_payload, entry_ordinals = _group_payload(entry, checkpoint=checkpoint)
        else:
            _raise_publication("invalid_response")
        if not entry_ordinals:
            _raise_publication("invalid_response")
        first_ordinal = entry_ordinals[0]
        if type(first_ordinal) is not int or first_ordinal <= previous_first_ordinal:
            _raise_publication("invalid_response")
        previous_first_ordinal = first_ordinal
        ordinals.extend(entry_ordinals)
        output.append(entry_payload)
    if len(ordinals) > MAX_SINA_FZB_ROWS_PER_REPORT:
        _raise_publication("limit_exceeded")
    if sorted(ordinals) != list(range(len(ordinals))):
        _raise_publication("invalid_response")
    return output, len(ordinals)


def _item_payload(
    item: SinaFzbItem, *, checkpoint: Callable[[], None] | None = None
) -> tuple[dict[str, JsonValue], tuple[int, ...]]:
    """Serialize one retained financial item, including duplicate ordinals."""
    if (
        type(item.source_item_id) is not str
        or item.source_item_id == ""
        or type(item.source_title) is not str
        or item.source_title == ""
        or (item.raw_value is not None and type(item.raw_value) is not str)
    ):
        _raise_publication("invalid_response")
    expected_state = (
        "null" if item.raw_value is None else "empty" if item.raw_value == "" else "present"
    )
    if item.value_state != expected_state:
        _raise_publication("invalid_response")
    source_ordinals, ordinal_values = _validated_ordinals(
        item.source_ordinals, checkpoint=checkpoint
    )
    return (
        {
            "kind": "item",
            "source_item_id": item.source_item_id,
            "source_title": item.source_title,
            "source_ordinals": source_ordinals,
            "raw_value": item.raw_value,
            "value_state": item.value_state,
            "source_metadata": _metadata_object(item.source_metadata, checkpoint=checkpoint),
        },
        ordinal_values,
    )


def _group_payload(
    marker: SinaFzbGroupMarker, *, checkpoint: Callable[[], None] | None = None
) -> tuple[dict[str, JsonValue], tuple[int, ...]]:
    """Serialize one recognized structural marker without treating it as data."""
    if (
        marker.raw_item_field != ""
        or marker.raw_item_value != ""
        or type(marker.source_group_no) is not int
        or type(marker.source_title) is not str
    ):
        _raise_publication("invalid_response")
    source_ordinals, ordinal_values = _validated_ordinals(
        marker.source_ordinals, checkpoint=checkpoint
    )
    return (
        {
            "kind": "group_marker",
            "source_ordinals": source_ordinals,
            "source_group_no": marker.source_group_no,
            "source_title": marker.source_title,
            "raw_item_field": marker.raw_item_field,
            "raw_item_value": marker.raw_item_value,
            "source_metadata": _metadata_object(marker.source_metadata, checkpoint=checkpoint),
        },
        ordinal_values,
    )


def _validated_ordinals(
    value: object, *, checkpoint: Callable[[], None] | None = None
) -> tuple[list[JsonValue], tuple[int, ...]]:
    """Copy a nonempty, strictly increasing source ordinal tuple."""
    if type(value) is not tuple or not value:
        _raise_publication("invalid_response")
    output: list[JsonValue] = []
    ordinal_values: list[int] = []
    previous = -1
    for ordinal in value:
        if checkpoint is not None:
            checkpoint()
        if type(ordinal) is not int or ordinal < 0 or ordinal <= previous:
            _raise_publication("invalid_response")
        output.append(ordinal)
        ordinal_values.append(ordinal)
        previous = ordinal
    return output, tuple(ordinal_values)


def _parsed_snapshot_digest(
    *,
    symbol: str,
    retrieved_at_text: str,
    payloads: list[dict[str, JsonValue]],
    checkpoint: Callable[[], None] | None = None,
) -> str:
    """Hash canonical parsed report payloads plus query and retrieval identity."""
    reports: list[JsonValue] = []
    for payload in payloads:
        if checkpoint is not None:
            checkpoint()
        report_payload: dict[str, JsonValue] = {}
        for key, value in payload.items():
            if checkpoint is not None:
                checkpoint()
            if key not in {"capture_id", "retrieved_at_utc"}:
                report_payload[key] = value
        reports.append(report_payload)
    envelope: dict[str, JsonValue] = {
        "digest_version": "sina-fzb-parsed-snapshot-v1",
        "query": {"symbol": symbol},
        "retrieved_at_utc": retrieved_at_text,
        "reports": reports,
    }
    digest = hashlib.sha256(_canonical_json_bytes(envelope, checkpoint=checkpoint)).hexdigest()
    return digest


def _paper_code(symbol: str) -> str:
    """Translate the validated query symbol to Sina's exact paper-code form."""
    code, separator, exchange = symbol.partition(".")
    if separator != "." or exchange not in {"SH", "SZ"}:
        _raise_publication("invalid_input")
    return f"{exchange.lower()}{code}"


def _business_key(*, symbol: str, capture_id: str, report_period: object) -> str:
    """Encode the exact contract business-key fields as compact canonical JSON."""
    if type(report_period) is not str:
        _raise_publication("invalid_response")
    value = [symbol, capture_id, "fzb", report_period]
    return _canonical_json_bytes(value).decode("utf-8", errors="strict")


def _utc_iso(value: datetime) -> str:
    """Render a normalized UTC timestamp with microseconds and a Z suffix."""
    if type(value) is not datetime or value.tzinfo is not _UTC:
        _raise_publication("invalid_response")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json_bytes(value: object, *, checkpoint: Callable[[], None] | None = None) -> bytes:
    """Encode strict canonical JSON without lossy fallback conversion."""
    chunks: list[bytes] = []
    try:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        _raise_publication("invalid_response")
    iterator = encoder.iterencode(value)
    while True:
        try:
            text_chunk = next(iterator)
        except StopIteration:
            break
        except (RecursionError, TypeError, ValueError):
            _raise_publication("invalid_response")
        try:
            chunks.append(text_chunk.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            _raise_publication("invalid_response")
        if checkpoint is not None:
            checkpoint()
    return b"".join(chunks)


def _preflight_batch(
    batch: NormalizedBatch, *, checkpoint: Callable[[], None] | None = None
) -> None:
    """Reject data that any configured run or publication boundary would reject."""
    if len(batch.records) > MAX_RESULT_RECORDS:
        _raise_publication("limit_exceeded")
    _validate_batch_label(batch.schema_version, 64)
    _validate_batch_label(batch.semantic_version, 64)
    _validate_batch_label(batch.unit, 128)
    _validate_batch_label(batch.adjustment_basis, 128)

    run_nodes = [0]
    for _name, value in (("quality", batch.quality), ("provenance", batch.provenance)):
        if checkpoint is not None:
            checkpoint()
        _validate_run_object(value, run_nodes, checkpoint=checkpoint)
        encoded = _store_json_object(value, checkpoint=checkpoint)
        if len(encoded) > _MAX_STORE_METADATA_BYTES:
            _raise_publication("limit_exceeded")

    stored_batch_bytes = 0
    serialized_records: list[JsonValue] = []
    for record in batch.records:
        if checkpoint is not None:
            checkpoint()
        business_key = record.business_key
        _validate_run_text(business_key, _MAX_STORE_BUSINESS_KEY_LENGTH)
        key_bytes = business_key.encode("utf-8", errors="strict")
        payload_bytes = _store_json_object(record.payload, checkpoint=checkpoint)
        record_size = len(payload_bytes) + len(key_bytes) + 256
        if record_size > _MAX_STORE_RECORD_BYTES:
            _raise_publication("limit_exceeded")
        stored_batch_bytes += record_size
        if stored_batch_bytes > _MAX_STORE_BATCH_BYTES:
            _raise_publication("limit_exceeded")
        _validate_run_object(record.payload, run_nodes, checkpoint=checkpoint)
        serialized_records.append(
            {
                "business_key": business_key,
                "payload": dict(record.payload),
                "business_date": None,
                "event_at": None,
                "observed_at": _utc_iso(record.observed_at)
                if record.observed_at is not None
                else None,
                "source_published_at": None,
                "available_at": None,
            }
        )

    result: dict[str, JsonValue] = {
        "schema_version": batch.schema_version,
        "semantic_version": batch.semantic_version,
        "unit": batch.unit,
        "adjustment_basis": batch.adjustment_basis,
        "quality": dict(batch.quality),
        "provenance": dict(batch.provenance),
        "records": serialized_records,
    }
    if len(_canonical_json_bytes(result, checkpoint=checkpoint)) > MAX_RESULT_BYTES:
        _raise_publication("limit_exceeded")


def _validate_batch_label(value: object, maximum: int) -> None:
    """Apply RunService's fixed text and credential checks to batch labels."""
    _validate_run_text(value, maximum)


def _validate_run_object(
    value: object,
    node_count: list[int],
    *,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Mirror RunService's root-object checks and share its whole-batch node budget."""
    if not isinstance(value, dict):
        _raise_publication("invalid_response")
    for key, nested in value.items():
        if checkpoint is not None:
            checkpoint()
        _validate_run_key(key, _MAX_RUN_RESULT_TEXT)
        _validate_run_value(nested, depth=0, node_count=node_count, checkpoint=checkpoint)


def _validate_run_value(
    value: object,
    *,
    depth: int,
    node_count: list[int],
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Apply the source producer's frozen RunService-compatible JSON limits."""
    if checkpoint is not None:
        checkpoint()
    node_count[0] += 1
    if node_count[0] > _MAX_RUN_RESULT_NODES or depth > _MAX_RUN_RESULT_DEPTH:
        _raise_publication("limit_exceeded")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _raise_publication("invalid_response")
        return
    if type(value) is str:
        if len(value) > _MAX_RUN_RESULT_TEXT:
            _raise_publication("limit_exceeded")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
            _raise_publication("invalid_response")
        if _is_secret_value(value):
            _raise_publication("invalid_response")
        return
    if type(value) is list or type(value) is tuple:
        if len(value) > MAX_RESULT_RECORDS:
            _raise_publication("limit_exceeded")
        for child in value:
            _validate_run_value(
                child, depth=depth + 1, node_count=node_count, checkpoint=checkpoint
            )
        return
    if isinstance(value, dict):
        if len(value) > MAX_RESULT_RECORDS:
            _raise_publication("limit_exceeded")
        for key, child in value.items():
            if checkpoint is not None:
                checkpoint()
            _validate_run_key(key, _MAX_RUN_RESULT_TEXT)
            _validate_run_value(
                child, depth=depth + 1, node_count=node_count, checkpoint=checkpoint
            )
        return
    _raise_publication("invalid_response")


def _validate_run_key(value: object, maximum: int) -> None:
    """Validate result object keys, including secret-like field names."""
    if (
        type(value) is not str
        or value == ""
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        _raise_publication("limit_exceeded")
    if _is_secret_key(value):
        _raise_publication("invalid_response")


def _validate_run_text(value: object, maximum: int) -> None:
    """Validate bounded standalone result text without echoing rejected values."""
    if type(value) is not str or not value.strip() or len(value) > maximum:
        _raise_publication("limit_exceeded")
    if any(ord(character) < 32 for character in value) or _is_secret_value(value):
        _raise_publication("invalid_response")


def _store_json_object(value: object, *, checkpoint: Callable[[], None] | None = None) -> bytes:
    """Validate and canonically encode one store JSON object without store imports."""
    if not isinstance(value, dict):
        _raise_publication("invalid_response")
    node_count = [0]
    _validate_store_value(value, depth=0, node_count=node_count, checkpoint=checkpoint)
    encoded = _canonical_json_bytes(value, checkpoint=checkpoint)
    return encoded


def _validate_store_value(
    value: object,
    *,
    depth: int,
    node_count: list[int],
    checkpoint: Callable[[], None] | None = None,
) -> None:
    """Apply the store JSON node, nesting, container, text, and credential bounds."""
    if checkpoint is not None:
        checkpoint()
    node_count[0] += 1
    if node_count[0] > _MAX_STORE_NODES or depth > _MAX_STORE_DEPTH:
        _raise_publication("limit_exceeded")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _raise_publication("invalid_response")
        return
    if type(value) is str:
        if len(value) > _MAX_STORE_TEXT_LENGTH:
            _raise_publication("limit_exceeded")
        if any(ord(character) < 32 for character in value) or _is_secret_value(value):
            _raise_publication("invalid_response")
        return
    if type(value) is list or type(value) is tuple:
        if len(value) > _MAX_STORE_CONTAINER_ITEMS:
            _raise_publication("limit_exceeded")
        for child in value:
            _validate_store_value(
                child, depth=depth + 1, node_count=node_count, checkpoint=checkpoint
            )
        return
    if isinstance(value, dict):
        if len(value) > _MAX_STORE_CONTAINER_ITEMS:
            _raise_publication("limit_exceeded")
        for key, child in value.items():
            if checkpoint is not None:
                checkpoint()
            if type(key) is not str or key == "" or len(key) > _MAX_STORE_KEY_LENGTH:
                _raise_publication("limit_exceeded")
            if _is_secret_key(key):
                _raise_publication("invalid_response")
            _validate_store_value(
                child, depth=depth + 1, node_count=node_count, checkpoint=checkpoint
            )
        return
    _raise_publication("invalid_response")


def _is_secret_key(value: str) -> bool:
    """Match the run and publication-store secret field marker policies."""
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(marker in normalized for marker in _RUN_SECRET_KEY_PARTS)


def _is_secret_value(value: str) -> bool:
    """Reject known bearer and common token-shaped result values."""
    return bool(_BEARER_VALUE.match(value) or _COMMON_SECRET_VALUE.fullmatch(value))


def _raise_publication(code: _PublicationErrorCode) -> NoReturn:
    """Raise one fixed-message publication error."""
    raise SinaFzbPublicationError(code)
