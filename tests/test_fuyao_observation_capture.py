"""Contract tests for preparing already-fetched Fuyao financial observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from opendata.data.providers.ths.endpoints import (
    financial_statement_items,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models.financial_observation_fetcher import (
    FinancialObservationFetchResult,
    ThsFinancialNativeUnitProfile,
    canonical_native_unit_mapping_sha256,
)
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeError,
    decode_financial_observation_body,
)
from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    TransportAttempt,
)
from opendata.pipeline import fuyao_observation_capture as capture_module
from opendata.pipeline.financial_observation_store import PreparedCapture
from opendata.pipeline.fuyao_observation_capture import (
    FuyaoCaptureProvenance,
    FuyaoObservationCaptureBridgeError,
    FuyaoSourceAttemptDeclaration,
    PreparedFuyaoObservationCapture,
    prepare_fuyao_observation_capture,
)
from opendata.pipeline.raw_evidence import (
    RawResponseArtifactWriter,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.raw_evidence_reader import RawResponseArtifactReader

if TYPE_CHECKING:
    from pathlib import Path

    from opendata.data.financial_observation import StatementType

_OBSERVED_AT = datetime(2025, 4, 2, 4, 0, tzinfo=timezone.utc)
_INSTRUMENT_ID = "600519.SH"


def _source_attempt(*, reserved_at_us: int | None = None) -> FuyaoSourceAttemptDeclaration:
    """Build a successful but explicitly caller-declared gate attempt."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    attempt = TransportAttempt(
        attempt_id="capture-attempt-0001",
        request_id="capture-request-0001",
        attempt_no=1,
        grant_id="grant-0001",
        expected_revision=1,
        source="ths",
        canonical_model="financial_statement",
        operation="query",
        principal_scope="operator-0001",
        host=host,
    )
    reservation = AttemptReservation(
        status=ReservationStatus.RESERVED_NEW,
        reserved_at_us=(
            int(_OBSERVED_AT.timestamp()) * 1_000_000 - 1
            if reserved_at_us is None
            else reserved_at_us
        ),
    )
    return FuyaoSourceAttemptDeclaration(attempt=attempt, reservation=reservation)


def _profile(
    *,
    statement_type: StatementType = "income",
    version: str = "synthetic-units-v1",
) -> ThsFinancialNativeUnitProfile:
    """Create an operator-declared mapping for the synthetic statement row."""
    units = dict.fromkeys(financial_statement_items(statement_type), "CNY")
    digest = canonical_native_unit_mapping_sha256(
        statement_type=statement_type,
        version=version,
        native_units=units,
    )
    return ThsFinancialNativeUnitProfile(
        statement_type=statement_type,
        version=version,
        native_units=units,
        mapping_sha256_declared=digest,
        evidence_ref="synthetic-test-profile",
    )


def _fetch_result(
    *,
    profile: ThsFinancialNativeUnitProfile | None = None,
    observed_at: datetime = _OBSERVED_AT,
    disclosure_date: date = date(2025, 3, 31),
    include_quarantined_row: bool = False,
) -> FinancialObservationFetchResult:
    """Build a synthetic Fuyao-envelope response and its exact decoded result."""
    checked_profile = profile or _profile()
    item_codes = financial_statement_items("income")
    period_end_ms = shanghai_midnight_millis(date(2024, 12, 31))
    disclosure_ms = shanghai_midnight_millis(disclosure_date)
    row: dict[str, object] = {
        "thscode": _INSTRUMENT_ID,
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": period_end_ms,
        "report_date_ms": disclosure_ms,
        "currency": "CNY",
    }
    row.update(dict.fromkeys(item_codes, "0"))
    row[item_codes[0]] = "123.45"
    response_rows = [row]
    if include_quarantined_row:
        quarantined_row = dict(row)
        quarantined_row["report_date_ms"] = shanghai_midnight_millis(date(2025, 4, 5))
        response_rows.append(quarantined_row)
    body = json.dumps(
        {
            "code": 0,
            "message": "ok",
            "request_id": "provider-request-0001",
            "data": {"timestamp": period_end_ms, "item": response_rows},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request_digest = request_summary_sha256(
        {"thscode": _INSTRUMENT_ID, "period": "annual"},
        allowed_fields={"thscode", "period", "start", "end", "limit"},
    )
    decoded = decode_financial_observation_body(
        body,
        expected_instrument_id=_INSTRUMENT_ID,
        statement_type="income",
        periodicity="annual",
        observed_at=observed_at,
        native_units=checked_profile.native_units,
        native_unit_mapping_version_declared=checked_profile.version,
        native_unit_mapping_sha256_declared=checked_profile.mapping_sha256_declared,
    )
    return FinancialObservationFetchResult(
        instrument_id=_INSTRUMENT_ID,
        statement_type="income",
        periodicity="annual",
        source_request_id=decoded.source_request_id,
        body_sha256=hashlib.sha256(body).hexdigest(),
        observed_at=observed_at,
        status_code=200,
        unit_mapping_version_declared=checked_profile.version,
        unit_mapping_sha256_declared=checked_profile.mapping_sha256_declared,
        unit_mapping_evidence_kind=checked_profile.evidence_kind,
        coverage_assessment="not_assessed",
        usability_status=capture_module._usability_status(
            decoded.observations, decoded.quarantined_rows
        ),
        raw_body=body,
        observations=decoded.observations,
        quarantined_rows=decoded.quarantined_rows,
        request_summary_sha256=request_digest,
    )


def _provenance() -> FuyaoCaptureProvenance:
    """Use safe synthetic raw-response provenance labels."""
    return FuyaoCaptureProvenance(
        raw_source=RawResponseSource(
            provider_id="ths",
            dataset_id="ths_financial_statement",
            operation_id="income_annual",
        ),
        retention_class="restricted",
        source_history_limit="unknown",
    )


def _prepare(
    root: Path, result: FinancialObservationFetchResult
) -> PreparedFuyaoObservationCapture:
    """Invoke the bridge with only local synthetic inputs."""
    return prepare_fuyao_observation_capture(
        result,
        unit_profile=_profile(),
        provenance=_provenance(),
        source_attempt_declaration=_source_attempt(),
        writer=RawResponseArtifactWriter(root, max_response_bytes=1024 * 1024),
    )


def test_bridge_archives_exact_entity_and_returns_store_prepared_values(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-raw-evidence"
    fetch_result = _fetch_result()
    assert fetch_result.request_summary_sha256 is not None

    outcome = _prepare(root, fetch_result)

    assert type(outcome.prepared) is PreparedCapture
    assert outcome.attempt_association_status == "caller_declared_unverified"
    assert len(outcome.manifest_sha256) == 64
    assert outcome.manifest_sha256 in {path.stem for path in (root / "manifests").glob("*/*.json")}
    reader = RawResponseArtifactReader(root)
    evidence = reader.read(
        outcome.manifest_sha256,
        expected_source=_provenance().raw_source,
        expected_request_summary_sha256=fetch_result.request_summary_sha256,
        captured_at_not_after=datetime.now(timezone.utc),
        expected_http_status=fetch_result.status_code,
    )
    assert evidence.body == fetch_result.raw_body
    assert evidence.manifest.response_sha256 == fetch_result.body_sha256
    assert evidence.manifest.request_summary_sha256 == fetch_result.request_summary_sha256
    assert evidence.manifest.captured_at_utc >= fetch_result.observed_at


def test_repeating_bridge_preparation_creates_a_new_manifest_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repeat-capture-evidence"
    fetch_result = _fetch_result()

    first = _prepare(root, fetch_result)
    second = _prepare(root, fetch_result)

    assert first.manifest_sha256 != second.manifest_sha256
    assert len(tuple((root / "manifests").glob("*/*.json"))) == 2


def test_missing_fetcher_request_digest_fails_before_creating_artifact_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    fetch_result = replace(_fetch_result(), request_summary_sha256=None)

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="request digest"):
        _prepare(root, fetch_result)

    assert not root.exists()


def test_unit_profile_mismatch_fails_before_writing_raw_bytes(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    fetch_result = _fetch_result()

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="unit profile differs"):
        prepare_fuyao_observation_capture(
            fetch_result,
            unit_profile=_profile(version="different-unit-profile-v1"),
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_denied_attempt_declaration_fails_before_writing_raw_bytes(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    attempt = _source_attempt().attempt
    declaration = FuyaoSourceAttemptDeclaration(
        attempt=attempt,
        reservation=AttemptReservation(
            status=ReservationStatus.DENIED,
            denial_code="synthetic-denial",
        ),
    )

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="not a new successful"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=declaration,
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_attempt_reservation_after_observation_fails_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    reserved_after_response = int(_OBSERVED_AT.timestamp()) * 1_000_000 + 1

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="not a new successful"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(reserved_at_us=reserved_after_response),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_future_observation_time_fails_before_writing_raw_bytes(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="later than the local capture"):
        prepare_fuyao_observation_capture(
            _fetch_result(observed_at=future),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_redecoded_observation_mismatch_fails_before_writing_raw_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    fetch_result = _fetch_result()
    first = fetch_result.observations[0]
    changed_observation = replace(first.observation, native_unit="UNDECLARED")
    changed_reference = replace(first, observation=changed_observation)
    inconsistent_result = replace(
        fetch_result,
        observations=(changed_reference, *fetch_result.observations[1:]),
    )

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="metadata differs"):
        _prepare(root, inconsistent_result)

    assert not root.exists()


@pytest.mark.parametrize(
    ("provider_id", "dataset_id", "operation_id"),
    (
        ("other", "ths_financial_statement", "income_annual"),
        ("ths", "unrelated", "income_annual"),
        ("ths", "ths_financial_statement", "cashflow_quarterly"),
    ),
)
def test_raw_provenance_must_match_statement_scope_before_writing(
    tmp_path: Path,
    provider_id: str,
    dataset_id: str,
    operation_id: str,
) -> None:
    root = tmp_path / "must-remain-absent"
    provenance = replace(
        _provenance(),
        raw_source=RawResponseSource(
            provider_id=provider_id,
            dataset_id=dataset_id,
            operation_id=operation_id,
        ),
    )

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="differs from the fetched"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=provenance,
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_writer_requires_the_reviewed_exact_type_before_creating_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="writer is not"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=object(),  # type: ignore[arg-type]
        )

    assert not root.exists()


def test_writer_response_hash_mismatch_is_rejected_before_reader_or_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "writer-mismatch"
    fetch_result = _fetch_result()

    def report_mismatched_capture(
        _writer: RawResponseArtifactWriter, _chunks: object, **_kwargs: object
    ) -> SimpleNamespace:
        return SimpleNamespace(
            manifest=SimpleNamespace(
                response_sha256="0" * 64,
                response_size_bytes=len(fetch_result.raw_body),
            ),
            manifest_path=SimpleNamespace(stem="a" * 64),
        )

    monkeypatch.setattr(RawResponseArtifactWriter, "capture", report_mismatched_capture)
    with pytest.raises(FuyaoObservationCaptureBridgeError, match="entity bytes differ"):
        prepare_fuyao_observation_capture(
            fetch_result,
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_fetch_result_exact_type_is_required_before_artifact_creation(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="fetch result is not"):
        _prepare(root, object())  # type: ignore[arg-type]

    assert not root.exists()


def test_fetch_result_reconstruction_rejects_a_corrupted_utc_timestamp(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    result = _fetch_result()
    object.__setattr__(
        result, "observed_at", datetime(2025, 4, 2, tzinfo=timezone(timedelta(hours=8)))
    )

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="failed boundary validation"):
        _prepare(root, result)

    assert not root.exists()


def test_fetch_result_rejects_invalid_instrument_identity_before_artifact_creation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    result = replace(_fetch_result(), instrument_id="not-an-exchange-qualified-ticker")

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="capture boundary checks"):
        _prepare(root, result)

    assert not root.exists()


def test_fetch_result_response_size_limit_is_enforced_before_decoding(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    oversized_body = b"x" * (8 * 1024 * 1024 + 1)
    result = replace(
        _fetch_result(),
        raw_body=oversized_body,
        body_sha256=hashlib.sha256(oversized_body).hexdigest(),
        observations=(),
        quarantined_rows=(),
        usability_status="NO_USABLE_OBSERVATIONS",
    )

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="capture boundary checks"):
        _prepare(root, result)

    assert not root.exists()


def test_unit_profile_exact_type_is_required_before_artifact_creation(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="unit profile is not"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=object(),  # type: ignore[arg-type]
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_corrupted_unit_profile_fails_reconstruction_before_artifact_creation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    profile = _profile()
    object.__setattr__(profile, "native_units", None)

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="unit profile failed"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=profile,
            provenance=_provenance(),
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_provenance_wrapper_exact_type_is_required_before_artifact_creation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="provenance is not"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=object(),  # type: ignore[arg-type]
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_raw_source_exact_type_is_required_before_artifact_creation(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    provenance = replace(_provenance(), raw_source=object())

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="raw source is not"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=provenance,
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_mutated_raw_source_is_revalidated_before_artifact_creation(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    provenance = _provenance()
    object.__setattr__(provenance.raw_source, "dataset_id", "invalid dataset id")

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="raw source failed"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=provenance,
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "message"),
    (
        ("retention_class", "unclassified", "retention declaration is invalid"),
        ("source_history_limit", "complete", "source history declaration is invalid"),
    ),
)
def test_provenance_labels_must_use_the_finite_vocabulary_before_write(
    tmp_path: Path,
    field_name: str,
    invalid_value: str,
    message: str,
) -> None:
    root = tmp_path / "must-remain-absent"
    provenance = replace(_provenance(), **{field_name: invalid_value})

    with pytest.raises(FuyaoObservationCaptureBridgeError, match=message):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=provenance,
            source_attempt_declaration=_source_attempt(),
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_source_attempt_declaration_exact_type_is_required_before_write(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="source attempt declaration"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=object(),  # type: ignore[arg-type]
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_transport_attempt_exact_type_is_required_before_write(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    declaration = replace(_source_attempt(), attempt=object())

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="transport attempt declaration"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=declaration,
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_transport_attempt_reconstruction_rejects_corrupted_values_before_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "must-remain-absent"
    declaration = _source_attempt()
    object.__setattr__(declaration.attempt, "attempt_no", True)

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="failed boundary validation"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=declaration,
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_transport_attempt_scope_is_bound_to_fuyao_before_write(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    declaration = _source_attempt()
    wrong_scope_attempt = replace(declaration.attempt, source="other-provider")
    declaration = replace(declaration, attempt=wrong_scope_attempt)

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="unexpected Fuyao scope"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=declaration,
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_reservation_declaration_exact_type_is_required_before_write(tmp_path: Path) -> None:
    root = tmp_path / "must-remain-absent"
    declaration = replace(_source_attempt(), reservation=object())

    with pytest.raises(FuyaoObservationCaptureBridgeError, match="attempt reservation declaration"):
        prepare_fuyao_observation_capture(
            _fetch_result(),
            unit_profile=_profile(),
            provenance=_provenance(),
            source_attempt_declaration=declaration,
            writer=RawResponseArtifactWriter(root),
        )

    assert not root.exists()


def test_decoder_error_is_preserved_and_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "must-remain-absent"

    def reject_body(*_args: object, **_kwargs: object) -> None:
        raise FinancialObservationDecodeError("json_invalid")

    monkeypatch.setattr(capture_module, "decode_financial_observation_body", reject_body)
    with pytest.raises(FinancialObservationDecodeError, match="json_invalid"):
        _prepare(root, _fetch_result())

    assert not root.exists()


def test_unexpected_decoder_error_is_sanitized_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "must-remain-absent"

    def fail_decoder(*_args: object, **_kwargs: object) -> None:
        raise TypeError("synthetic decoder failure")

    monkeypatch.setattr(capture_module, "decode_financial_observation_body", fail_decoder)
    with pytest.raises(FuyaoObservationCaptureBridgeError, match="could not be revalidated"):
        _prepare(root, _fetch_result())

    assert not root.exists()


def test_response_usability_labels_remain_response_local() -> None:
    empty_status = capture_module._usability_status((), ())
    quarantined_only_result = _fetch_result(disclosure_date=date(2025, 4, 5))
    mixed_result = _fetch_result(include_quarantined_row=True)

    assert empty_status == "NO_USABLE_OBSERVATIONS"
    assert not quarantined_only_result.observations
    assert quarantined_only_result.quarantined_rows
    assert (
        capture_module._usability_status(
            quarantined_only_result.observations,
            quarantined_only_result.quarantined_rows,
        )
        == "NO_USABLE_OBSERVATIONS"
    )
    assert mixed_result.observations
    assert mixed_result.quarantined_rows
    assert (
        capture_module._usability_status(
            mixed_result.observations,
            mixed_result.quarantined_rows,
        )
        == "PARTIAL"
    )
