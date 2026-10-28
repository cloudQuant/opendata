"""Offline checks for the default-off Fuyao manual preflight composition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import pytest

from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
from opendata.data.request_budget import GrantDecision, RequestOperation
from opendata.data.source_policy import SourcePolicyDocument, SourceUsePolicy
from opendata.pipeline import fuyao_manual_composition as composition
from opendata.pipeline.fuyao_manual_composition import (
    FuyaoManualCompositionConfig,
    FuyaoManualCompositionError,
    FuyaoManualQuery,
    prepare_fuyao_manual_preflight,
)
from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding

if TYPE_CHECKING:
    from pathlib import Path

_INSTRUMENT = "999999.BJ"
_BUNDLE_PIN = "a" * 64
_POLICY_BODY = b"synthetic-policy-document"
_NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
_SYNTHETIC_KEY = "synthetic-fuyao-key-never-send"


class _NoOffsetTimezone(tzinfo):
    """Synthetic timezone that makes an otherwise aware-looking value naive."""

    def utcoffset(self, _dt: datetime | None) -> timedelta | None:
        return None


class _BrokenTimezone(tzinfo):
    """Synthetic timezone whose offset lookup fails with a safe value error."""

    def utcoffset(self, _dt: datetime | None) -> timedelta | None:
        raise ValueError("synthetic invalid offset")


def _query() -> FuyaoManualQuery:
    """Return one synthetic bounded query."""
    return FuyaoManualQuery(
        instrument_id=_INSTRUMENT,
        statement_type="income",
        periodicity="annual",
        idempotency_key="synthetic-manual-run-001",
        limit=3,
    )


def _policy_document(
    *,
    host: str | None = None,
    expires_at: datetime | None = None,
    canonical_model: str = "financial_statement",
) -> SourcePolicyDocument:
    """Return one synthetic operator declaration without legal significance."""
    selected_host = host or urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert selected_host is not None
    policy = SourceUsePolicy(
        policy_id="synthetic-manual-policy",
        source="ths",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        principal_user_id=17,
        product="synthetic fixture",
        purpose="offline composition test",
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic fixture only",
        allowed_hosts=(selected_host,),
        expires_at=expires_at or datetime(2099, 1, 1, tzinfo=timezone.utc),
        conditions=(),
        task_attempts=1,
        source_attempts=1,
    )
    return SourcePolicyDocument(version=1, policies=(policy,))


def _install_preflight_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    document: SourcePolicyDocument | None = None,
) -> None:
    """Stub external readers while retaining the composition's validation path."""
    selected_document = document if document is not None else _policy_document()
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id=_INSTRUMENT,
            bundle_sha256=_BUNDLE_PIN,
        ),
    )
    monkeypatch.setattr(composition, "_read_atomic_regular_file", lambda _path: _POLICY_BODY)
    monkeypatch.setattr(
        composition,
        "parse_source_policy_document",
        lambda _body: selected_document,
    )


def _enabled_config(
    tmp_path: Path,
    *,
    query: FuyaoManualQuery | None = None,
) -> FuyaoManualCompositionConfig:
    """Build an enabled config with synthetic, non-secret deployment values."""
    return FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding(tmp_path),
        query=_query() if query is None else query,
    )


def _binding(tmp_path: Path) -> FuyaoProcessDeploymentBinding:
    """Return an inert binding whose paths are never opened by this helper."""
    policy_pin = hashlib.sha256(_POLICY_BODY).hexdigest()
    return _binding_with_pins(tmp_path, bundle_pin=_BUNDLE_PIN, policy_pin=policy_pin)


def _binding_with_pins(
    tmp_path: Path,
    *,
    bundle_pin: str,
    policy_pin: str,
) -> FuyaoProcessDeploymentBinding:
    """Build a synthetic binding with explicit content pins."""
    return FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_KEY,
        financial_bundle_path=str(tmp_path / "bundle.json"),
        financial_bundle_sha256=bundle_pin,
        source_policy_path=str(tmp_path / "policy.json"),
        source_policy_sha256=policy_pin,
        gate_database_path=str(tmp_path / "gate.sqlite3"),
        source_attempt_grant_id="synthetic-grant",
        source_attempt_expected_revision=1,
        source_attempt_principal_scope="synthetic-worker",
        raw_artifact_root=str(tmp_path / "raw"),
        retention_class="unknown",
        source_history_limit="unknown",
    )


def _write_bundle(path: Path) -> str:
    """Write a real-loader-compatible single-instrument synthetic bundle."""
    from opendata.data.instrument_reference import (
        InstrumentLifecycle,
        InstrumentReferenceRegistry,
        LifecycleStatus,
    )
    from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
    from opendata.data.providers.ths.endpoints import financial_statement_items
    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        ThsFinancialNativeUnitProfile,
        canonical_native_unit_mapping_sha256,
    )

    registry = InstrumentReferenceRegistry(
        registry_version="synthetic-registry-v1",
        evidence_ref="synthetic:test-only-registry",
        lifecycles=(
            InstrumentLifecycle(
                dataset_id="financial_statement",
                source_id="ths",
                instrument_id=_INSTRUMENT,
                lifecycle_version="synthetic-lifecycle-v1",
                status=LifecycleStatus.ACTIVE,
                status_as_of=date(2026, 10, 29),
                status_evidence_ref="synthetic:test-only-status",
                start_boundary_unknown=True,
            ),
        ),
    )
    unit_profiles: dict[str, ThsFinancialNativeUnitProfile] = {}
    for statement in ("income", "balance", "cashflow"):
        units = dict.fromkeys(financial_statement_items(statement), "synthetic-unit")
        version = "synthetic-unit-profile-v1"
        unit_profiles[statement] = ThsFinancialNativeUnitProfile(
            statement_type=statement,
            version=version,
            native_units=units,
            mapping_sha256_declared=canonical_native_unit_mapping_sha256(
                statement_type=statement,
                version=version,
                native_units=units,
            ),
            evidence_ref="synthetic:test-only-unit-declaration",
        )
    document = {
        "schema_version": 1,
        "scope": {"instrument_id": _INSTRUMENT},
        "instrument_reference_snapshot": json.loads(
            encode_instrument_reference_snapshot(registry).decode("utf-8")
        ),
        "unit_profiles": {
            statement: {
                "statement_type": profile.statement_type,
                "version": profile.version,
                "native_units": dict(profile.native_units),
                "mapping_sha256_declared": profile.mapping_sha256_declared,
                "evidence_ref": profile.evidence_ref,
            }
            for statement, profile in unit_profiles.items()
        },
    }
    body = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(body)
    path.chmod(0o600)
    return hashlib.sha256(body).hexdigest()


def _write_policy(path: Path, *, decision: GrantDecision = GrantDecision.ALLOWED) -> str:
    """Write one synthetic policy file accepted by the real bounded parser."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    policy = SourceUsePolicy(
        policy_id="synthetic-manual-policy",
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        principal_user_id=17,
        product="synthetic fixture",
        purpose="offline composition test",
        decision=decision,
        rights_evidence="synthetic fixture only",
        allowed_hosts=(host,),
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        conditions=(),
        task_attempts=1,
        source_attempts=1,
    )
    body = json.dumps(
        {"version": 1, "policies": [policy.model_dump(mode="json")]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(body)
    path.chmod(0o600)
    return hashlib.sha256(body).hexdigest()


def test_default_preflight_is_disabled_before_reading_any_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-argument path does not open files or inspect credentials."""
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: pytest.fail("disabled preflight must not load a bundle"),
    )
    monkeypatch.setattr(
        composition,
        "_read_atomic_regular_file",
        lambda _path: pytest.fail("disabled preflight must not read policy bytes"),
    )

    report = prepare_fuyao_manual_preflight()

    assert report.status == "disabled"
    assert report.dispatch_allowed is False
    assert report.scheduler_started is False
    assert report.worker_started is False
    assert report.gate_grant_status == "not_checked"


def test_enabled_preflight_requires_deployment_binding_and_query() -> None:
    """Opt-in without explicit deployment values is refused safely."""
    config = FuyaoManualCompositionConfig(preflight_enabled=True)

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "deployment_binding_invalid"
    assert _SYNTHETIC_KEY not in str(error.value)


def test_pinned_preflight_returns_summary_without_claiming_gate_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid local pins produce only a non-executable summary."""
    binding = _binding(tmp_path)
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id=_INSTRUMENT,
            bundle_sha256=_BUNDLE_PIN,
        ),
    )
    monkeypatch.setattr(composition, "_read_atomic_regular_file", lambda _path: _POLICY_BODY)
    monkeypatch.setattr(
        composition,
        "parse_source_policy_document",
        lambda _body: _policy_document(),
    )
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=binding,
        query=_query(),
    )

    report = prepare_fuyao_manual_preflight(config)

    assert report.status == "preflight_only"
    assert report.instrument_id == _INSTRUMENT
    assert report.source_policy_id == "synthetic-manual-policy"
    assert report.bundle_sha256 == _BUNDLE_PIN
    assert report.source_policy_sha256 == binding.source_policy_sha256
    assert report.dispatch_allowed is False
    assert report.scheduler_started is False
    assert report.worker_started is False
    assert report.gate_grant_status == "not_checked"
    assert _SYNTHETIC_KEY not in repr(binding)
    assert _SYNTHETIC_KEY not in repr(config)
    assert _SYNTHETIC_KEY not in repr(report)


def test_source_policy_pin_mismatch_fails_with_fixed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy file that differs from its deployment pin blocks preflight."""
    binding = _binding(tmp_path)
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id=_INSTRUMENT,
            bundle_sha256=_BUNDLE_PIN,
        ),
    )
    monkeypatch.setattr(composition, "_read_atomic_regular_file", lambda _path: b"changed")
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=binding,
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "source_policy_pin_mismatch"
    assert _SYNTHETIC_KEY not in str(error.value)


def test_bundle_scope_mismatch_fails_before_policy_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bundle for another instrument cannot pass the manual query scope."""
    binding = _binding(tmp_path)
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id="999998.BJ",
            bundle_sha256=_BUNDLE_PIN,
        ),
    )
    monkeypatch.setattr(
        composition,
        "_read_atomic_regular_file",
        lambda _path: pytest.fail("bundle scope mismatch must stop before policy loading"),
    )
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=binding,
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "bundle_pin_or_scope_invalid"


def test_real_synthetic_files_pass_both_external_pins(
    tmp_path: Path,
) -> None:
    """The real bundle and policy readers accept exact synthetic file pins."""
    bundle_pin = _write_bundle(tmp_path / "bundle.json")
    policy_pin = _write_policy(tmp_path / "policy.json")
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding_with_pins(
            tmp_path,
            bundle_pin=bundle_pin,
            policy_pin=policy_pin,
        ),
        query=_query(),
    )

    report = prepare_fuyao_manual_preflight(config, now=_NOW)

    assert report.status == "preflight_only"
    assert report.instrument_id == _INSTRUMENT
    assert report.bundle_sha256 == bundle_pin
    assert report.source_policy_sha256 == policy_pin
    assert report.gate_grant_status == "not_checked"
    assert report.dispatch_allowed is False


def test_real_bundle_file_change_after_pin_fails_closed(
    tmp_path: Path,
) -> None:
    """Changing one byte after recording the bundle pin blocks preflight."""
    bundle_pin = _write_bundle(tmp_path / "bundle.json")
    policy_pin = _write_policy(tmp_path / "policy.json")
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(bundle_path.read_bytes() + b" ")
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding_with_pins(
            tmp_path,
            bundle_pin=bundle_pin,
            policy_pin=policy_pin,
        ),
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "bundle_pin_or_schema_invalid"
    assert _SYNTHETIC_KEY not in str(error.value)


def test_real_policy_file_change_after_pin_fails_closed(
    tmp_path: Path,
) -> None:
    """Changing one byte after recording the policy pin blocks preflight."""
    bundle_pin = _write_bundle(tmp_path / "bundle.json")
    policy_pin = _write_policy(tmp_path / "policy.json")
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(policy_path.read_bytes() + b" ")
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding_with_pins(
            tmp_path,
            bundle_pin=bundle_pin,
            policy_pin=policy_pin,
        ),
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "source_policy_pin_mismatch"
    assert _SYNTHETIC_KEY not in str(error.value)


def test_real_pinned_denied_policy_fails_closed(
    tmp_path: Path,
) -> None:
    """A content-pinned DENIED policy cannot produce a preflight report."""
    bundle_pin = _write_bundle(tmp_path / "bundle.json")
    policy_pin = _write_policy(tmp_path / "policy.json", decision=GrantDecision.DENIED)
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding_with_pins(
            tmp_path,
            bundle_pin=bundle_pin,
            policy_pin=policy_pin,
        ),
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "source_policy_not_usable"


@pytest.mark.parametrize(
    "config",
    [
        cast("FuyaoManualCompositionConfig", object()),
        FuyaoManualCompositionConfig(preflight_enabled=cast("bool", 1)),
    ],
)
def test_non_exact_config_or_toggle_type_fails_closed(
    config: FuyaoManualCompositionConfig,
) -> None:
    """A non-exact config or truthy non-bool toggle cannot opt in."""
    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config)

    assert error.value.code == "fuyao_manual_preflight_invalid"


def test_enabled_preflight_rejects_non_query_value(
    tmp_path: Path,
) -> None:
    """An enabled request must carry the exact bounded query type."""
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding(tmp_path),
        query=cast("FuyaoManualQuery", object()),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "fuyao_manual_preflight_invalid"


def test_corrupted_binding_is_revalidated_without_exposing_key(
    tmp_path: Path,
) -> None:
    """Replacing a frozen binding re-runs its grant identity constraints."""
    binding = _binding(tmp_path)
    object.__setattr__(binding, "source_attempt_expected_revision", 0)
    config = FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=binding,
        query=_query(),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(config, now=_NOW)

    assert error.value.code == "deployment_binding_invalid"
    assert _SYNTHETIC_KEY not in str(error.value)


@pytest.mark.parametrize("credential_result", ["missing", "raises"])
def test_credential_validation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_result: str,
) -> None:
    """Credential lookup failures are fixed-code errors before bundle I/O."""
    expected_code = "credential_missing" if credential_result == "missing" else "credential_invalid"

    def loader(**_kwargs: object) -> None:
        if credential_result == "raises":
            raise ValueError("synthetic credential rejection")

    monkeypatch.setattr(
        composition,
        "FuyaoCredentials",
        SimpleNamespace(from_settings=loader),
    )
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: pytest.fail("credential error must stop before bundle I/O"),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=_NOW)

    assert error.value.code == expected_code
    assert _SYNTHETIC_KEY not in str(error.value)


def test_invalid_bounded_query_is_rejected_after_preflight_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lower-level payload builder remains the query bounds authority."""
    _install_preflight_fakes(monkeypatch)
    query = replace(_query(), limit=0)

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path, query=query), now=_NOW)

    assert error.value.code == "manual_query_invalid"


def test_unreadable_policy_fails_with_content_free_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy filesystem errors are sanitized instead of leaking local paths."""
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id=_INSTRUMENT,
            bundle_sha256=_BUNDLE_PIN,
        ),
    )

    def fail_read(_path: str) -> bytes:
        raise OSError("synthetic local read failure")

    monkeypatch.setattr(composition, "_read_atomic_regular_file", fail_read)

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=_NOW)

    assert error.value.code == "source_policy_invalid"
    assert str(tmp_path) not in str(error.value)


def test_policy_without_matching_ths_query_scope_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correctly pinned policy document still needs exactly one THS query."""
    unrelated = _policy_document(canonical_model="other_model").policies[0]
    _install_preflight_fakes(
        monkeypatch,
        document=SourcePolicyDocument(version=1, policies=(unrelated,)),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=_NOW)

    assert error.value.code == "source_policy_scope_invalid"


def test_policy_for_unconfigured_host_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source policy scoped to another host cannot authorize this preflight."""
    _install_preflight_fakes(monkeypatch, document=_policy_document(host="invalid.example"))

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=_NOW)

    assert error.value.code == "source_policy_host_invalid"


def test_policy_context_integrity_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context construction errors do not escape with policy internals."""
    _install_preflight_fakes(monkeypatch)

    def fail_context(**_kwargs: object) -> None:
        raise ValueError("synthetic policy context failure")

    monkeypatch.setattr(composition, "build_source_policy_context", fail_context)

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=_NOW)

    assert error.value.code == "source_policy_invalid"
    assert "synthetic policy context failure" not in str(error.value)


@pytest.mark.parametrize(
    "current_time",
    [
        cast("datetime", object()),
        datetime(2026, 10, 29),
        datetime(2026, 10, 29, tzinfo=_NoOffsetTimezone()),
        datetime(2026, 10, 29, tzinfo=_BrokenTimezone()),
    ],
)
def test_invalid_preflight_time_fails_before_external_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current_time: datetime,
) -> None:
    """Bad, naive, absent-offset, or failing-offset times cannot reach I/O."""
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: pytest.fail("invalid time must stop before bundle I/O"),
    )

    with pytest.raises(FuyaoManualCompositionError) as error:
        prepare_fuyao_manual_preflight(_enabled_config(tmp_path), now=current_time)

    assert error.value.code == "preflight_time_invalid"
