"""Publish reversible source-native observations from Sina LRB captures.

The serializer keeps the two source collections separate. A capture contains a
single context record and one record for each ``report_list`` key. The apparent
``report_date.date_value`` to ``report_list`` key relationship is used only as
a strict admission rule for this dataset version; it is not asserted to be a
universal Sina contract. Each collection's source ordinal is retained.

Source values are encoded as a flat node table so ordinary financial report
depth does not consume the run service's shallow JSON depth budget. Decimal
sign, coefficient digits, and exponent are retained exactly. The parser has
already converted JSON ``-0`` to integer ``0``, so its original wire spelling
cannot be recovered. The response body is hashed but is not persisted by this
publication representation; callers that need exact wire evidence must retain
the original bytes separately.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, NoReturn, TypeAlias, TypeGuard

from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.sina.lrb_statement_parser import (
    MAX_SINA_LRB_JSON_DEPTH,
    MAX_SINA_LRB_REPORTS,
    SinaLrbRawArray,
    SinaLrbRawObject,
    SinaLrbRawResponse,
    SinaLrbRawValue,
    SinaLrbWireError,
    parse_sina_lrb_response,
)
from opendata.data.providers.sina.lrb_statement_process import (
    SINA_LRB_PROCESS_OPERATION_ID,
    sina_lrb_request_summary_sha256,
)
from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource
from opendata.pipeline.raw_evidence_reader import RawEvidenceReadResult
from opendata.pipeline.run_service import (
    MAX_RESULT_BYTES,
    MAX_RESULT_DEPTH,
    MAX_RESULT_NODES,
    NormalizedBatch,
    NormalizedRecord,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

__all__ = [
    "SINA_LRB_DATASET_ID",
    "SINA_LRB_RESPONSE_ENCODING",
    "SinaLrbPublicationError",
    "bind_sina_lrb_raw_provenance",
    "build_sina_lrb_observation_contract",
    "reconstruct_sina_lrb_source_document",
    "serialize_sina_lrb_response",
]

SINA_LRB_DATASET_ID = "sina_lrb_report_observations"
SINA_LRB_RESPONSE_ENCODING = "sina-lrb-node-table-v1"

_SCHEMA_VERSION = "source-native-report-v1"
_SEMANTIC_VERSION = "sina-lrb-source-native-v1"
_STATEMENT_CODE = "lrb"
_SOURCE_PAPER_CODE_PREFIX = {"SH": "sh", "SZ": "sz"}
_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z", re.ASCII)
_CAPTURE_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SHA256 = _CAPTURE_ID
_AUTHORIZATION_REFERENCE = re.compile(
    r"^(?:source-auth|raw-retention):[A-Za-z][A-Za-z0-9_.-]{0,63}$",
    re.ASCII,
)
_SECRET_SHAPED_REFERENCE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,})\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bBearer\s+)",
    re.ASCII | re.IGNORECASE,
)
_REPORT_DATE_VALUE = re.compile(r"[0-9]{8}\Z", re.ASCII)
_UTC = timezone.utc
_RAW_RESPONSE_SOURCE = RawResponseSource(
    provider_id="sina",
    dataset_id=SINA_LRB_DATASET_ID,
    operation_id=SINA_LRB_PROCESS_OPERATION_ID,
)

# A single source report is independently addressable by the generic snapshot
# API. Keep each row below the publication-store hard limit and bound the full
# result below RunService's limit. Oversized captures fail atomically; no rows
# or source members are clipped.
_MAX_RECORD_JSON_BYTES = 192 * 1024
_MAX_BATCH_JSON_BYTES = MAX_RESULT_BYTES - 1024 * 1024
_MAX_NODE_TABLE_NODES = 200_000
_MAX_NODE_TABLE_DEPTH = MAX_SINA_LRB_JSON_DEPTH
_MAX_VALUE_TEXT = 4_096
_MAX_DECIMAL_EXPONENT = 1_000_000
_MAX_CAPTURE_RECORDS = MAX_SINA_LRB_REPORTS + 1
_HISTORY_LIMITATION = (
    "This capture requests page 1 with num=1000; upstream retention and issuer-specific "
    "historical coverage boundaries are unknown."
)

_PUBLICATION_ERROR_MESSAGES: dict[str, str] = {
    "invalid_input": "Sina LRB publication input is invalid.",
    "invalid_response": "Sina LRB publication response is invalid.",
    "limit_exceeded": "Sina LRB publication exceeded a downstream limit.",
    "raw_evidence_mismatch": "Sina LRB raw-evidence provenance does not match the batch.",
}


class SinaLrbPublicationError(ValueError):
    """Sanitized failure raised while building or reversing an LRB publication."""

    def __init__(
        self,
        code: Literal[
            "invalid_input", "invalid_response", "limit_exceeded", "raw_evidence_mismatch"
        ],
    ) -> None:
        """Initialize a stable, value-free public error."""
        message = _PUBLICATION_ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina LRB publication error code.")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _FragmentReference:
    """Internal node-table reference to one separately stored collection."""

    name: Literal["report_date", "report_list"]


@dataclass(frozen=True, slots=True)
class _ObjectFragment:
    """Ordered object node that may contain collection references."""

    fields: tuple[tuple[str, _DecodedNode], ...]


@dataclass(frozen=True, slots=True)
class _ArrayFragment:
    """Ordered array node that may contain collection references."""

    items: tuple[_DecodedNode, ...]


_DecodedNode: TypeAlias = SinaLrbRawValue | _FragmentReference | _ObjectFragment | _ArrayFragment


def build_sina_lrb_observation_contract() -> DatasetContract:
    """Return the source-native, atemporal Sina LRB observation contract."""
    fields = (
        FieldContract(name="instrument_id", data_type="string"),
        FieldContract(name="capture_id", data_type="string"),
        FieldContract(name="statement_code", data_type="string"),
        FieldContract(name="source_paper_code", data_type="string"),
        FieldContract(name="record_kind", data_type="string"),
        FieldContract(name="record_key", data_type="string"),
        FieldContract(name="response_encoding", data_type="string"),
        FieldContract(name="retrieved_at_utc", data_type="datetime"),
        FieldContract(name="response_sha256", data_type="string"),
        FieldContract(name="report_list_key", data_type="string", nullable=True),
        FieldContract(name="report_list_ordinal", data_type="integer", nullable=True),
        FieldContract(name="report_date_ordinal", data_type="integer", nullable=True),
        FieldContract(name="response_context", data_type="object", nullable=True),
        FieldContract(name="report_date_entry", data_type="object", nullable=True),
        FieldContract(name="report_list_entry", data_type="object", nullable=True),
    )
    key = ("instrument_id", "capture_id", "statement_code", "record_kind", "record_key")
    return DatasetContract(
        dataset_id=SINA_LRB_DATASET_ID,
        schema_version=_SCHEMA_VERSION,
        semantic_version=_SEMANTIC_VERSION,
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=fields,
        business_key=key,
        ordering=key,
        filter_fields=("instrument_id", "capture_id"),
        time_field=None,
        temporal_semantics="none",
        source_available_at_field=None,
        source=SourceIdentity(source="sina", source_id="sina-lrb"),
        field_units={
            "report_list_ordinal": "source_ordinal",
            "report_date_ordinal": "source_ordinal",
        },
        field_currencies={},
        currency=None,
        adjustment_basis="not_applicable",
        field_adjustments={},
        calendar_id=None,
        timezone="UTC",
        pit_policy="unsupported",
    )


def bind_sina_lrb_raw_provenance(
    batch: NormalizedBatch,
    *,
    evidence: RawEvidenceReadResult,
    instrument_id: str,
    run_key: str,
    source_access_ref: str,
    raw_retention_ref: str,
    retrieved_at_utc: datetime,
) -> NormalizedBatch:
    """Bind verified raw-evidence hashes to an LRB publication batch.

    The caller supplies the exact result from ``RawResponseArtifactReader``.
    This function rechecks its source identity, request summary, HTTP status,
    response size/hash, manifest digest shape, and collection time before
    matching the serializer's independent response digest and every row. This
    binds one immutable manifest address to this body/request/run in publication
    provenance; it does not create a RunLedger recovery journal.

    Args:
        batch: Unmodified result of serializing the exact verified response body.
        evidence: Exact artifact-reader result used to supply that response body.
        instrument_id: Canonical SH/SZ instrument from the archived capture.
        run_key: Fenced RunService key used as the publication capture identity.
        source_access_ref: Non-secret authorization review reference.
        raw_retention_ref: Non-secret raw-retention review reference.
        retrieved_at_utc: UTC instant decoded from the source response.

    Returns:
        A copied batch whose provenance contains the linked raw evidence hashes.

    Raises:
        SinaLrbPublicationError: If any supplied evidence conflicts with the
            serialized capture or is malformed.
    """
    if (
        type(batch) is not NormalizedBatch
        or type(evidence) is not RawEvidenceReadResult
        or type(evidence.manifest) is not RawResponseManifest
        or type(evidence.body) is not bytes
        or evidence.manifest.source != _RAW_RESPONSE_SOURCE
        or evidence.manifest.http_status != 200
        or evidence.manifest.response_size_bytes != len(evidence.body)
        or evidence.manifest.response_sha256 != hashlib.sha256(evidence.body).hexdigest()
        or type(evidence.manifest_sha256) is not str
        or _SHA256.fullmatch(evidence.manifest_sha256) is None
        or type(instrument_id) is not str
        or _SYMBOL.fullmatch(instrument_id) is None
        or type(run_key) is not str
        or _CAPTURE_ID.fullmatch(run_key) is None
        or type(source_access_ref) is not str
        or _AUTHORIZATION_REFERENCE.fullmatch(source_access_ref) is None
        or not source_access_ref.startswith("source-auth:")
        or _SECRET_SHAPED_REFERENCE.search(source_access_ref) is not None
        or type(raw_retention_ref) is not str
        or _AUTHORIZATION_REFERENCE.fullmatch(raw_retention_ref) is None
        or not raw_retention_ref.startswith("raw-retention:")
        or _SECRET_SHAPED_REFERENCE.search(raw_retention_ref) is not None
        or type(retrieved_at_utc) is not datetime
    ):
        _raise("raw_evidence_mismatch")

    try:
        expected_request_summary = sina_lrb_request_summary_sha256(instrument_id)
        normalized_captured_at = _normalize_utc_datetime(evidence.manifest.captured_at_utc)
        normalized_retrieved_at = _normalize_utc_datetime(retrieved_at_utc)
        if normalized_captured_at < normalized_retrieved_at:
            _raise("raw_evidence_mismatch")
        retrieved_time_text = _utc_iso(normalized_retrieved_at)
        capture_time_text = _utc_iso(normalized_captured_at)
    except Exception:
        _raise("raw_evidence_mismatch")
    if evidence.manifest.request_summary_sha256 != expected_request_summary:
        _raise("raw_evidence_mismatch")

    provenance = batch.provenance
    response_sha256 = evidence.manifest.response_sha256
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("raw_response_persisted") is not False
        or provenance.get("capture_id") != run_key
        or provenance.get("response_sha256") != response_sha256
        or provenance.get("retrieved_at_utc") != retrieved_time_text
        or batch.schema_version != _SCHEMA_VERSION
        or batch.semantic_version != _SEMANTIC_VERSION
        or not batch.records
    ):
        _raise("raw_evidence_mismatch")

    for record in batch.records:
        if (
            type(record) is not NormalizedRecord
            or not isinstance(record.payload, Mapping)
            or record.payload.get("instrument_id") != instrument_id
            or record.payload.get("capture_id") != run_key
            or record.payload.get("response_sha256") != response_sha256
            or record.observed_at != normalized_retrieved_at
        ):
            _raise("raw_evidence_mismatch")

    linked_provenance = dict(provenance)
    linked_provenance.update(
        {
            "run_key": run_key,
            "raw_response_persisted": True,
            "raw_manifest_sha256": evidence.manifest_sha256,
            "request_summary_sha256": evidence.manifest.request_summary_sha256,
            "source_access_ref": source_access_ref,
            "raw_retention_ref": raw_retention_ref,
            "raw_captured_at_utc": capture_time_text,
        }
    )
    return replace(batch, provenance=linked_provenance)


def serialize_sina_lrb_response(
    body: bytes,
    *,
    instrument_id: str,
    retrieved_at_utc: datetime,
    capture_id: str,
) -> NormalizedBatch:
    """Convert one bounded source response into a reversible publication batch.

    ``report_date`` and ``report_list`` remain distinct collections. For this
    version, the serializer admits only a unique exact-key join from each
    eight-digit ``date_value`` to a ``report_list`` key. The two original
    ordinals are stored separately so source order is recoverable.

    Args:
        body: Exact UTF-8 JSON response bytes accepted by the bounded parser.
        instrument_id: Canonical six-digit Shanghai or Shenzhen symbol.
        retrieved_at_utc: Explicit aware timestamp at zero UTC offset.
        capture_id: Lowercase 64-hex local capture identity, not a body hash.

    Returns:
        One context row and one row per report-list entry.

    Raises:
        SinaLrbPublicationError: If inputs, source identity, strict collection
            admission, or downstream size bounds are invalid.
    """
    normalized_retrieved_at = _validate_inputs(
        body,
        instrument_id=instrument_id,
        retrieved_at_utc=retrieved_at_utc,
        capture_id=capture_id,
    )
    try:
        response = parse_sina_lrb_response(body)
    except SinaLrbWireError as error:
        if error.code == "limit_exceeded":
            _raise("limit_exceeded")
        _raise("invalid_response")
    response_sha256 = hashlib.sha256(body).hexdigest()
    retrieved_text = _utc_iso(normalized_retrieved_at)
    paper_code = _paper_code(instrument_id)

    date_by_key = _date_entries_by_report_key(response)
    context_table = _encode_node_table(
        response.source_document,
        reference_paths={
            ("result", "data", "report_date"): "report_date",
            ("result", "data", "report_list"): "report_list",
        },
    )
    common: dict[str, object] = {
        "instrument_id": instrument_id,
        "capture_id": capture_id,
        "statement_code": _STATEMENT_CODE,
        "source_paper_code": paper_code,
        "response_encoding": SINA_LRB_RESPONSE_ENCODING,
        "retrieved_at_utc": retrieved_text,
        "response_sha256": response_sha256,
    }

    payloads: list[dict[str, object]] = [
        {
            **common,
            "record_kind": "context",
            "record_key": "context",
            "report_list_key": None,
            "report_list_ordinal": None,
            "report_date_ordinal": None,
            "response_context": context_table,
            "report_date_entry": None,
            "report_list_entry": None,
        }
    ]
    for report in response.reports:
        date_ordinal, date_entry = date_by_key[report.report_list_key]
        payloads.append(
            {
                **common,
                "record_kind": "report",
                "record_key": report.report_list_key,
                "report_list_key": report.report_list_key,
                "report_list_ordinal": report.source_ordinal,
                "report_date_ordinal": date_ordinal,
                "response_context": None,
                "report_date_entry": _encode_node_table(date_entry),
                "report_list_entry": _encode_node_table(report.source_fields),
            }
        )

    if len(payloads) > _MAX_CAPTURE_RECORDS:
        _raise("limit_exceeded")
    records: list[NormalizedRecord] = []
    for payload in payloads:
        _validate_payload_shape(payload)
        if not _is_json_object(payload):
            _raise("invalid_response")
        business_key = _business_key(payload)
        records.append(
            NormalizedRecord(
                business_key=business_key,
                payload=payload,
                business_date=None,
                event_at=None,
                observed_at=normalized_retrieved_at,
                source_published_at=None,
                available_at=None,
            )
        )
    records.sort(key=lambda record: record.business_key)
    batch = NormalizedBatch(
        records=tuple(records),
        schema_version=_SCHEMA_VERSION,
        semantic_version=_SEMANTIC_VERSION,
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={"status": "unknown", "report_id": None, "issue_count": 0},
        provenance={
            "representation": "decoded_source_structure",
            "capture_id": capture_id,
            "response_sha256": response_sha256,
            "retrieved_at_utc": retrieved_text,
            "response_encoding": SINA_LRB_RESPONSE_ENCODING,
            "coverage_status": "unknown",
            "history_limitation": _HISTORY_LIMITATION,
            "raw_response_persisted": False,
        },
        watermark=None,
    )
    _preflight_batch(batch)
    return batch


def reconstruct_sina_lrb_source_document(
    rows: Sequence[Mapping[str, object]],
) -> SinaLrbRawObject:
    """Rebuild the parser's complete decoded document from a capture's rows.

    The accepted input is the complete set of payload objects for a single
    capture. Business-key sorting does not affect reconstruction because both
    original collection ordinals are carried in each report record.

    Args:
        rows: Complete context-plus-report payloads from one LRB capture.

    Returns:
        The ordered immutable source document produced by the LRB parser.

    Raises:
        SinaLrbPublicationError: If records are incomplete, inconsistent, or
            contain an invalid node table.
    """
    if type(rows) not in {list, tuple} or not rows or len(rows) > _MAX_CAPTURE_RECORDS:
        _raise("invalid_input")
    context_payload: Mapping[str, object] | None = None
    report_payloads: list[Mapping[str, object]] = []
    expected_common: tuple[object, ...] | None = None
    seen_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            _raise("invalid_input")
        _validate_payload_shape(row)
        common = _common_identity(row)
        if expected_common is None:
            expected_common = common
        elif common != expected_common:
            _raise("invalid_response")
        record_kind = row["record_kind"]
        record_key = row["record_key"]
        if type(record_kind) is not str or type(record_key) is not str:
            _raise("invalid_response")
        unique = f"{record_kind}\x00{record_key}"
        if unique in seen_keys:
            _raise("invalid_response")
        seen_keys.add(unique)
        if record_kind == "context":
            if context_payload is not None or record_key != "context":
                _raise("invalid_response")
            context_payload = row
        elif record_kind == "report":
            report_payloads.append(row)
        else:
            _raise("invalid_response")
    if context_payload is None or expected_common is None:
        _raise("invalid_response")
    if len(rows) != len(report_payloads) + 1:
        _raise("invalid_response")

    count = len(report_payloads)
    report_by_list_ordinal: dict[int, tuple[str, SinaLrbRawObject]] = {}
    report_by_date_ordinal: dict[int, tuple[str, SinaLrbRawObject]] = {}
    for row in report_payloads:
        key = row["report_list_key"]
        list_ordinal = row["report_list_ordinal"]
        date_ordinal = row["report_date_ordinal"]
        if (
            type(key) is not str
            or row["record_key"] != key
            or type(list_ordinal) is not int
            or type(date_ordinal) is not int
            or not 0 <= list_ordinal < count
            or not 0 <= date_ordinal < count
        ):
            _raise("invalid_response")
        date_entry = _decode_table_object(row["report_date_entry"])
        list_entry = _decode_table_object(row["report_list_entry"])
        if _date_value(date_entry) != key:
            _raise("invalid_response")
        if list_ordinal in report_by_list_ordinal or date_ordinal in report_by_date_ordinal:
            _raise("invalid_response")
        report_by_list_ordinal[list_ordinal] = (key, list_entry)
        report_by_date_ordinal[date_ordinal] = (key, date_entry)
    if set(report_by_list_ordinal) != set(range(count)):
        _raise("invalid_response")
    if set(report_by_date_ordinal) != set(range(count)):
        _raise("invalid_response")
    if {key for key, _entry in report_by_list_ordinal.values()} != {
        key for key, _entry in report_by_date_ordinal.values()
    }:
        _raise("invalid_response")

    report_date = SinaLrbRawArray(
        items=tuple(report_by_date_ordinal[index][1] for index in range(count))
    )
    report_list = SinaLrbRawObject(
        fields=tuple(report_by_list_ordinal[index] for index in range(count))
    )
    context_value = _decode_node_table(context_payload["response_context"], allow_references=True)
    if not isinstance(context_value, _ObjectFragment):
        _raise("invalid_response")
    reconstructed, counts = _replace_fragment_references(
        context_value,
        report_date=report_date,
        report_list=report_list,
    )
    if counts != {"report_date": 1, "report_list": 1}:
        _raise("invalid_response")
    if not isinstance(reconstructed, SinaLrbRawObject):
        _raise("invalid_response")
    return reconstructed


def _validate_inputs(
    body: object,
    *,
    instrument_id: object,
    retrieved_at_utc: object,
    capture_id: object,
) -> datetime:
    """Validate exact public input types and normalize one UTC instant."""
    if type(body) is not bytes:
        _raise("invalid_input")
    if type(instrument_id) is not str or _SYMBOL.fullmatch(instrument_id) is None:
        _raise("invalid_input")
    if type(capture_id) is not str or _CAPTURE_ID.fullmatch(capture_id) is None:
        _raise("invalid_input")
    if type(retrieved_at_utc) is not datetime or retrieved_at_utc.tzinfo is None:
        _raise("invalid_input")
    try:
        offset = retrieved_at_utc.utcoffset()
        normalized = retrieved_at_utc.astimezone(_UTC)
    except Exception:
        _raise("invalid_input")
    if type(offset) is not timedelta or offset != timedelta(0):
        _raise("invalid_input")
    if type(normalized) is not datetime or normalized.tzinfo is not _UTC:
        _raise("invalid_input")
    return normalized


def _date_entries_by_report_key(
    response: SinaLrbRawResponse,
) -> dict[str, tuple[int, SinaLrbRawObject]]:
    """Apply the exact one-to-one date-value/key admission rule."""
    if type(response) is not SinaLrbRawResponse:
        _raise("invalid_response")
    if len(response.report_dates) != len(response.reports):
        _raise("invalid_response")
    if len(response.report_dates) > MAX_SINA_LRB_REPORTS:
        _raise("limit_exceeded")
    dates: dict[str, tuple[int, SinaLrbRawObject]] = {}
    for ordinal, entry in enumerate(response.report_dates):
        key = _date_value(entry)
        if key in dates:
            _raise("invalid_response")
        dates[key] = (ordinal, entry)
    report_keys = [report.report_list_key for report in response.reports]
    if len(set(report_keys)) != len(report_keys) or set(report_keys) != set(dates):
        _raise("invalid_response")
    return dates


def _date_value(value: object) -> str:
    """Read the opaque eight-digit source join key without date coercion."""
    if not isinstance(value, SinaLrbRawObject):
        _raise("invalid_response")
    matches = [item for key, item in value.fields if key == "date_value"]
    if len(matches) != 1 or type(matches[0]) is not str:
        _raise("invalid_response")
    text = matches[0]
    if _REPORT_DATE_VALUE.fullmatch(text) is None:
        _raise("invalid_response")
    return text


def _encode_node_table(
    value: SinaLrbRawValue,
    *,
    reference_paths: Mapping[tuple[str, ...], Literal["report_date", "report_list"]] | None = None,
) -> dict[str, object]:
    """Encode one ordered raw JSON tree as a flat, bounded node table."""
    refs = reference_paths or {}
    nodes: list[dict[str, object]] = []

    def append(raw: SinaLrbRawValue, path: tuple[str, ...]) -> int:
        if len(nodes) >= _MAX_NODE_TABLE_NODES:
            _raise("limit_exceeded")
        node_id = len(nodes)
        nodes.append({})
        if path in refs:
            nodes[node_id] = {"id": node_id, "kind": "reference", "name": refs[path]}
            return node_id
        if isinstance(raw, SinaLrbRawObject):
            fields: list[list[object]] = []
            for key, child in raw.fields:
                if type(key) is not str or len(key) > _MAX_VALUE_TEXT:
                    _raise("limit_exceeded")
                fields.append([key, append(child, (*path, key))])
            nodes[node_id] = {"id": node_id, "kind": "object", "fields": fields}
        elif isinstance(raw, SinaLrbRawArray):
            nodes[node_id] = {
                "id": node_id,
                "kind": "array",
                "items": [append(child, path) for child in raw.items],
            }
        elif raw is None:
            nodes[node_id] = {"id": node_id, "kind": "null"}
        elif type(raw) is bool:
            nodes[node_id] = {"id": node_id, "kind": "boolean", "value": raw}
        elif type(raw) is int:
            if len(str(raw)) > _MAX_VALUE_TEXT:
                _raise("limit_exceeded")
            nodes[node_id] = {"id": node_id, "kind": "integer", "value": raw}
        elif isinstance(raw, Decimal):
            sign, digits, exponent = raw.as_tuple()
            if (
                type(exponent) is not int
                or abs(exponent) > _MAX_DECIMAL_EXPONENT
                or len(digits) > _MAX_VALUE_TEXT
            ):
                _raise("limit_exceeded")
            nodes[node_id] = {
                "id": node_id,
                "kind": "decimal",
                "sign": sign,
                "digits": "".join(str(digit) for digit in digits),
                "exponent": exponent,
            }
        elif type(raw) is str:
            if len(raw) > _MAX_VALUE_TEXT:
                _raise("limit_exceeded")
            nodes[node_id] = {"id": node_id, "kind": "string", "value": raw}
        else:
            _raise("invalid_response")
        return node_id

    root_id = append(value, ())
    return {"encoding": SINA_LRB_RESPONSE_ENCODING, "root_id": root_id, "nodes": nodes}


def _decode_table_object(value: object) -> SinaLrbRawObject:
    """Decode a node table that must have an object root and no references."""
    decoded = _decode_node_table(value, allow_references=False)
    if not isinstance(decoded, SinaLrbRawObject):
        _raise("invalid_response")
    return decoded


def _source_value(value: _DecodedNode) -> SinaLrbRawValue:
    """Narrow a decoded node to the source-value tree when fragments are forbidden."""
    if isinstance(value, (_FragmentReference, _ObjectFragment, _ArrayFragment)):
        _raise("invalid_response")
    return value


def _decode_node_table(value: object, *, allow_references: bool) -> _DecodedNode:
    """Decode a bounded tree, allowing shared scalar leaves but no container DAG."""
    if not isinstance(value, Mapping) or set(value) != {"encoding", "root_id", "nodes"}:
        _raise("invalid_response")
    if value["encoding"] != SINA_LRB_RESPONSE_ENCODING:
        _raise("invalid_response")
    nodes_value = value["nodes"]
    root_id = value["root_id"]
    if (
        type(nodes_value) is not list
        or not nodes_value
        or len(nodes_value) > _MAX_NODE_TABLE_NODES
        or type(root_id) is not int
        or not 0 <= root_id < len(nodes_value)
    ):
        _raise("invalid_response")
    nodes: list[Mapping[str, object]] = []
    for expected_id, node in enumerate(nodes_value):
        if not isinstance(node, Mapping) or type(node.get("id")) is not int:
            _raise("invalid_response")
        if node["id"] != expected_id or type(node.get("kind")) is not str:
            _raise("invalid_response")
        nodes.append(node)
    active: set[int] = set()
    decoded: dict[int, _DecodedNode] = {}
    reference_names: set[str] = set()

    def visit(node_id: int, depth: int = 0) -> _DecodedNode:
        # The source parser admits at most this many nested JSON containers.
        # Count the terminal scalar/reference as a path edge so a wire document
        # with exactly the maximum container depth remains representable while
        # malformed node tables cannot drive Python recursion without bound.
        if depth > _MAX_NODE_TABLE_DEPTH:
            _raise("invalid_response")
        if node_id in decoded:
            # A shared container expands once for every parent edge during
            # reconstruction. Reject it before caching can turn a compact DAG
            # into an unbounded tree; immutable source scalar leaves may share.
            if nodes[node_id]["kind"] in {"array", "object", "reference"}:
                _raise("invalid_response")
            return decoded[node_id]
        if node_id in active:
            _raise("invalid_response")
        active.add(node_id)
        node = nodes[node_id]
        kind = node["kind"]
        if kind == "null" and set(node) == {"id", "kind"}:
            result: _DecodedNode = None
        elif kind in {"boolean", "integer", "string"} and set(node) == {
            "id",
            "kind",
            "value",
        }:
            scalar = node["value"]
            if kind == "boolean" and type(scalar) is bool:
                result = scalar
            elif kind == "integer" and type(scalar) is int:
                if len(str(scalar)) > _MAX_VALUE_TEXT:
                    _raise("invalid_response")
                result = scalar
            elif kind == "string" and type(scalar) is str and len(scalar) <= _MAX_VALUE_TEXT:
                result = scalar
            else:
                _raise("invalid_response")
        elif kind == "decimal" and set(node) == {
            "id",
            "kind",
            "sign",
            "digits",
            "exponent",
        }:
            sign = node["sign"]
            digits_text = node["digits"]
            exponent = node["exponent"]
            if (
                type(sign) is not int
                or sign not in {0, 1}
                or type(digits_text) is not str
                or not digits_text
                or len(digits_text) > _MAX_VALUE_TEXT
                or any(character not in "0123456789" for character in digits_text)
                or type(exponent) is not int
                or abs(exponent) > _MAX_DECIMAL_EXPONENT
            ):
                _raise("invalid_response")
            result = Decimal((sign, tuple(int(character) for character in digits_text), exponent))
        elif kind == "array" and set(node) == {"id", "kind", "items"}:
            items = node["items"]
            if type(items) is not list or len(items) > MAX_SINA_LRB_REPORTS * 512 + 1_000_000:
                _raise("invalid_response")
            decoded_items: list[_DecodedNode] = []
            for child_id in items:
                if type(child_id) is not int or not 0 <= child_id < len(nodes):
                    _raise("invalid_response")
                child = visit(child_id, depth + 1)
                if isinstance(child, _FragmentReference) and not allow_references:
                    _raise("invalid_response")
                decoded_items.append(child)
            result = (
                _ArrayFragment(tuple(decoded_items))
                if allow_references
                else SinaLrbRawArray(tuple(_source_value(child) for child in decoded_items))
            )
        elif kind == "object" and set(node) == {"id", "kind", "fields"}:
            fields = node["fields"]
            if type(fields) is not list or len(fields) > MAX_SINA_LRB_REPORTS * 512 + 1_000_000:
                _raise("invalid_response")
            decoded_fields: list[tuple[str, _DecodedNode]] = []
            seen: set[str] = set()
            for pair in fields:
                if type(pair) is not list or len(pair) != 2:
                    _raise("invalid_response")
                key, child_id = pair
                if (
                    type(key) is not str
                    or len(key) > _MAX_VALUE_TEXT
                    or key in seen
                    or type(child_id) is not int
                    or not 0 <= child_id < len(nodes)
                ):
                    _raise("invalid_response")
                seen.add(key)
                child = visit(child_id, depth + 1)
                if isinstance(child, _FragmentReference) and not allow_references:
                    _raise("invalid_response")
                decoded_fields.append((key, child))
            result = (
                _ObjectFragment(tuple(decoded_fields))
                if allow_references
                else SinaLrbRawObject(
                    tuple((key, _source_value(child)) for key, child in decoded_fields)
                )
            )
        elif kind == "reference" and allow_references and set(node) == {"id", "kind", "name"}:
            name = node["name"]
            if type(name) is not str:
                _raise("invalid_response")
            if name == "report_date":
                reference_name: Literal["report_date", "report_list"] = "report_date"
            elif name == "report_list":
                reference_name = "report_list"
            else:
                _raise("invalid_response")
            if reference_name in reference_names:
                # These placeholders expand to full collection values later.
                # A duplicate would amplify the publication before exact
                # placement validation can reject it.
                _raise("invalid_response")
            reference_names.add(reference_name)
            result = _FragmentReference(reference_name)
        else:
            _raise("invalid_response")
        active.remove(node_id)
        decoded[node_id] = result
        return result

    result = visit(root_id)
    if len(decoded) != len(nodes):
        _raise("invalid_response")
    return result


def _replace_fragment_references(
    value: _DecodedNode,
    *,
    report_date: SinaLrbRawArray,
    report_list: SinaLrbRawObject,
) -> tuple[SinaLrbRawValue, dict[str, int]]:
    """Substitute collection references while counting exact placements."""
    counts = {"report_date": 0, "report_list": 0}

    def replace(node: _DecodedNode) -> SinaLrbRawValue:
        if isinstance(node, _FragmentReference):
            counts[node.name] += 1
            return report_date if node.name == "report_date" else report_list
        if isinstance(node, _ObjectFragment):
            return SinaLrbRawObject(tuple((key, replace(child)) for key, child in node.fields))
        if isinstance(node, _ArrayFragment):
            return SinaLrbRawArray(tuple(replace(child) for child in node.items))
        if isinstance(node, SinaLrbRawObject):
            return SinaLrbRawObject(tuple((key, replace(child)) for key, child in node.fields))
        if isinstance(node, SinaLrbRawArray):
            return SinaLrbRawArray(tuple(replace(child) for child in node.items))
        return node

    return replace(value), counts


def _common_identity(row: Mapping[str, object]) -> tuple[object, ...]:
    """Return the common identity tuple after checking capture-level constants."""
    symbol = row["instrument_id"]
    capture_id = row["capture_id"]
    statement_code = row["statement_code"]
    source_paper_code = row["source_paper_code"]
    retrieved = row["retrieved_at_utc"]
    response_hash = row["response_sha256"]
    if (
        type(symbol) is not str
        or _SYMBOL.fullmatch(symbol) is None
        or type(capture_id) is not str
        or _CAPTURE_ID.fullmatch(capture_id) is None
        or statement_code != _STATEMENT_CODE
        or source_paper_code != _paper_code(symbol)
        or row["response_encoding"] != SINA_LRB_RESPONSE_ENCODING
        or type(retrieved) is not str
        or type(response_hash) is not str
        or re.fullmatch(r"[0-9a-f]{64}", response_hash, re.ASCII) is None
    ):
        _raise("invalid_response")
    return (symbol, capture_id, statement_code, source_paper_code, retrieved, response_hash)


def _validate_payload_shape(payload: Mapping[str, object]) -> None:
    """Require exact field inventory and types from the frozen dataset contract."""
    expected = {
        "instrument_id",
        "capture_id",
        "statement_code",
        "source_paper_code",
        "record_kind",
        "record_key",
        "response_encoding",
        "retrieved_at_utc",
        "response_sha256",
        "report_list_key",
        "report_list_ordinal",
        "report_date_ordinal",
        "response_context",
        "report_date_entry",
        "report_list_entry",
    }
    if set(payload) != expected:
        _raise("invalid_response")
    for key in (
        "instrument_id",
        "capture_id",
        "statement_code",
        "source_paper_code",
        "record_kind",
        "record_key",
        "response_encoding",
        "retrieved_at_utc",
        "response_sha256",
    ):
        if type(payload[key]) is not str:
            _raise("invalid_response")
    if payload["record_kind"] == "context":
        if (
            payload["report_list_key"] is not None
            or payload["report_list_ordinal"] is not None
            or payload["report_date_ordinal"] is not None
            or not isinstance(payload["response_context"], Mapping)
            or payload["report_date_entry"] is not None
            or payload["report_list_entry"] is not None
        ):
            _raise("invalid_response")
    elif payload["record_kind"] == "report":
        if (
            type(payload["report_list_key"]) is not str
            or type(payload["report_list_ordinal"]) is not int
            or type(payload["report_date_ordinal"]) is not int
            or payload["response_context"] is not None
            or not isinstance(payload["report_date_entry"], Mapping)
            or not isinstance(payload["report_list_entry"], Mapping)
        ):
            _raise("invalid_response")
    else:
        _raise("invalid_response")
    _common_identity(payload)


def _business_key(payload: Mapping[str, object]) -> str:
    """Encode the contract key as compact canonical JSON."""
    values = [
        payload["instrument_id"],
        payload["capture_id"],
        payload["statement_code"],
        payload["record_kind"],
        payload["record_key"],
    ]
    try:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError):
        _raise("invalid_response")


def _preflight_batch(batch: NormalizedBatch) -> None:
    """Apply run-service shape and encoded-size bounds before returning data."""
    count = _count_json_nodes(batch.quality, depth=0) + _count_json_nodes(batch.provenance, depth=0)
    for record in batch.records:
        payload = record.payload
        size = _json_size(payload)
        if size > _MAX_RECORD_JSON_BYTES:
            _raise("limit_exceeded")
        count += _count_json_nodes(payload, depth=0)
        _validate_json_depth(payload, depth=0)
        _validate_run_service_text(payload)
        if count > MAX_RESULT_NODES:
            _raise("limit_exceeded")
    summary = {
        "schema_version": batch.schema_version,
        "semantic_version": batch.semantic_version,
        "unit": batch.unit,
        "adjustment_basis": batch.adjustment_basis,
        "quality": batch.quality,
        "provenance": batch.provenance,
        "records": [
            {
                "business_key": record.business_key,
                "payload": record.payload,
                "business_date": None,
                "event_at": None,
                "observed_at": _utc_iso(record.observed_at),
                "source_published_at": None,
                "available_at": None,
            }
            for record in batch.records
        ],
    }
    if _json_size(summary) > _MAX_BATCH_JSON_BYTES:
        _raise("limit_exceeded")


def _validate_run_service_text(value: object) -> None:
    """Apply the run-service string length and control-character boundary."""
    if isinstance(value, str):
        if len(value) > _MAX_VALUE_TEXT or any(
            ord(character) < 32 and character not in "\t\n\r" for character in value
        ):
            _raise("limit_exceeded")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                not isinstance(key, str)
                or len(key) > _MAX_VALUE_TEXT
                or any(ord(character) < 32 for character in key)
            ):
                _raise("limit_exceeded")
            _validate_run_service_text(nested)
    elif _is_plain_json_sequence(value):
        for nested in value:
            _validate_run_service_text(nested)


def _count_json_nodes(value: object, *, depth: int) -> int:
    """Count JSON tree nodes with early rejection at configured bounds."""
    if depth > MAX_RESULT_DEPTH:
        _raise("limit_exceeded")
    if isinstance(value, Mapping):
        return 1 + sum(_count_json_nodes(item, depth=depth + 1) for item in value.values())
    if _is_plain_json_sequence(value):
        count = 1
        for item in value:
            count += _count_json_nodes(item, depth=depth + 1)
        return count
    if value is None or type(value) in {bool, int, float, str}:
        return 1
    _raise("invalid_response")


def _validate_json_depth(value: object, *, depth: int) -> None:
    """Validate run-service JSON nesting independently of node counting."""
    if depth > MAX_RESULT_DEPTH:
        _raise("limit_exceeded")
    if isinstance(value, Mapping):
        for nested in value.values():
            _validate_json_depth(nested, depth=depth + 1)
    elif _is_plain_json_sequence(value):
        for nested in value:
            _validate_json_depth(nested, depth=depth + 1)


def _is_plain_json_sequence(value: object) -> TypeGuard[Sequence[object]]:
    """Narrow exact built-in list/tuple values without admitting subclasses."""
    return type(value) is list or type(value) is tuple


def _is_json_object(value: object) -> TypeGuard[dict[str, JsonValue]]:
    """Validate and narrow one exact JSON object without converting its values."""
    if not isinstance(value, dict) or type(value) is not dict:
        return False
    return all(type(key) is str and _is_json_value(nested) for key, nested in value.items())


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    """Recursively validate exact JSON scalar, object, and array values."""
    if value is None:
        return True
    if isinstance(value, (bool, int, float, str)):
        return type(value) in {bool, int, float, str}
    if isinstance(value, list):
        return type(value) is list and all(_is_json_value(nested) for nested in value)
    if isinstance(value, dict):
        return type(value) is dict and all(
            type(key) is str and _is_json_value(nested) for key, nested in value.items()
        )
    return False


def _json_size(value: object) -> int:
    """Return strict canonical UTF-8 JSON size, rejecting non-JSON values."""
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        _raise("invalid_response")


def _paper_code(symbol: str) -> str:
    """Return Sina's exchange-prefixed paper code for one canonical symbol."""
    if type(symbol) is not str or _SYMBOL.fullmatch(symbol) is None:
        _raise("invalid_input")
    code, exchange = symbol.split(".")
    return f"{_SOURCE_PAPER_CODE_PREFIX[exchange]}{code}"


def _normalize_utc_datetime(value: object) -> datetime:
    """Normalize an exact aware zero-offset datetime for provenance binding."""
    if type(value) is not datetime or value.tzinfo is None:
        _raise("raw_evidence_mismatch")
    try:
        offset = value.utcoffset()
        normalized = value.astimezone(_UTC)
    except Exception:
        _raise("raw_evidence_mismatch")
    if type(offset) is not timedelta or offset != timedelta(0):
        _raise("raw_evidence_mismatch")
    if type(normalized) is not datetime or normalized.tzinfo is not _UTC:
        _raise("raw_evidence_mismatch")
    return normalized


def _utc_iso(value: datetime | None) -> str:
    """Render an explicit UTC timestamp in the stable API representation."""
    if type(value) is not datetime or value.tzinfo is not _UTC:
        _raise("invalid_response")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _raise(
    code: Literal["invalid_input", "invalid_response", "limit_exceeded", "raw_evidence_mismatch"],
) -> NoReturn:
    """Raise one fixed publication error without attaching source details."""
    raise SinaLrbPublicationError(code) from None
