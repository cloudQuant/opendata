"""Synthetic offline checks for raw-capture to identity-v2 preparation."""

from __future__ import annotations

import hashlib
import json
import traceback
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, cast

import pytest

import opendata.pipeline.identity_bound_stock_candidate_v2 as candidate_v2
from opendata.data.models.identity_bound_stock_v2 import (
    IdentityBoundStockDailyV2RawCandidate,
)
from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    OPERATION_ID,
    StockDailyNativeEnvelope,
    StockDailyNativeRequest,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.temporal_identity_pins import (
    TemporalIdentityPin,
    TemporalIdentityPinRegistry,
)
from opendata.data.temporal_instrument_identity import (
    Exchange,
    IdentityResolution,
    IdentityResolutionSegment,
    IdentityResolutionStatus,
    MarketSymbolPair,
    ResolutionSubject,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    encode_temporal_identity_snapshot,
)
from opendata.pipeline.identity_bound_stock_candidate_v2 import (
    IdentityBoundStockV2PreparationError,
    PreparedIdentityBoundStockDailyV2Candidate,
    prepare_identity_bound_stock_candidate_v2,
)
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)

_DATASET_ID = "iteration204.stock_daily"
_CAPTURE_BOUND = datetime(2099, 1, 1, tzinfo=timezone.utc)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class _SyntheticCapture:
    """One fully synthetic local capture and separately hashed identity files."""

    reader: RawResponseArtifactReader
    manifest_sha256: str
    pin_document_bytes: bytes
    pin_document_sha256: str
    identity_snapshot_bytes: bytes
    request: StockDailyNativeRequest
    body: bytes
    response_path: Path


class _ExplodingTimezone(tzinfo):
    """Raise a recognizable error when datetime offset validation is attempted."""

    def __init__(self) -> None:
        self.sentinel = RuntimeError("TZ_OFFSET_SENTINEL")

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise self.sentinel

    def dst(self, dt: datetime | None) -> timedelta | None:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str | None:
        return "synthetic"


class _NoOffsetTimezone(tzinfo):
    """Represent a malformed synthetic timezone with no UTC offset."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


class _SerializationSentinel:
    """Expose a marker only if a malformed model is rendered or logged."""

    def __repr__(self) -> str:
        return "SERIALIZATION_SENTINEL"


class _SyntheticResolver:
    """Supply one resolver result or exception to the row-boundary unit checks."""

    def __init__(
        self,
        result: object,
        *,
        assignments: tuple[TemporalIdentityAssignment, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.assignments = assignments
        self.error = error

    def resolve_pair(
        self,
        pair: MarketSymbolPair,
        start: date,
        end: date,
    ) -> object:
        if self.error is not None:
            raise self.error
        return self.result


def _request(
    start: str,
    end: str,
    *,
    symbol: str = "600519.SH",
    request_source: str = "akshare",
) -> StockDailyNativeRequest:
    """Build one explicit synthetic SH or SZ request for native-parser tests."""
    return StockDailyNativeRequest.model_validate(
        {
            "symbol": symbol,
            "exchange": "SZ" if symbol.upper().endswith(".SZ") else "SH",
            "start_date": start,
            "end_date": end,
            "request_source": request_source,
        }
    )


def _assignment(
    assignment_id: str,
    instrument_id: str,
    start: str,
    end: str | None,
    *,
    source_id: str = ADAPTER_ID,
    exchange: Exchange = Exchange.XSHG,
    symbol: str = "600519",
) -> TemporalIdentityAssignment:
    """Build one synthetic, nonoverlapping Shanghai assignment interval."""
    return TemporalIdentityAssignment(
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        exchange=exchange,
        symbol=symbol,
        valid_from=date.fromisoformat(start),
        valid_to=date.fromisoformat(end) if end is not None else None,
        source_id=source_id,
        source_version="fixture-identity-v1",
        evidence_ref=f"TEST-IDENTITY-{assignment_id}",
    )


def _request_digest(request: StockDailyNativeRequest) -> str:
    """Reproduce the existing adapter's bounded legacy summary contract."""
    return request_summary_sha256(
        {
            "adjust": "",
            "end_date": request.end_date,
            "market": "cn",
            "source": request.request_source,
            "start_date": request.start_date,
            "symbol": request.symbol,
        },
        allowed_fields=("adjust", "end_date", "market", "source", "start_date", "symbol"),
    )


def _wire_body(
    request: StockDailyNativeRequest,
    rows: tuple[str, ...],
    *,
    market: int | None = None,
    response_symbol: str | None = None,
) -> bytes:
    """Create a synthetic response with exact native token strings."""
    return json.dumps(
        {
            "rc": 0,
            "data": {
                "code": request.symbol if response_symbol is None else response_symbol,
                "market": request.market_id if market is None else market,
                "klines": list(rows),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _fixture(
    tmp_path: Path,
    *,
    assignments: tuple[TemporalIdentityAssignment, ...],
    rows: tuple[str, ...],
    start: str = "2024-01-02",
    end: str = "2024-01-03",
    request_source: str = "akshare",
    response_market: int | None = None,
    response_symbol: str | None = None,
    source_dataset_id: str = _DATASET_ID,
    http_status: int = 200,
    symbol: str = "600519.SH",
) -> _SyntheticCapture:
    """Write only temporary synthetic raw, pin, and snapshot bytes."""
    request = _request(start, end, symbol=symbol, request_source=request_source)
    body = _wire_body(
        request,
        rows,
        market=response_market,
        response_symbol=response_symbol,
    )
    registry = TemporalIdentityRegistry(
        reference_version="fixture-identity-v1",
        evidence_ref="TEST-SNAPSHOT-IDENTITY-V2",
        assignments=assignments,
    )
    snapshot_bytes = encode_temporal_identity_snapshot(registry)
    snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    pin = TemporalIdentityPin(
        dataset_id=_DATASET_ID,
        source_id=ADAPTER_ID,
        authority_id="fixture-authority",
        registry_id="fixture-registry",
        reference_version=registry.reference_version,
        snapshot_sha256=snapshot_sha256,
    )
    pin_document_bytes = json.dumps(
        {"schema_version": 1, "pins": [pin.model_dump(mode="json")]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    pin_document_sha256 = hashlib.sha256(pin_document_bytes).hexdigest()

    artifact_root = tmp_path / "raw-capture"
    writer = RawResponseArtifactWriter(artifact_root, max_response_bytes=4096)
    capture = writer.capture(
        (body,),
        source=RawResponseSource(
            provider_id=ADAPTER_ID,
            dataset_id=source_dataset_id,
            operation_id=OPERATION_ID,
        ),
        request_summary_sha256=_request_digest(request),
        http_status=http_status,
        retention_class="unknown",
        source_history_limit="unknown",
    )
    return _SyntheticCapture(
        reader=RawResponseArtifactReader(artifact_root, max_response_bytes=4096),
        manifest_sha256=capture.manifest_path.stem,
        pin_document_bytes=pin_document_bytes,
        pin_document_sha256=pin_document_sha256,
        identity_snapshot_bytes=snapshot_bytes,
        request=request,
        body=body,
        response_path=capture.artifact_path,
    )


def _prepare(
    capture: _SyntheticCapture,
) -> PreparedIdentityBoundStockDailyV2Candidate:
    """Run candidate composition with only local synthetic inputs."""
    return _prepare_with_overrides(capture)


def _prepare_with_overrides(
    capture: _SyntheticCapture,
    **overrides: object,
) -> PreparedIdentityBoundStockDailyV2Candidate:
    """Run composition after one explicitly selected synthetic input mutation."""
    arguments: dict[str, object] = {
        "reader": capture.reader,
        "manifest_sha256": capture.manifest_sha256,
        "dataset_id": _DATASET_ID,
        "request": capture.request,
        "captured_at_not_after": _CAPTURE_BOUND,
        "pin_document_bytes": capture.pin_document_bytes,
        "pin_document_sha256": capture.pin_document_sha256,
        "identity_snapshot_bytes": capture.identity_snapshot_bytes,
    }
    arguments.update(overrides)
    return prepare_identity_bound_stock_candidate_v2(**cast("Any", arguments))


def _read_result(capture: _SyntheticCapture) -> RawEvidenceReadResult:
    """Read the fixture through the real local evidence reader."""
    return capture.reader.read(
        capture.manifest_sha256,
        expected_source=_expected_source(),
        expected_request_summary_sha256=_request_digest(capture.request),
        captured_at_not_after=_CAPTURE_BOUND,
        expected_http_status=200,
    )


def _expected_source() -> RawResponseSource:
    """Build the exact source identity expected by the synthetic candidate."""
    return RawResponseSource(
        provider_id=ADAPTER_ID,
        dataset_id=_DATASET_ID,
        operation_id=OPERATION_ID,
    )


def _pin_document_with_updates(
    capture: _SyntheticCapture,
    **updates: str,
) -> tuple[bytes, str]:
    """Return a canonical synthetic pin document with a fresh local digest."""
    document = json.loads(capture.pin_document_bytes)
    document["pins"][0].update(updates)
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def _known_resolution(
    pair: MarketSymbolPair,
    trade_date: date,
    *,
    assignment_id: str,
    instrument_id: str,
    source_id: str = ADAPTER_ID,
    source_version: str = "fixture-identity-v1",
    evidence_ref: str = "TEST-RESOLVER-IDENTITY-V2",
    source_sha256: str | None = None,
) -> IdentityResolution:
    """Build one valid one-day known resolution for consistency-boundary tests."""
    next_date = trade_date + timedelta(days=1)
    segment = IdentityResolutionSegment(
        valid_from=trade_date,
        valid_to=next_date,
        status=IdentityResolutionStatus.KNOWN,
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        pair=pair,
        source_id=source_id,
        source_version=source_version,
        evidence_ref=evidence_ref,
        source_sha256=source_sha256,
    )
    return IdentityResolution(
        subject=ResolutionSubject.PAIR,
        pair=pair,
        instrument_id=None,
        reference_version="fixture-identity-v1",
        reference_evidence_ref="TEST-RESOLVER-SNAPSHOT-V2",
        window_start=trade_date,
        window_end=next_date,
        segments=(segment,),
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    (
        ("manifest_sha256", "not-a-digest", "manifest_digest_invalid"),
        ("dataset_id", "../outside", "dataset_id_invalid"),
        ("captured_at_not_after", None, "capture_bound_invalid"),
        ("captured_at_not_after", datetime(2099, 1, 1), "capture_bound_invalid"),
        (
            "captured_at_not_after",
            datetime(2099, 1, 1, tzinfo=timezone(timedelta(hours=8))),
            "capture_bound_invalid",
        ),
        (
            "captured_at_not_after",
            datetime(2099, 1, 1, tzinfo=_NoOffsetTimezone()),
            "capture_bound_invalid",
        ),
        ("pin_document_bytes", "not bytes", "pin_document_invalid"),
        ("pin_document_sha256", "F" * 64, "pin_digest_invalid"),
        ("identity_snapshot_bytes", bytearray(b"snapshot"), "identity_snapshot_invalid"),
    ),
)
def test_malformed_preparation_inputs_fail_before_reading_artifacts(
    tmp_path: Path,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    """Reject malformed selectors, types, and non-UTC bounds at the public boundary."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare_with_overrides(capture, **{field: value})

    assert captured.value.code == expected_code


def test_wrong_reader_type_is_rejected_before_capture_access(tmp_path: Path) -> None:
    """The public entry point accepts only the concrete artifact reader."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    with pytest.raises(TypeError, match="exact RawResponseArtifactReader"):
        _prepare_with_overrides(capture, reader=object())


def test_wrong_request_type_and_tampered_request_are_rejected(tmp_path: Path) -> None:
    """Reject absent request models and revalidate a corrupted frozen request."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    with pytest.raises(TypeError, match="exact StockDailyNativeRequest"):
        _prepare_with_overrides(capture, request=object())

    corrupted_request = capture.request.model_copy(update={"symbol": "invalid-sentinel"})
    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare_with_overrides(capture, request=corrupted_request)

    assert captured.value.code == "request_invalid"


def test_request_serialization_warning_is_raised_and_sanitized(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed request fields never emit Pydantic's input-value warning."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    corrupted_request = capture.request.model_copy(update={"symbol": _SerializationSentinel()})

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
            _prepare_with_overrides(capture, request=corrupted_request)
        output = capsys.readouterr()

    formatted_traceback = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert captured.value.code == "request_invalid"
    assert caught_warnings == []
    assert "SERIALIZATION_SENTINEL" not in output.out + output.err + caplog.text
    assert "SERIALIZATION_SENTINEL" not in str(captured.value)
    assert "SERIALIZATION_SENTINEL" not in formatted_traceback
    assert captured.value.__suppress_context__ is True
    assert captured.value.__context__ is not None
    assert "SERIALIZATION_SENTINEL" in str(captured.value.__context__)


def test_maximum_business_date_cannot_overflow_the_half_open_window(tmp_path: Path) -> None:
    """Inclusive date.max cannot be represented as a finite exclusive end."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("9999-12-31,1,2,3,0,5,6,7,8,9,10",),
        start="9999-12-31",
        end="9999-12-31",
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "request_window_invalid"


def test_candidate_resolves_reused_pair_without_rewriting_assignment_intervals(
    tmp_path: Path,
) -> None:
    """Resolve each trade date to the actual adjacent assignment and keep raw tokens."""
    old = _assignment("listing-before-reuse", "instrument-before", "2024-01-01", "2024-01-03")
    new = _assignment("listing-after-reuse", "instrument-after", "2024-01-03", None)
    capture = _fixture(
        tmp_path,
        assignments=(old, new),
        rows=(
            "2024-01-02,010.00,11.25,12.50,9.75,0007,90.10,,+1.2,-0.5,3.4",
            "2024-01-03,011.00,12.25,13.50,10.75,0008,91.10,2.3,-0.7,+0.4,3.5",
        ),
    )

    prepared = _prepare(capture)
    candidate = prepared.candidate

    assert type(candidate) is IdentityBoundStockDailyV2RawCandidate
    assert candidate.request_pair == MarketSymbolPair(Exchange.XSHG, "600519")
    assert candidate.window.start == date(2024, 1, 2)
    assert candidate.window.end_exclusive == date(2024, 1, 4)
    assert tuple(row.assignment.assignment_id for row in candidate.rows if row.assignment) == (
        old.assignment_id,
        new.assignment_id,
    )
    assert candidate.rows[0].assignment is not None
    assert candidate.rows[0].assignment.valid_from == old.valid_from
    assert candidate.rows[0].assignment.valid_to == old.valid_to
    assert candidate.rows[0].raw_tokens == (
        "2024-01-02",
        "010.00",
        "11.25",
        "12.50",
        "9.75",
        "0007",
        "90.10",
        "",
        "+1.2",
        "-0.5",
        "3.4",
    )
    assert candidate.provenance.adapter_version == "unknown"
    assert candidate.provenance.response_sha256 == hashlib.sha256(capture.body).hexdigest()
    assert candidate.provenance.response_size_bytes == len(capture.body)
    assert prepared.verified_capture_manifest_sha256 == capture.manifest_sha256
    assert prepared.verified_pin_document_sha256 == capture.pin_document_sha256
    assert prepared.verified_identity_snapshot_sha256 == candidate.identity_pin.snapshot_sha256
    assert prepared.request_binding_status == "caller_declared_summary_hash_only"
    assert prepared.source_rights_status == "unknown"
    assert candidate.provenance.source_identity_status == "unverified"
    assert candidate.provenance.source_authorization_status == "unknown"
    assert candidate.provenance.adjustment_semantics == "unknown"
    assert candidate.provenance.price_unit_semantics == "unknown"
    assert candidate.provenance.volume_unit_semantics == "unknown"
    assert candidate.provenance.amount_unit_semantics == "unknown"
    assert candidate.provenance.history_coverage_status == "unknown"


def test_unknown_registry_gap_keeps_the_observed_raw_row_without_synthesizing_dates(
    tmp_path: Path,
) -> None:
    """Only a real registry gap yields an unknown identity; no missing date is added."""
    later = _assignment("listed-later", "instrument-later", "2024-01-03", None)
    capture = _fixture(
        tmp_path,
        assignments=(later,),
        rows=(
            "2024-01-02,1,2,3,0,5,6,7,8,9,10",
            "2024-01-03,11,12,13,10,15,16,17,18,19,20",
        ),
    )

    candidate = _prepare(capture).candidate

    assert len(candidate.rows) == 2
    assert candidate.rows[0].trade_date == date(2024, 1, 2)
    assert candidate.rows[0].assignment_claim_status == "unknown"
    assert candidate.rows[0].assignment is None
    assert candidate.rows[0].raw_tokens[1] == "1"
    assert candidate.rows[1].assignment_claim_status == "caller_declared_unverified"
    assert candidate.rows[1].assignment is not None
    assert candidate.rows[1].assignment.assignment_id == later.assignment_id


def test_empty_pinned_registry_keeps_all_returned_rows_unknown(tmp_path: Path) -> None:
    """An empty, correctly pinned registry supplies no lifecycle evidence to infer."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    candidate = _prepare(capture).candidate

    assert len(candidate.rows) == 1
    assert candidate.rows[0].assignment is None
    assert candidate.rows[0].assignment_claim_status == "unknown"


def test_empty_native_response_is_rejected_without_inventing_rows(tmp_path: Path) -> None:
    """A valid empty provider response is not interpreted as a no-trading day."""
    capture = _fixture(tmp_path, assignments=(), rows=())

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "native_response_empty"


def test_duplicate_native_business_dates_reject_the_whole_capture(tmp_path: Path) -> None:
    """Duplicate observations remain an error instead of silently selecting a row."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=(
            "2024-01-02,1,2,3,0,5,6,7,8,9,10",
            "2024-01-02,11,12,13,10,15,16,17,18,19,20",
        ),
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "native_response_duplicate_dates"


def test_exchange_is_part_of_the_temporal_identity_pair(tmp_path: Path) -> None:
    """The same six-digit code on another exchange remains unknown in this snapshot."""
    shanghai_assignment = _assignment(
        "shanghai-only",
        "instrument-shanghai",
        "2024-01-01",
        None,
        exchange=Exchange.XSHG,
        symbol="000001",
    )
    capture = _fixture(
        tmp_path,
        assignments=(shanghai_assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
        symbol="000001.SZ",
    )

    candidate = _prepare(capture).candidate

    assert candidate.request_pair == MarketSymbolPair(Exchange.XSHE, "000001")
    assert candidate.rows[0].assignment is None
    assert candidate.rows[0].assignment_claim_status == "unknown"


def test_capture_after_the_callers_utc_bound_is_rejected(tmp_path: Path) -> None:
    """The raw reader rejects evidence later than the caller's explicit UTC bound."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare_with_overrides(
            capture,
            captured_at_not_after=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )

    assert captured.value.code == "capture_rejected"


@pytest.mark.parametrize("http_status", (201, 404))
def test_non_success_capture_status_is_rejected_by_the_evidence_reader(
    tmp_path: Path,
    http_status: int,
) -> None:
    """A body with a non-success recorded status cannot become a stock candidate."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
        http_status=http_status,
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "capture_rejected"


def test_capture_from_a_different_dataset_is_not_reused(tmp_path: Path) -> None:
    """An otherwise valid artifact remains bound to its recorded dataset key."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
        source_dataset_id="iteration204.other_dataset",
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "capture_rejected"


def test_foreign_source_assignment_anywhere_in_pinned_snapshot_fails_closed(
    tmp_path: Path,
) -> None:
    """The snapshot source homogeneity contract rejects even unrelated foreign rows."""
    foreign = _assignment(
        "foreign-source", "foreign-instrument", "2020-01-01", None, source_id="other-source"
    )
    capture = _fixture(
        tmp_path,
        assignments=(foreign,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "identity_snapshot_source_mismatch"


def test_capture_body_tampering_is_rejected_before_identity_candidate_is_returned(
    tmp_path: Path,
) -> None:
    """The content-addressed reader rejects a changed local response blob."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    capture.response_path.write_bytes(b"tampered synthetic response")

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "capture_rejected"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (RawEvidenceReadError("CAPTURE_ERROR_SENTINEL"), "capture_rejected"),
        (RuntimeError("CAPTURE_ERROR_SENTINEL"), "capture_reader_failed"),
    ),
)
def test_reader_failures_are_reduced_to_fixed_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    """Both expected and unexpected reader failures avoid exposing source text."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    def fail_read(*args: object, **kwargs: object) -> RawEvidenceReadResult:
        raise failure

    monkeypatch.setattr(RawResponseArtifactReader, "read", fail_read)
    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    formatted_traceback = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert captured.value.code == expected_code
    assert "CAPTURE_ERROR_SENTINEL" not in str(captured.value)
    assert "CAPTURE_ERROR_SENTINEL" not in formatted_traceback


def test_native_response_digest_must_match_the_capture_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parser result carrying a different response digest cannot bind to capture."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    parser = candidate_v2.parse_eastmoney_stock_daily_response

    def return_mismatched_digest(
        body: bytes,
        *,
        request: StockDailyNativeRequest,
    ) -> StockDailyNativeEnvelope:
        envelope = parser(body, request=request)
        return envelope.model_copy(update={"response_sha256": "0" * 64})

    monkeypatch.setattr(
        candidate_v2, "parse_eastmoney_stock_daily_response", return_mismatched_digest
    )
    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "native_response_binding_mismatch"


@pytest.mark.parametrize("malformed_kind", ("result", "manifest", "body"))
def test_capture_result_requires_exact_dto_and_bytes(
    tmp_path: Path,
    malformed_kind: str,
) -> None:
    """The defensive reader boundary rejects malformed result, manifest, or body types."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    result = _read_result(capture)
    malformed: object = object()
    if malformed_kind == "manifest":
        malformed = RawEvidenceReadResult(
            body=result.body,
            manifest=cast("Any", object()),
            manifest_sha256=result.manifest_sha256,
        )
    elif malformed_kind == "body":
        malformed = RawEvidenceReadResult(
            body=cast("Any", "not response bytes"),
            manifest=result.manifest,
            manifest_sha256=result.manifest_sha256,
        )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._validate_capture_result(
            cast("Any", malformed),
            manifest_sha256=capture.manifest_sha256,
            expected_source=_expected_source(),
            expected_request_sha256=_request_digest(capture.request),
            captured_at_not_after=_CAPTURE_BOUND,
        )

    assert captured.value.code == "capture_result_invalid"


def test_capture_manifest_is_revalidated_after_reader_return(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mutated manifest DTO cannot bypass strict field validation."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    result = _read_result(capture)
    invalid_manifest = result.manifest.model_copy(
        update={"response_size_bytes": _SerializationSentinel()}
    )
    malformed_result = RawEvidenceReadResult(
        body=result.body,
        manifest=invalid_manifest,
        manifest_sha256=result.manifest_sha256,
    )

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
            candidate_v2._validate_capture_result(
                malformed_result,
                manifest_sha256=capture.manifest_sha256,
                expected_source=_expected_source(),
                expected_request_sha256=_request_digest(capture.request),
                captured_at_not_after=_CAPTURE_BOUND,
            )
        output = capsys.readouterr()

    assert captured.value.code == "capture_manifest_invalid"
    assert caught_warnings == []
    assert "SERIALIZATION_SENTINEL" not in output.out + output.err + caplog.text
    assert "SERIALIZATION_SENTINEL" not in str(captured.value)
    assert captured.value.__suppress_context__ is True
    assert captured.value.__context__ is not None
    assert "SERIALIZATION_SENTINEL" in str(captured.value.__context__)


def test_capture_result_cannot_be_relabelled_with_a_different_manifest_hash(
    tmp_path: Path,
) -> None:
    """An authentic reader result is still bound to the digest passed downstream."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    result = _read_result(capture)

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._validate_capture_result(
            result,
            manifest_sha256="0" * 64,
            expected_source=_expected_source(),
            expected_request_sha256=_request_digest(capture.request),
            captured_at_not_after=_CAPTURE_BOUND,
        )

    assert captured.value.code == "capture_binding_mismatch"


def test_pin_and_snapshot_hashes_must_match_the_exact_supplied_bytes(tmp_path: Path) -> None:
    """Pin-document and full snapshot digests fail closed on tampering."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    with pytest.raises(IdentityBoundStockV2PreparationError) as bad_pin:
        prepare_identity_bound_stock_candidate_v2(
            reader=capture.reader,
            manifest_sha256=capture.manifest_sha256,
            dataset_id=_DATASET_ID,
            request=capture.request,
            captured_at_not_after=_CAPTURE_BOUND,
            pin_document_bytes=capture.pin_document_bytes,
            pin_document_sha256="0" * 64,
            identity_snapshot_bytes=capture.identity_snapshot_bytes,
        )
    assert bad_pin.value.code == "pin_document_rejected"

    with pytest.raises(IdentityBoundStockV2PreparationError) as bad_snapshot:
        prepare_identity_bound_stock_candidate_v2(
            reader=capture.reader,
            manifest_sha256=capture.manifest_sha256,
            dataset_id=_DATASET_ID,
            request=capture.request,
            captured_at_not_after=_CAPTURE_BOUND,
            pin_document_bytes=capture.pin_document_bytes,
            pin_document_sha256=capture.pin_document_sha256,
            identity_snapshot_bytes=capture.identity_snapshot_bytes + b" ",
        )
    assert bad_snapshot.value.code == "identity_snapshot_rejected"


@pytest.mark.parametrize(
    ("pin_updates", "expected_code"),
    (
        ({"dataset_id": "iteration204.other_dataset"}, "identity_pin_unavailable"),
        ({"source_id": "different-provider"}, "identity_pin_mismatch"),
        ({"snapshot_sha256": "0" * 64}, "identity_snapshot_pin_mismatch"),
        ({"reference_version": "other-reference"}, "identity_snapshot_pin_mismatch"),
    ),
)
def test_pin_selector_and_snapshot_reference_must_match(
    tmp_path: Path,
    pin_updates: dict[str, str],
    expected_code: str,
) -> None:
    """A validly hashed pin cannot select another dataset, source, or snapshot."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    pin_bytes, pin_sha256 = _pin_document_with_updates(capture, **pin_updates)

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare_with_overrides(
            capture,
            pin_document_bytes=pin_bytes,
            pin_document_sha256=pin_sha256,
        )

    assert captured.value.code == expected_code


def test_pin_decoder_unexpected_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected pin-decoder details do not cross the candidate boundary."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    def fail_decode(raw: bytes, expected_sha256: str) -> TemporalIdentityPinRegistry:
        raise RuntimeError("PIN_DECODER_SENTINEL")

    monkeypatch.setattr(candidate_v2, "decode_temporal_identity_pin_registry", fail_decode)
    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    formatted_traceback = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert captured.value.code == "pin_document_invalid"
    assert "PIN_DECODER_SENTINEL" not in str(captured.value)
    assert "PIN_DECODER_SENTINEL" not in formatted_traceback


def test_decoded_pin_is_revalidated_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pin object returned by a decoder is checked again at this boundary."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    valid_pin = TemporalIdentityPin.model_validate(
        json.loads(capture.pin_document_bytes)["pins"][0],
        strict=True,
    )
    invalid_pin = valid_pin.model_copy(update={"snapshot_sha256": _SerializationSentinel()})

    def return_invalid_pin(raw: bytes, expected_sha256: str) -> TemporalIdentityPinRegistry:
        return TemporalIdentityPinRegistry((invalid_pin,))

    monkeypatch.setattr(candidate_v2, "decode_temporal_identity_pin_registry", return_invalid_pin)
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
            _prepare(capture)
        output = capsys.readouterr()

    assert captured.value.code == "identity_pin_invalid"
    assert caught_warnings == []
    assert "SERIALIZATION_SENTINEL" not in output.out + output.err + caplog.text
    assert "SERIALIZATION_SENTINEL" not in str(captured.value)
    assert captured.value.__suppress_context__ is True
    assert captured.value.__context__ is not None
    assert "SERIALIZATION_SENTINEL" in str(captured.value.__context__)


def test_snapshot_decoder_unexpected_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected snapshot decoder details do not cross the candidate boundary."""
    capture = _fixture(
        tmp_path,
        assignments=(),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )

    def fail_decode(raw: bytes) -> TemporalIdentityRegistry:
        raise RuntimeError("SNAPSHOT_DECODER_SENTINEL")

    monkeypatch.setattr(candidate_v2, "decode_temporal_identity_snapshot", fail_decode)
    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    formatted_traceback = "".join(
        traceback.format_exception(
            type(captured.value), captured.value, captured.value.__traceback__
        )
    )
    assert captured.value.code == "identity_snapshot_invalid"
    assert "SNAPSHOT_DECODER_SENTINEL" not in str(captured.value)
    assert "SNAPSHOT_DECODER_SENTINEL" not in formatted_traceback


def test_native_response_market_mismatch_rejects_the_complete_capture(tmp_path: Path) -> None:
    """A response for a different market never degrades into an unknown row."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
        response_market=0,
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "native_response_invalid"


def test_auto_request_source_is_not_treated_as_the_akshare_capture(tmp_path: Path) -> None:
    """The capture contract accepts an explicit provider only, not auto-routing."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
        request_source="auto",
    )

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        _prepare(capture)

    assert captured.value.code == "request_source_unsupported"


def test_timezone_failure_is_sanitized_without_destroying_exception_context(
    tmp_path: Path,
) -> None:
    """Expose a fixed error while retaining the suppressed original as context."""
    assignment = _assignment("known", "instrument-known", "2024-01-01", None)
    capture = _fixture(
        tmp_path,
        assignments=(assignment,),
        rows=("2024-01-02,1,2,3,0,5,6,7,8,9,10",),
    )
    exploding_timezone = _ExplodingTimezone()
    bad_bound = datetime(2099, 1, 1, tzinfo=exploding_timezone)

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        prepare_identity_bound_stock_candidate_v2(
            reader=capture.reader,
            manifest_sha256=capture.manifest_sha256,
            dataset_id=_DATASET_ID,
            request=capture.request,
            captured_at_not_after=bad_bound,
            pin_document_bytes=capture.pin_document_bytes,
            pin_document_sha256=capture.pin_document_sha256,
            identity_snapshot_bytes=capture.identity_snapshot_bytes,
        )

    error = captured.value
    formatted_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert str(error) == "capture_bound_invalid"
    assert "TZ_OFFSET_SENTINEL" not in str(error)
    assert "TZ_OFFSET_SENTINEL" not in formatted_traceback
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert error.__context__ is exploding_timezone.sentinel


@pytest.mark.parametrize(
    ("result", "error", "expected_code"),
    (
        (
            None,
            TemporalInstrumentIdentityError("RESOLUTION_SENTINEL"),
            "identity_resolution_rejected",
        ),
        (object(), None, "identity_resolution_invalid"),
    ),
)
def test_row_resolution_rejects_errors_and_unexpected_result_types(
    result: object,
    error: Exception | None,
    expected_code: str,
) -> None:
    """The row resolver boundary accepts only one complete typed pair resolution."""
    trade_date = date(2024, 1, 2)
    pair = MarketSymbolPair(Exchange.XSHG, "600519")
    resolver = _SyntheticResolver(result, error=error)

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._assignment_for_row(
            cast("Any", resolver),
            pair,
            trade_date,
            ADAPTER_ID,
        )

    assert captured.value.code == expected_code


def test_row_resolution_overflow_is_reported_with_a_fixed_code() -> None:
    """A business date with no representable successor is rejected safely."""
    resolver = _SyntheticResolver(object())

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._assignment_for_row(
            cast("Any", resolver),
            MarketSymbolPair(Exchange.XSHG, "600519"),
            date.max,
            ADAPTER_ID,
        )

    assert captured.value.code == "identity_resolution_rejected"


def test_row_resolution_requires_a_matching_snapshot_assignment() -> None:
    """A known segment without its assignment in the supplied registry fails closed."""
    trade_date = date(2024, 1, 2)
    pair = MarketSymbolPair(Exchange.XSHG, "600519")
    resolution = _known_resolution(
        pair,
        trade_date,
        assignment_id="missing-assignment",
        instrument_id="instrument-known",
    )
    resolver = _SyntheticResolver(resolution)

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._assignment_for_row(
            cast("Any", resolver),
            pair,
            trade_date,
            ADAPTER_ID,
        )

    assert captured.value.code == "identity_assignment_missing"


def test_row_resolution_rejects_segment_metadata_that_disagrees_with_assignment() -> None:
    """A segment cannot relabel an assignment to a different instrument."""
    trade_date = date(2024, 1, 2)
    pair = MarketSymbolPair(Exchange.XSHG, "600519")
    assignment = _assignment(
        "known-assignment",
        "instrument-known",
        "2024-01-01",
        None,
    )
    resolution = _known_resolution(
        pair,
        trade_date,
        assignment_id=assignment.assignment_id,
        instrument_id="different-instrument",
        source_version=assignment.source_version,
        evidence_ref=assignment.evidence_ref,
        source_sha256=assignment.source_sha256,
    )
    resolver = _SyntheticResolver(resolution, assignments=(assignment,))

    with pytest.raises(IdentityBoundStockV2PreparationError) as captured:
        candidate_v2._assignment_for_row(
            cast("Any", resolver),
            pair,
            trade_date,
            ADAPTER_ID,
        )

    assert captured.value.code == "identity_assignment_mismatch"
