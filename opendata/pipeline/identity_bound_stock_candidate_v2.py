"""Prepare a raw identity-bound stock candidate from bounded local artifacts.

This module composes local evidence, the fixed SH/SZ Eastmoney-native parser,
caller-supplied pin and snapshot bytes, and the in-memory v2 raw candidate.
The preparation function issues no network or database operation and writes
or publishes nothing.

The returned digests prove only that the supplied hashes match the local bytes
read here. They do not authenticate who supplied those hashes, the upstream
source, source authorization, licensing rights, or history completeness. The
legacy request-summary digest does not include the exchange route or actual
HTTP query bytes; the parser independently checks response symbol and market,
but the captured request itself remains caller-declared and unverified.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from opendata.data.models.identity_bound_stock_v2 import (
    IdentityBoundStockDailyV2RawCandidate,
    IdentityBoundStockDailyV2RawRow,
    IdentityBoundStockDailyV2Window,
    IdentityBoundStockV2RawProvenance,
)
from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    MAX_RESPONSE_BYTES,
    OPERATION_ID,
    UNDERLYING_PRODUCT_ID,
    StockDailyNativeRequest,
    parse_eastmoney_stock_daily_response,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.temporal_identity_pins import (
    TemporalIdentityPin,
    TemporalIdentityPinDocumentError,
    decode_temporal_identity_pin_registry,
)
from opendata.data.temporal_instrument_identity import (
    Exchange,
    IdentityResolution,
    IdentityResolutionStatus,
    MarketSymbolPair,
    ResolutionSubject,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    TemporalIdentitySnapshotError,
    decode_temporal_identity_snapshot,
)
from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_REQUEST_FIELDS = ("adjust", "end_date", "market", "source", "start_date", "symbol")
_ENDPOINT_ID = "eastmoney.push2his.stock.kline.get"


class IdentityBoundStockV2PreparationError(ValueError):
    """Expose fixed, content-free error codes for offline candidate preparation."""

    def __init__(self, code: str) -> None:
        """Create a stable error without retaining paths, bytes, or SQL details."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedIdentityBoundStockDailyV2Candidate:
    """In-memory candidate plus the exact local digests used to assemble it.

    ``verified_*_sha256`` means only that each caller-supplied lowercase digest
    matched the exact local bytes consumed by this function. It is not a
    signature, issuer authentication, permission, or completeness assertion.
    """

    candidate: IdentityBoundStockDailyV2RawCandidate
    verified_capture_manifest_sha256: str
    verified_pin_document_sha256: str
    verified_identity_snapshot_sha256: str
    request_binding_status: Literal["caller_declared_summary_hash_only"] = (
        "caller_declared_summary_hash_only"
    )
    source_rights_status: Literal["unknown"] = "unknown"


def prepare_identity_bound_stock_candidate_v2(
    *,
    reader: RawResponseArtifactReader,
    manifest_sha256: str,
    dataset_id: str,
    request: StockDailyNativeRequest,
    captured_at_not_after: datetime,
    pin_document_bytes: bytes,
    pin_document_sha256: str,
    identity_snapshot_bytes: bytes,
) -> PreparedIdentityBoundStockDailyV2Candidate:
    """Assemble one source-native v2 candidate from hash-checked local inputs.

    Only an explicit AkShare SH/SZ request is accepted. The native parser binds
    the response's symbol and market to that request, while the content-addressed
    reader verifies the local manifest and response body against their supplied
    digests. The pin document digest is checked independently; the snapshot is
    decoded and its complete-byte SHA-256 must equal the pin. Each returned row
    is resolved over its own one-day half-open interval against that exact
    registry. Truly unknown dates remain unassigned, and no dates are synthesized.

    Request-summary hashes in the existing capture contract omit the exchange
    route and wire query bytes. ``request_binding_status`` therefore remains
    ``caller_declared_summary_hash_only`` even though response symbol/market are
    checked. Source adapter version is not present in the capture manifest and
    is recorded as the literal ``unknown`` rather than inferred from code.

    Args:
        reader: Exact reader bound to a private local artifact root.
        manifest_sha256: Caller-supplied lowercase digest of the capture manifest.
        dataset_id: Exact dataset key expected in the manifest and pin document.
        request: Explicit immutable AkShare SH/SZ daily request with no adjustment.
        captured_at_not_after: Caller-supplied aware UTC upper bound for capture time.
        pin_document_bytes: Complete operator pin document bytes already available locally.
        pin_document_sha256: Independently supplied digest of the complete pin document.
        identity_snapshot_bytes: Complete local canonical snapshot named by the pin.

    Returns:
        An in-memory source-native raw candidate and the exact digests matched locally.

    Raises:
        IdentityBoundStockV2PreparationError: If any capture, request, pin,
            snapshot, or identity binding is invalid. Messages contain only a code.
        TypeError: If a concrete reader or request model of the supported type is absent.
    """
    checked_request = _validate_request(request)
    if type(reader) is not RawResponseArtifactReader:
        raise TypeError("reader must be the exact RawResponseArtifactReader")
    _validate_inputs(
        manifest_sha256,
        dataset_id,
        captured_at_not_after,
        pin_document_bytes,
        pin_document_sha256,
        identity_snapshot_bytes,
    )
    try:
        exclusive_end = checked_request.end_date + timedelta(days=1)
    except OverflowError:
        raise IdentityBoundStockV2PreparationError("request_window_invalid") from None

    expected_source = RawResponseSource(
        provider_id=ADAPTER_ID,
        dataset_id=dataset_id,
        operation_id=OPERATION_ID,
    )
    expected_request_sha256 = _request_digest(checked_request)
    try:
        read_result = reader.read(
            manifest_sha256,
            expected_source=expected_source,
            expected_request_summary_sha256=expected_request_sha256,
            captured_at_not_after=captured_at_not_after,
            expected_http_status=200,
        )
    except RawEvidenceReadError:
        raise IdentityBoundStockV2PreparationError("capture_rejected") from None
    except Exception:
        raise IdentityBoundStockV2PreparationError("capture_reader_failed") from None

    manifest, body = _validate_capture_result(
        read_result,
        manifest_sha256=manifest_sha256,
        expected_source=expected_source,
        expected_request_sha256=expected_request_sha256,
        captured_at_not_after=captured_at_not_after,
    )
    try:
        envelope = parse_eastmoney_stock_daily_response(body, request=checked_request)
    except (TypeError, ValueError, ValidationError):
        raise IdentityBoundStockV2PreparationError("native_response_invalid") from None
    if (
        envelope.response_sha256 != manifest.response_sha256
        or envelope.response_size_bytes != manifest.response_size_bytes
    ):
        raise IdentityBoundStockV2PreparationError("native_response_binding_mismatch")
    if not envelope.rows:
        raise IdentityBoundStockV2PreparationError("native_response_empty")
    if envelope.duplicate_trade_dates:
        raise IdentityBoundStockV2PreparationError("native_response_duplicate_dates")

    pin, registry = _decode_pinned_registry(
        pin_document_bytes=pin_document_bytes,
        pin_document_sha256=pin_document_sha256,
        identity_snapshot_bytes=identity_snapshot_bytes,
        dataset_id=dataset_id,
        source_id=manifest.source.provider_id,
    )
    pair = MarketSymbolPair(
        Exchange.XSHG if checked_request.exchange == "SH" else Exchange.XSHE,
        checked_request.symbol,
    )
    try:
        rows = tuple(
            IdentityBoundStockDailyV2RawRow(
                trade_date=row.trade_date,
                source_row_index=row.row_index,
                raw_tokens=row.raw_tokens,
                assignment_claim_status=(
                    "caller_declared_unverified" if assignment is not None else "unknown"
                ),
                assignment=assignment,
            )
            for row in envelope.rows
            for assignment in (_assignment_for_row(registry, pair, row.trade_date, pin.source_id),)
        )
    except IdentityBoundStockV2PreparationError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise IdentityBoundStockV2PreparationError("candidate_rows_rejected") from None
    try:
        provenance = IdentityBoundStockV2RawProvenance(
            dataset_id=manifest.source.dataset_id,
            adapter_id=manifest.source.provider_id,
            product_id=UNDERLYING_PRODUCT_ID,
            endpoint_id=_ENDPOINT_ID,
            adapter_version="unknown",
            capture_id=manifest.capture_id,
            request_sha256=manifest.request_summary_sha256,
            response_sha256=manifest.response_sha256,
            response_size_bytes=manifest.response_size_bytes,
            captured_at_utc=manifest.captured_at_utc,
        )
        candidate = IdentityBoundStockDailyV2RawCandidate(
            request_pair=pair,
            window=IdentityBoundStockDailyV2Window(
                start=checked_request.start_date,
                end_exclusive=exclusive_end,
            ),
            identity_pin=pin,
            provenance=provenance,
            rows=rows,
        )
    except (TypeError, ValueError, ValidationError):
        raise IdentityBoundStockV2PreparationError("candidate_contract_rejected") from None

    return PreparedIdentityBoundStockDailyV2Candidate(
        candidate=candidate,
        verified_capture_manifest_sha256=manifest_sha256,
        verified_pin_document_sha256=pin_document_sha256,
        verified_identity_snapshot_sha256=pin.snapshot_sha256,
    )


def _validate_request(request: StockDailyNativeRequest) -> StockDailyNativeRequest:
    """Revalidate the exact immutable request and reject provider auto-routing."""
    if type(request) is not StockDailyNativeRequest:
        raise TypeError("request must be the exact StockDailyNativeRequest")
    try:
        checked = StockDailyNativeRequest.model_validate(
            request.model_dump(mode="python", warnings="error"), strict=True
        )
    except (AttributeError, PydanticSerializationError, TypeError, ValidationError):
        raise IdentityBoundStockV2PreparationError("request_invalid") from None
    if checked.request_source != ADAPTER_ID:
        raise IdentityBoundStockV2PreparationError("request_source_unsupported")
    return checked


def _validate_inputs(
    manifest_sha256: str,
    dataset_id: str,
    captured_at_not_after: datetime,
    pin_document_bytes: bytes,
    pin_document_sha256: str,
    identity_snapshot_bytes: bytes,
) -> None:
    """Reject malformed selectors and non-UTC bounds before reading artifacts."""
    if type(manifest_sha256) is not str or _SHA256.fullmatch(manifest_sha256) is None:
        raise IdentityBoundStockV2PreparationError("manifest_digest_invalid")
    if type(dataset_id) is not str or _IDENTIFIER.fullmatch(dataset_id) is None:
        raise IdentityBoundStockV2PreparationError("dataset_id_invalid")
    if type(captured_at_not_after) is not datetime:
        raise IdentityBoundStockV2PreparationError("capture_bound_invalid")
    try:
        offset = captured_at_not_after.utcoffset()
    except Exception:
        raise IdentityBoundStockV2PreparationError("capture_bound_invalid") from None
    if captured_at_not_after.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise IdentityBoundStockV2PreparationError("capture_bound_invalid")
    if type(pin_document_bytes) is not bytes:
        raise IdentityBoundStockV2PreparationError("pin_document_invalid")
    if type(pin_document_sha256) is not str or _SHA256.fullmatch(pin_document_sha256) is None:
        raise IdentityBoundStockV2PreparationError("pin_digest_invalid")
    if type(identity_snapshot_bytes) is not bytes:
        raise IdentityBoundStockV2PreparationError("identity_snapshot_invalid")


def _request_digest(request: StockDailyNativeRequest) -> str:
    """Recompute the legacy allowlisted summary, which does not bind wire bytes."""
    return request_summary_sha256(
        {
            "adjust": "",
            "end_date": request.end_date,
            "market": "cn",
            "source": request.request_source,
            "start_date": request.start_date,
            "symbol": request.symbol,
        },
        allowed_fields=_REQUEST_FIELDS,
    )


def _validate_capture_result(
    result: RawEvidenceReadResult,
    *,
    manifest_sha256: str,
    expected_source: RawResponseSource,
    expected_request_sha256: str,
    captured_at_not_after: datetime,
) -> tuple[RawResponseManifest, bytes]:
    """Recheck the reader DTO and canonical manifest/body hashes before parsing."""
    if (
        type(result) is not RawEvidenceReadResult
        or type(result.manifest) is not RawResponseManifest
        or type(result.body) is not bytes
    ):
        raise IdentityBoundStockV2PreparationError("capture_result_invalid")
    try:
        manifest = RawResponseManifest.model_validate(
            result.manifest.model_dump(mode="python", warnings="error"), strict=True
        )
    except (AttributeError, PydanticSerializationError, TypeError, ValidationError):
        raise IdentityBoundStockV2PreparationError("capture_manifest_invalid") from None
    try:
        canonical_manifest = json.dumps(
            manifest.model_dump(mode="json", warnings="error"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except PydanticSerializationError:
        raise IdentityBoundStockV2PreparationError("capture_manifest_invalid") from None
    if (
        type(result.manifest_sha256) is not str
        or not hmac.compare_digest(result.manifest_sha256, manifest_sha256)
        or not hmac.compare_digest(hashlib.sha256(canonical_manifest).hexdigest(), manifest_sha256)
        or manifest.source != expected_source
        or manifest.request_summary_sha256 != expected_request_sha256
        or manifest.http_status != 200
        or manifest.captured_at_utc > captured_at_not_after
        or len(result.body) != manifest.response_size_bytes
        or manifest.response_size_bytes > MAX_RESPONSE_BYTES
        or not hmac.compare_digest(
            hashlib.sha256(result.body).hexdigest(), manifest.response_sha256
        )
    ):
        raise IdentityBoundStockV2PreparationError("capture_binding_mismatch")
    return manifest, result.body


def _decode_pinned_registry(
    *,
    pin_document_bytes: bytes,
    pin_document_sha256: str,
    identity_snapshot_bytes: bytes,
    dataset_id: str,
    source_id: str,
) -> tuple[TemporalIdentityPin, TemporalIdentityRegistry]:
    """Decode exact pin and snapshot bytes and compare their independent digests."""
    try:
        pin_registry = decode_temporal_identity_pin_registry(
            pin_document_bytes, pin_document_sha256
        )
        raw_pin = pin_registry.get(dataset_id)
    except TemporalIdentityPinDocumentError:
        raise IdentityBoundStockV2PreparationError("pin_document_rejected") from None
    except Exception:
        raise IdentityBoundStockV2PreparationError("pin_document_invalid") from None
    if type(raw_pin) is not TemporalIdentityPin:
        raise IdentityBoundStockV2PreparationError("identity_pin_unavailable")
    try:
        pin = TemporalIdentityPin.model_validate(
            raw_pin.model_dump(mode="python", warnings="error"), strict=True
        )
    except (AttributeError, PydanticSerializationError, TypeError, ValidationError):
        raise IdentityBoundStockV2PreparationError("identity_pin_invalid") from None
    if pin.dataset_id != dataset_id or pin.source_id != source_id or pin.source_id != ADAPTER_ID:
        raise IdentityBoundStockV2PreparationError("identity_pin_mismatch")

    try:
        registry = decode_temporal_identity_snapshot(identity_snapshot_bytes)
    except TemporalIdentitySnapshotError:
        raise IdentityBoundStockV2PreparationError("identity_snapshot_rejected") from None
    except Exception:
        raise IdentityBoundStockV2PreparationError("identity_snapshot_invalid") from None
    snapshot_sha256 = hashlib.sha256(identity_snapshot_bytes).hexdigest()
    if (
        type(registry) is not TemporalIdentityRegistry
        or registry.reference_version != pin.reference_version
        or not hmac.compare_digest(snapshot_sha256, pin.snapshot_sha256)
    ):
        raise IdentityBoundStockV2PreparationError("identity_snapshot_pin_mismatch")
    if any(assignment.source_id != pin.source_id for assignment in registry.assignments):
        raise IdentityBoundStockV2PreparationError("identity_snapshot_source_mismatch")
    return pin, registry


def _assignment_for_row(
    registry: TemporalIdentityRegistry,
    pair: MarketSymbolPair,
    trade_date: date,
    pin_source_id: str,
) -> TemporalIdentityAssignment | None:
    """Re-resolve one date and return the exact original assignment or unknown."""
    try:
        next_date = trade_date + timedelta(days=1)
        resolution = registry.resolve_pair(pair, trade_date, next_date)
    except (OverflowError, TemporalInstrumentIdentityError, TypeError, ValueError):
        raise IdentityBoundStockV2PreparationError("identity_resolution_rejected") from None
    if (
        type(resolution) is not IdentityResolution
        or resolution.subject is not ResolutionSubject.PAIR
        or resolution.pair != pair
        or resolution.window_start != trade_date
        or resolution.window_end != next_date
        or len(resolution.segments) != 1
    ):
        raise IdentityBoundStockV2PreparationError("identity_resolution_invalid")
    segment = resolution.segments[0]
    if segment.valid_from != trade_date or segment.valid_to != next_date:
        raise IdentityBoundStockV2PreparationError("identity_resolution_invalid")
    if segment.status is IdentityResolutionStatus.UNKNOWN:
        return None
    if segment.status is not IdentityResolutionStatus.KNOWN or segment.assignment_id is None:
        raise IdentityBoundStockV2PreparationError("identity_resolution_invalid")
    assignments = tuple(
        assignment
        for assignment in registry.assignments
        if assignment.assignment_id == segment.assignment_id
    )
    if len(assignments) != 1:
        raise IdentityBoundStockV2PreparationError("identity_assignment_missing")
    assignment = assignments[0]
    if (
        type(assignment) is not TemporalIdentityAssignment
        or assignment.pair != pair
        or segment.pair != pair
        or assignment.source_id != pin_source_id
        or segment.source_id != pin_source_id
        or assignment.assignment_id != segment.assignment_id
        or assignment.instrument_id != segment.instrument_id
        or assignment.source_version != segment.source_version
        or assignment.evidence_ref != segment.evidence_ref
        or assignment.source_sha256 != segment.source_sha256
        or not assignment.valid_from <= trade_date
        or (assignment.valid_to is not None and trade_date >= assignment.valid_to)
    ):
        raise IdentityBoundStockV2PreparationError("identity_assignment_mismatch")
    return assignment


__all__ = [
    "IdentityBoundStockV2PreparationError",
    "PreparedIdentityBoundStockDailyV2Candidate",
    "prepare_identity_bound_stock_candidate_v2",
]
