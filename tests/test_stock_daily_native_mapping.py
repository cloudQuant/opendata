"""Offline behavior checks for raw-evidence to identity-mapping orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.stock_daily_native_mapping as mapping_module
from opendata.data.providers.akshare.stock_daily_native import (
    ADAPTER_ID,
    OPERATION_ID,
    StockDailyNativeRequest,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.temporal_instrument_identity import (
    Exchange,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)
from opendata.data.temporal_instrument_identity_snapshot import (
    encode_temporal_identity_snapshot,
)
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseManifest,
    RawResponseSource,
)
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)
from opendata.pipeline.stock_daily_native_mapping import (
    StockDailyIdentityRuntime,
    StockDailyNativeMappingCandidate,
    StockDailyNativeMappingError,
    prepare_stock_daily_native_mapping_candidate,
)

if TYPE_CHECKING:
    from pathlib import Path

_DATASET = "iteration204.stock_daily"
_REQUEST_BOUND = datetime(2099, 1, 1, tzinfo=timezone.utc)
_ASSIGNMENT = TemporalIdentityAssignment(
    assignment_id="fixture-assignment-1",
    instrument_id="fixture-instrument-1",
    exchange=Exchange.XSHG,
    symbol="600519",
    valid_from=date(2024, 1, 1),
    valid_to=None,
    source_id=ADAPTER_ID,
    source_version="synthetic-v1",
    evidence_ref="TEST-IDENTITY-SYNTHETIC",
)


@dataclass(frozen=True, slots=True)
class _Pin:
    """Synthetic caller-declared pin for an offline orchestration contract test."""

    dataset_id: str
    source_id: str
    authority_id: str
    registry_id: str
    reference_version: str
    snapshot_sha256: str


class _PinLookup:
    """Synthetic lookup that records the exact dataset key requested."""

    def __init__(self, pin: object | None) -> None:
        self.pin = pin
        self.requested: list[str] = []

    def get(self, dataset_id: str) -> object | None:
        self.requested.append(dataset_id)
        if isinstance(self.pin, _Pin) and self.pin.dataset_id == dataset_id:
            return self.pin
        return self.pin


class _SnapshotLookup:
    """Synthetic snapshot lookup with exact key binding and optional failures."""

    def __init__(self, registry: object, *, fail: bool = False) -> None:
        self.registry = registry
        self.fail = fail
        self.requested: list[dict[str, str]] = []

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> object:
        self.requested.append(
            {
                "authority_id": authority_id,
                "registry_id": registry_id,
                "reference_version": reference_version,
                "expected_sha256": expected_sha256,
            }
        )
        if self.fail:
            raise RuntimeError("synthetic database details must not escape")
        return self.registry


@dataclass(frozen=True, slots=True)
class _Runtime:
    """Synthetic injected runtime; the result must label its origin unverified."""

    enabled: bool
    pin_lookup: _PinLookup
    snapshot_store: _SnapshotLookup


@dataclass(frozen=True, slots=True)
class _Fixture:
    """One private synthetic raw capture and the expected identity lookup values."""

    reader: RawResponseArtifactReader
    manifest_sha256: str
    request: StockDailyNativeRequest
    registry: TemporalIdentityRegistry
    pin: _Pin
    body: bytes


def _request(
    symbol: str = "600519.SH", *, request_source: str = "akshare"
) -> StockDailyNativeRequest:
    """Build one explicit single-day Shanghai request."""
    return StockDailyNativeRequest.model_validate(
        {
            "symbol": symbol,
            "exchange": "SH",
            "start_date": "2024-01-02",
            "end_date": "2024-01-02",
            "request_source": request_source,
        }
    )


def _request_digest(request: StockDailyNativeRequest) -> str:
    """Match the stock-native adapter's fixed scalar request summary."""
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


def _fixture(tmp_path: Path, *, body: bytes | None = None) -> _Fixture:
    """Create an offline, content-addressed response and its canonical identity."""
    request = _request()
    response = (
        body
        if body is not None
        else json.dumps(
            {
                "rc": 0,
                "data": {
                    "code": request.symbol,
                    "market": request.market_id,
                    "klines": ["2024-01-02,1,2,3,0,0007,9,10,11,12,13"],
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    writer = RawResponseArtifactWriter(tmp_path / "raw", max_response_bytes=4096)
    source = RawResponseSource(
        provider_id=ADAPTER_ID,
        dataset_id=_DATASET,
        operation_id=OPERATION_ID,
    )
    capture = writer.capture(
        (response,),
        source=source,
        request_summary_sha256=_request_digest(request),
        http_status=200,
        retention_class="unknown",
        source_history_limit="unknown",
    )
    registry = TemporalIdentityRegistry(
        reference_version="identity-v1",
        evidence_ref="TEST-SNAPSHOT-SYNTHETIC",
        assignments=(_ASSIGNMENT,),
    )
    digest = hashlib.sha256(encode_temporal_identity_snapshot(registry)).hexdigest()
    pin = _Pin(
        dataset_id=_DATASET,
        source_id=ADAPTER_ID,
        authority_id="fixture-authority",
        registry_id="fixture-registry",
        reference_version=registry.reference_version,
        snapshot_sha256=digest,
    )
    return _Fixture(
        reader=RawResponseArtifactReader(tmp_path / "raw", max_response_bytes=4096),
        manifest_sha256=capture.manifest_path.stem,
        request=request,
        registry=registry,
        pin=pin,
        body=response,
    )


def _runtime(
    fixture: _Fixture,
    *,
    pin: object | None = None,
    registry: object | None = None,
    enabled: bool = True,
    fail_snapshot: bool = False,
) -> tuple[_Runtime, _PinLookup, _SnapshotLookup]:
    """Build synthetic protocol collaborators that never reach a real database."""
    pins = _PinLookup(fixture.pin if pin is None else pin)
    snapshots = _SnapshotLookup(
        fixture.registry if registry is None else registry,
        fail=fail_snapshot,
    )
    return _Runtime(enabled, pins, snapshots), pins, snapshots


def _prepare(
    fixture: _Fixture, runtime: StockDailyIdentityRuntime
) -> StockDailyNativeMappingCandidate:
    """Run the public orchestration contract with a future UTC evidence bound."""
    return prepare_stock_daily_native_mapping_candidate(
        reader=fixture.reader,
        identity_runtime=runtime,
        manifest_sha256=fixture.manifest_sha256,
        dataset_id=_DATASET,
        request=fixture.request,
        captured_at_not_after=_REQUEST_BOUND,
    )


def test_valid_synthetic_capture_maps_only_to_an_unpublished_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, pins, snapshots = _runtime(fixture)

    result = _prepare(fixture, runtime)

    assert isinstance(result, StockDailyNativeMappingCandidate)
    assert result.batch.mapping_status == "identity_bound_candidate"
    assert result.batch.rows[0].instrument_id == "fixture-instrument-1"
    assert result.batch.rows[0].raw_tokens == (
        "2024-01-02",
        "1",
        "2",
        "3",
        "0",
        "0007",
        "9",
        "10",
        "11",
        "12",
        "13",
    )
    assert result.raw_evidence_integrity_status == "local_manifest_and_body_verified"
    assert result.identity_pin_origin_status == "caller_supplied_runtime_unverified"
    assert result.source_authenticity_status == "unverified"
    assert result.history_coverage_status == "unknown"
    assert result.persistence_status == "not_persisted"
    assert result.publication_status == "not_published"
    assert pins.requested == [_DATASET]
    assert snapshots.requested == [
        {
            "authority_id": fixture.pin.authority_id,
            "registry_id": fixture.pin.registry_id,
            "reference_version": fixture.pin.reference_version,
            "expected_sha256": fixture.pin.snapshot_sha256,
        }
    ]


def test_reader_and_request_must_be_exact_supported_types(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runtime, _, _ = _runtime(fixture)

    with pytest.raises(TypeError, match="exact RawResponseArtifactReader"):
        prepare_stock_daily_native_mapping_candidate(
            reader=object(),  # type: ignore[arg-type]
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=fixture.request,
            captured_at_not_after=_REQUEST_BOUND,
        )
    with pytest.raises(TypeError, match="exact StockDailyNativeRequest"):
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=object(),  # type: ignore[arg-type]
            captured_at_not_after=_REQUEST_BOUND,
        )


@pytest.mark.parametrize(
    ("manifest_sha256", "dataset_id", "bound", "expected_code"),
    [
        ("bad", _DATASET, _REQUEST_BOUND, "manifest_digest_invalid"),
        ("a" * 64, "../bad", _REQUEST_BOUND, "dataset_id_invalid"),
        ("a" * 64, _DATASET, datetime(2099, 1, 1), "capture_bound_invalid"),
        ("a" * 64, _DATASET, datetime(2099, 1, 1, tzinfo=timezone.utc), "raw_evidence_rejected"),
    ],
)
def test_invalid_top_level_evidence_selectors_fail_closed(
    tmp_path: Path,
    manifest_sha256: str,
    dataset_id: str,
    bound: datetime,
    expected_code: str,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, _, _ = _runtime(fixture)

    with pytest.raises(StockDailyNativeMappingError) as captured:
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=manifest_sha256,
            dataset_id=dataset_id,
            request=fixture.request,
            captured_at_not_after=bound,
        )

    assert captured.value.code == expected_code


def test_timezone_failure_and_invalid_reconstructed_request_are_rejected(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, _, _ = _runtime(fixture)

    class BrokenTimezone(tzinfo):
        def utcoffset(self, _dt: datetime | None) -> timedelta:
            raise ValueError("timezone implementation detail")

        def dst(self, _dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, _dt: datetime | None) -> str | None:
            return "broken-synthetic-timezone"

    with pytest.raises(StockDailyNativeMappingError) as bad_bound:
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=fixture.request,
            captured_at_not_after=datetime(2024, 1, 1, tzinfo=BrokenTimezone()),
        )
    assert bad_bound.value.code == "capture_bound_invalid"

    invalid_request = StockDailyNativeRequest.model_construct(
        symbol="bad-symbol",
        exchange="SH",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 2),
        request_source="akshare",
    )
    with pytest.raises(StockDailyNativeMappingError) as bad_request:
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=invalid_request,
            captured_at_not_after=_REQUEST_BOUND,
        )
    assert bad_request.value.code == "request_invalid"

    automatic_request = _request(request_source="auto")
    with pytest.raises(StockDailyNativeMappingError) as unsupported_source:
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=automatic_request,
            captured_at_not_after=_REQUEST_BOUND,
        )
    assert unsupported_source.value.code == "request_source_unsupported"


def test_request_scope_mismatch_is_rejected_before_identity_lookup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runtime, pins, snapshots = _runtime(fixture)
    other_request = _request("600000.SH")

    with pytest.raises(StockDailyNativeMappingError) as captured:
        prepare_stock_daily_native_mapping_candidate(
            reader=fixture.reader,
            identity_runtime=runtime,
            manifest_sha256=fixture.manifest_sha256,
            dataset_id=_DATASET,
            request=other_request,
            captured_at_not_after=_REQUEST_BOUND,
        )

    assert captured.value.code == "raw_evidence_rejected"
    assert pins.requested == []
    assert snapshots.requested == []


def test_saved_body_mutation_is_rejected_before_identity_lookup(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runtime, pins, _ = _runtime(fixture)
    object_path = (
        tmp_path
        / "raw"
        / "objects"
        / hashlib.sha256(fixture.body).hexdigest()[:2]
        / f"{hashlib.sha256(fixture.body).hexdigest()}.blob"
    )
    object_path.write_bytes(b"mutated synthetic body")

    with pytest.raises(StockDailyNativeMappingError) as captured:
        _prepare(fixture, runtime)

    assert captured.value.code == "raw_evidence_rejected"
    assert pins.requested == []


def test_reader_exceptions_and_fabricated_result_values_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, pins, _ = _runtime(fixture)

    def raise_reader_error(*_args: object, **_kwargs: object) -> RawEvidenceReadResult:
        raise RuntimeError("synthetic reader internals must not escape")

    monkeypatch.setattr(fixture.reader, "read", raise_reader_error)
    with pytest.raises(StockDailyNativeMappingError) as failed:
        _prepare(fixture, runtime)
    assert failed.value.code == "raw_evidence_reader_failed"
    assert "synthetic reader internals" not in str(failed.value)
    assert pins.requested == []

    monkeypatch.undo()
    actual = fixture.reader.read(
        fixture.manifest_sha256,
        expected_source=RawResponseSource(
            provider_id=ADAPTER_ID,
            dataset_id=_DATASET,
            operation_id=OPERATION_ID,
        ),
        expected_request_summary_sha256=_request_digest(fixture.request),
        captured_at_not_after=_REQUEST_BOUND,
    )
    monkeypatch.setattr(fixture.reader, "read", lambda *_args, **_kwargs: object())
    with pytest.raises(StockDailyNativeMappingError) as bad_result:
        _prepare(fixture, runtime)
    assert bad_result.value.code == "raw_evidence_result_invalid"

    forged_hash = replace(actual, manifest_sha256="b" * 64)
    monkeypatch.setattr(fixture.reader, "read", lambda *_args, **_kwargs: forged_hash)
    with pytest.raises(StockDailyNativeMappingError) as bad_binding:
        _prepare(fixture, runtime)
    assert bad_binding.value.code == "raw_evidence_binding_mismatch"

    malformed_manifest_values = actual.manifest.model_dump(mode="python")
    malformed_manifest_values["capture_id"] = "not-a-capture-id"
    malformed_manifest = RawResponseManifest.model_construct(**malformed_manifest_values)
    malformed_result = RawEvidenceReadResult(
        body=actual.body,
        manifest=malformed_manifest,
        manifest_sha256=actual.manifest_sha256,
    )
    monkeypatch.setattr(fixture.reader, "read", lambda *_args, **_kwargs: malformed_result)
    with pytest.raises(StockDailyNativeMappingError) as malformed:
        _prepare(fixture, runtime)
    assert malformed.value.code == "raw_evidence_result_invalid"


def test_reader_result_must_be_the_exact_reader_dto_and_manifest_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, _, _ = _runtime(fixture)
    actual = fixture.reader.read(
        fixture.manifest_sha256,
        expected_source=RawResponseSource(
            provider_id=ADAPTER_ID,
            dataset_id=_DATASET,
            operation_id=OPERATION_ID,
        ),
        expected_request_summary_sha256=_request_digest(fixture.request),
        captured_at_not_after=_REQUEST_BOUND,
    )
    subclass_result = type("SubResult", (RawEvidenceReadResult,), {})(
        body=actual.body,
        manifest=actual.manifest,
        manifest_sha256=actual.manifest_sha256,
    )
    monkeypatch.setattr(fixture.reader, "read", lambda *_args, **_kwargs: subclass_result)

    with pytest.raises(StockDailyNativeMappingError) as captured:
        _prepare(fixture, runtime)

    assert captured.value.code == "raw_evidence_result_invalid"

    malformed_manifest_result = RawEvidenceReadResult(
        body=actual.body,
        manifest=object(),  # type: ignore[arg-type]
        manifest_sha256=actual.manifest_sha256,
    )
    monkeypatch.setattr(
        fixture.reader,
        "read",
        lambda *_args, **_kwargs: malformed_manifest_result,
    )
    with pytest.raises(StockDailyNativeMappingError) as malformed_manifest:
        _prepare(fixture, runtime)
    assert malformed_manifest.value.code == "raw_evidence_result_invalid"

    mutable_body_result = RawEvidenceReadResult(
        body=bytearray(actual.body),  # type: ignore[arg-type]
        manifest=actual.manifest,
        manifest_sha256=actual.manifest_sha256,
    )
    monkeypatch.setattr(
        fixture.reader,
        "read",
        lambda *_args, **_kwargs: mutable_body_result,
    )
    with pytest.raises(StockDailyNativeMappingError) as mutable_body:
        _prepare(fixture, runtime)
    assert mutable_body.value.code == "raw_evidence_result_invalid"


def test_invalid_native_body_does_not_reach_identity_runtime(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, body=b"not-json")
    runtime, pins, snapshots = _runtime(fixture)

    with pytest.raises(StockDailyNativeMappingError) as captured:
        _prepare(fixture, runtime)

    assert captured.value.code == "native_payload_invalid"
    assert pins.requested == []
    assert snapshots.requested == []


def test_disabled_or_incomplete_runtime_fails_without_looking_up_a_pin(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    disabled, pins, _ = _runtime(fixture, enabled=False)

    with pytest.raises(StockDailyNativeMappingError) as captured:
        _prepare(fixture, disabled)

    assert captured.value.code == "identity_runtime_unavailable"
    assert pins.requested == []

    with pytest.raises(StockDailyNativeMappingError) as incomplete:
        _prepare(fixture, object())  # type: ignore[arg-type]
    assert incomplete.value.code == "identity_runtime_unavailable"

    malformed = _Runtime(True, object(), _SnapshotLookup(fixture.registry))  # type: ignore[arg-type]
    with pytest.raises(StockDailyNativeMappingError) as no_lookup:
        _prepare(fixture, malformed)
    assert no_lookup.value.code == "identity_runtime_unavailable"


def test_pin_lookup_errors_and_malformed_values_are_sanitized(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    class FailingPinLookup(_PinLookup):
        def __init__(self) -> None:
            super().__init__(None)

        def get(self, dataset_id: str) -> object | None:
            del dataset_id
            raise RuntimeError("private pin file path must not escape")

    failing = _Runtime(True, FailingPinLookup(), _SnapshotLookup(fixture.registry))
    with pytest.raises(StockDailyNativeMappingError) as failed:
        _prepare(fixture, failing)
    assert failed.value.code == "identity_pin_unavailable"
    assert "private pin file path" not in str(failed.value)

    malformed_pin = _Pin(
        dataset_id=_DATASET,
        source_id=ADAPTER_ID,
        authority_id="invalid space",
        registry_id="fixture-registry",
        reference_version="identity-v1",
        snapshot_sha256="a" * 64,
    )
    malformed, _, snapshots = _runtime(fixture, pin=malformed_pin)
    with pytest.raises(StockDailyNativeMappingError) as invalid:
        _prepare(fixture, malformed)
    assert invalid.value.code == "identity_pin_invalid"
    assert snapshots.requested == []

    missing = _Runtime(True, _PinLookup(None), _SnapshotLookup(fixture.registry))
    with pytest.raises(StockDailyNativeMappingError) as absent:
        _prepare(fixture, missing)
    assert absent.value.code == "identity_pin_unavailable"

    non_string_pin = _Pin(
        dataset_id=_DATASET,
        source_id=ADAPTER_ID,
        authority_id="fixture-authority",
        registry_id="fixture-registry",
        reference_version="identity-v1",
        snapshot_sha256="a" * 64,
    )
    object.__setattr__(non_string_pin, "authority_id", 42)
    malformed, _, snapshots = _runtime(fixture, pin=non_string_pin)
    with pytest.raises(StockDailyNativeMappingError) as invalid_type:
        _prepare(fixture, malformed)
    assert invalid_type.value.code == "identity_pin_invalid"
    assert snapshots.requested == []

    invalid_digest_pin = replace(fixture.pin, snapshot_sha256="invalid")
    malformed, _, snapshots = _runtime(fixture, pin=invalid_digest_pin)
    with pytest.raises(StockDailyNativeMappingError) as invalid_digest:
        _prepare(fixture, malformed)
    assert invalid_digest.value.code == "identity_pin_invalid"
    assert snapshots.requested == []


def test_pin_must_match_the_requested_dataset_and_fixed_source(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    wrong_dataset = _Pin(
        dataset_id="another.dataset",
        source_id=ADAPTER_ID,
        authority_id=fixture.pin.authority_id,
        registry_id=fixture.pin.registry_id,
        reference_version=fixture.pin.reference_version,
        snapshot_sha256=fixture.pin.snapshot_sha256,
    )
    runtime, _, snapshots = _runtime(fixture, pin=wrong_dataset)
    with pytest.raises(StockDailyNativeMappingError) as mismatch:
        _prepare(fixture, runtime)
    assert mismatch.value.code == "identity_pin_mismatch"
    assert snapshots.requested == []

    wrong_source = _Pin(
        dataset_id=_DATASET,
        source_id="different-provider",
        authority_id=fixture.pin.authority_id,
        registry_id=fixture.pin.registry_id,
        reference_version=fixture.pin.reference_version,
        snapshot_sha256=fixture.pin.snapshot_sha256,
    )
    runtime, _, snapshots = _runtime(fixture, pin=wrong_source)
    with pytest.raises(StockDailyNativeMappingError) as mismatch:
        _prepare(fixture, runtime)
    assert mismatch.value.code == "identity_pin_mismatch"
    assert snapshots.requested == []


def test_snapshot_store_failure_and_non_registry_result_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    failing, _, _ = _runtime(fixture, fail_snapshot=True)

    with pytest.raises(StockDailyNativeMappingError) as unavailable:
        _prepare(fixture, failing)
    assert unavailable.value.code == "identity_snapshot_unavailable"

    malformed, _, _ = _runtime(fixture, registry={"forged": "mapping"})
    with pytest.raises(StockDailyNativeMappingError) as invalid:
        _prepare(fixture, malformed)
    assert invalid.value.code == "identity_snapshot_invalid"


def test_snapshot_digest_mismatch_and_unknown_identity_never_make_a_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    wrong_digest_pin = _Pin(
        dataset_id=fixture.pin.dataset_id,
        source_id=fixture.pin.source_id,
        authority_id=fixture.pin.authority_id,
        registry_id=fixture.pin.registry_id,
        reference_version=fixture.pin.reference_version,
        snapshot_sha256="a" * 64,
    )
    wrong_digest_runtime, _, _ = _runtime(fixture, pin=wrong_digest_pin)
    with pytest.raises(StockDailyNativeMappingError) as mismatch:
        _prepare(fixture, wrong_digest_runtime)
    assert mismatch.value.code == "identity_mapping_rejected"

    uncovered = TemporalIdentityRegistry(
        reference_version="identity-v1",
        evidence_ref="TEST-EMPTY-SNAPSHOT",
        assignments=(),
    )
    uncovered_digest = hashlib.sha256(encode_temporal_identity_snapshot(uncovered)).hexdigest()
    uncovered_pin = _Pin(
        dataset_id=fixture.pin.dataset_id,
        source_id=fixture.pin.source_id,
        authority_id=fixture.pin.authority_id,
        registry_id=fixture.pin.registry_id,
        reference_version=fixture.pin.reference_version,
        snapshot_sha256=uncovered_digest,
    )
    uncovered_runtime, _, _ = _runtime(fixture, registry=uncovered, pin=uncovered_pin)
    with pytest.raises(StockDailyNativeMappingError) as unknown:
        _prepare(fixture, uncovered_runtime)
    assert unknown.value.code == "identity_mapping_rejected"


def test_mapper_unexpected_failures_and_wrong_results_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    runtime, _, _ = _runtime(fixture)

    def raise_mapper(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic mapper internals must not escape")

    monkeypatch.setattr(mapping_module, "map_stock_daily_native_envelope", raise_mapper)
    with pytest.raises(StockDailyNativeMappingError) as failed:
        _prepare(fixture, runtime)
    assert failed.value.code == "identity_mapping_failed"
    assert "synthetic mapper internals" not in str(failed.value)

    monkeypatch.setattr(
        mapping_module,
        "map_stock_daily_native_envelope",
        lambda *_args, **_kwargs: object(),
    )
    with pytest.raises(StockDailyNativeMappingError) as invalid:
        _prepare(fixture, runtime)
    assert invalid.value.code == "identity_mapping_invalid_result"


def test_corrupt_pin_object_is_rejected_without_exposing_attribute_errors(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    class RaisingPin:
        @property
        def dataset_id(self) -> str:
            raise RuntimeError("synthetic descriptor detail")

    runtime, _, snapshots = _runtime(fixture, pin=RaisingPin())
    with pytest.raises(StockDailyNativeMappingError) as captured:
        _prepare(fixture, runtime)
    assert captured.value.code == "identity_pin_invalid"
    assert "synthetic descriptor detail" not in str(captured.value)
    assert snapshots.requested == []
