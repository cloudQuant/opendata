"""Synthetic checks for the disabled Fuyao slice and fenced archive commit.

These tests use only temporary SQLite files and HTTPX MockTransport. The main
pipeline test monkeypatches ``run_fuyao`` to call the fixed handler in-process,
so it does not verify real subprocess startup, closed child environment, or
IPC. No test here enables/registers a production job, supplies real grant
evidence, creates durable prepared-capture intent, or establishes authenticated
attempt association. Raw artifacts can remain after a rolled-back SQL commit.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, select
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_ROOT = Path(__file__).resolve().parents[1]
_GATE_MIGRATION = _ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
_ARCHIVE_MIGRATION = (
    _ROOT / "alembic_financial_archive/versions/20261027-0001_financial_observation_archive.py"
)
_INSTRUMENT = "999999.BJ"
_SYNTHETIC_API_KEY = "synthetic-fuyao-test-key-do-not-use"
_GRANT_ID = "synthetic-financial-grant"
_PRINCIPAL_SCOPE = "synthetic-financial-worker"
_QUOTA_KEY = "synthetic-financial-quota"
_NOW = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def offline_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Keep imports and all runtime artifacts inside an empty synthetic directory."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _load_migration(path: Path, module_name: str) -> Any:
    """Load one checked-in migration module without consulting Alembic config."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(engine: Engine, path: Path, module_name: str) -> None:
    """Apply one migration to an explicitly test-owned SQLite database."""
    migration = _load_migration(path, module_name)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


def _gate_engine(
    path: Path,
    *,
    grant_decision: str = "ALLOWED",
    rate_microtokens_per_second: int = 1_000_000,
    capacity_microtokens: int = 5_000_000,
    tokens_micro: int = 5_000_000,
) -> Engine:
    """Create the test-only file-backed SourceAttemptGate database."""
    engine = sa.create_engine(
        f"sqlite:///{path}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    _migrate(engine, _GATE_MIGRATION, "fuyao_financial_job_gate_migration_test")
    _seed_gate(
        engine,
        grant_decision=grant_decision,
        rate_microtokens_per_second=rate_microtokens_per_second,
        capacity_microtokens=capacity_microtokens,
        tokens_micro=tokens_micro,
    )
    return engine


def _seed_gate(
    engine: Engine,
    *,
    grant_decision: str,
    rate_microtokens_per_second: int,
    capacity_microtokens: int,
    tokens_micro: int,
) -> None:
    """Insert only synthetic rate policy, grant and host records for this test."""
    from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL

    evidence_digest = hashlib.sha256(b"synthetic fixture only").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, :rate, :capacity, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": _QUOTA_KEY,
                "rate": rate_microtokens_per_second,
                "capacity": capacity_microtokens,
                "reference": "synthetic:test-policy-only",
                "digest": evidence_digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES ('ths', :quota, :reference, :digest, :actor, 1)"
            ),
            {
                "quota": _QUOTA_KEY,
                "reference": "synthetic:test-source-binding-only",
                "digest": evidence_digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, :tokens, 0, CASE WHEN :tokens < :capacity THEN "
                "CAST(ROUND((julianday('now') - 2440587.5) * 86400000000.0) AS INTEGER) "
                "ELSE 1 END, 1)"
            ),
            {
                "quota": _QUOTA_KEY,
                "tokens": tokens_micro,
                "capacity": capacity_microtokens,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, revoked_at_us, created_at_us, updated_at_us) "
                "VALUES (:grant, 'ths', 'financial_statement', 'query', :principal, "
                ":decision, 1, 1, :expires, '[]', :evidence, :digest, :note, "
                "'offline-test', 1, NULL, 1, 1)"
            ),
            {
                "grant": _GRANT_ID,
                "decision": grant_decision,
                "principal": _PRINCIPAL_SCOPE,
                "expires": 253_402_300_799_999_999,
                "evidence": "synthetic:test-only-not-rights-evidence",
                "digest": evidence_digest,
                "note": "Synthetic gate fixture; not authorization evidence.",
            },
        )
        if grant_decision == "ALLOWED":
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"
                ),
                {
                    "grant": _GRANT_ID,
                    "host": urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grant_host_sets "
                    "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                    "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-test-only')"
                ),
                {"grant": _GRANT_ID},
            )


def _registry_and_profiles() -> tuple[Any, dict[str, Any]]:
    """Build one synthetic active instrument and all three unit declarations."""
    from opendata.data.instrument_reference import (
        InstrumentLifecycle,
        InstrumentReferenceRegistry,
        LifecycleStatus,
    )
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
                status_as_of=date(2026, 10, 28),
                status_evidence_ref="synthetic:test-only-status",
                start_boundary_unknown=True,
            ),
        ),
    )
    profiles: dict[str, Any] = {}
    for statement in ("income", "balance", "cashflow"):
        native_units = dict.fromkeys(financial_statement_items(statement), "CNY")
        version = "synthetic-unit-profile-v1"
        profiles[statement] = ThsFinancialNativeUnitProfile(
            statement_type=statement,
            version=version,
            native_units=native_units,
            mapping_sha256_declared=canonical_native_unit_mapping_sha256(
                statement_type=statement,
                version=version,
                native_units=native_units,
            ),
            evidence_ref="synthetic:test-only-unit-declaration",
        )
    return registry, profiles


def _write_pinned_bundle(path: Path) -> str:
    """Write one exact synthetic bundle and return its independent content pin."""
    from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot

    registry, profiles = _registry_and_profiles()
    snapshot = json.loads(encode_instrument_reference_snapshot(registry).decode("utf-8"))
    document = {
        "schema_version": 1,
        "scope": {"instrument_id": _INSTRUMENT},
        "instrument_reference_snapshot": snapshot,
        "unit_profiles": {
            statement: {
                "statement_type": profile.statement_type,
                "version": profile.version,
                "native_units": dict(profile.native_units),
                "mapping_sha256_declared": profile.mapping_sha256_declared,
                "evidence_ref": profile.evidence_ref,
            }
            for statement, profile in profiles.items()
        },
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(encoded)
    path.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def _write_pinned_policy(path: Path) -> str:
    """Write one synthetic operator policy, clearly separate from legal proof."""
    from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
    from opendata.data.request_budget import GrantDecision, RequestOperation
    from opendata.data.source_policy import SourceUsePolicy

    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    policy = SourceUsePolicy(
        policy_id="synthetic-fuyao-policy",
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        principal_user_id=17,
        product="synthetic offline test",
        purpose="exercise the reviewed local pipeline",
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic fixture only; not rights evidence",
        allowed_hosts=[host],
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        conditions=[],
        task_attempts=4,
        source_attempts=1,
    )
    encoded = json.dumps(
        {"version": 1, "policies": [policy.model_dump(mode="json")]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(encoded)
    path.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def _binding_document(binding: Any) -> str:
    """Encode the exact nonsecret child deployment fields used by schema v1."""
    return json.dumps(
        {
            "schema_version": 1,
            "financial_bundle_path": binding.financial_bundle_path,
            "financial_bundle_sha256": binding.financial_bundle_sha256,
            "source_policy_path": binding.source_policy_path,
            "source_policy_sha256": binding.source_policy_sha256,
            "gate_database_path": binding.gate_database_path,
            "source_attempt_grant_id": binding.source_attempt_grant_id,
            "source_attempt_expected_revision": binding.source_attempt_expected_revision,
            "source_attempt_principal_scope": binding.source_attempt_principal_scope,
            "raw_artifact_root": binding.raw_artifact_root,
            "retention_class": binding.retention_class,
            "source_history_limit": binding.source_history_limit,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _synthetic_binding(
    root: Path,
    *,
    grant_decision: str = "ALLOWED",
    rate_microtokens_per_second: int = 1_000_000,
    capacity_microtokens: int = 5_000_000,
    tokens_micro: int = 5_000_000,
) -> tuple[Engine, Any]:
    """Create only pinned synthetic files and a test-owned gate binding."""
    from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding

    gate_path = root / "source-gate.sqlite"
    gate_engine = _gate_engine(
        gate_path,
        grant_decision=grant_decision,
        rate_microtokens_per_second=rate_microtokens_per_second,
        capacity_microtokens=capacity_microtokens,
        tokens_micro=tokens_micro,
    )
    bundle_path = root / "financial-bundle.json"
    bundle_sha256 = _write_pinned_bundle(bundle_path)
    policy_path = root / "source-policy.json"
    policy_sha256 = _write_pinned_policy(policy_path)
    binding = FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(bundle_path),
        financial_bundle_sha256=bundle_sha256,
        source_policy_path=str(policy_path),
        source_policy_sha256=policy_sha256,
        gate_database_path=str(gate_path),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL_SCOPE,
        raw_artifact_root=str(root / "raw-evidence"),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    return gate_engine, binding


def _install_mock_process_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    response: bytes,
) -> list[httpx.Request]:
    """Install a MockTransport-backed fixed fetcher and return sent requests."""
    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        ThsFinancialObservationFetcher,
    )
    from opendata.pipeline import fuyao_financial_process as process_module

    sent_requests: list[httpx.Request] = []

    def mock_response(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(200, content=response)

    mock_transport = httpx.MockTransport(mock_response)

    def build_mock_fetcher(
        *,
        credentials: Any,
        source_attempt_gate: Any,
        bundle: Any,
    ) -> ThsFinancialObservationFetcher:
        return ThsFinancialObservationFetcher(
            credentials=credentials,
            source_attempt_gate=source_attempt_gate,
            instrument_registry=bundle.instrument_registry,
            unit_profiles=bundle.unit_profiles,
            mock_transport=mock_transport,
        )

    monkeypatch.setattr(process_module, "_build_fetcher", build_mock_fetcher)
    return sent_requests


def _response_body(
    statement_type: str = "income",
    periodicity: str = "annual",
) -> bytes:
    """Return one synthetic row for a chosen financial statement and cadence."""
    from opendata.data.providers.ths.endpoints import (
        financial_statement_items,
        shanghai_midnight_millis,
    )

    items = financial_statement_items(statement_type)
    annual = periodicity == "annual"
    report_period = date(2024, 12, 31) if annual else date(2024, 3, 31)
    fiscal_period = "FY" if annual else "Q1"
    disclosure_date = date(2025, 3, 31) if annual else date(2024, 4, 30)
    period_end = shanghai_midnight_millis(report_period)
    disclosure = shanghai_midnight_millis(disclosure_date)
    row: dict[str, object] = {
        "thscode": _INSTRUMENT,
        "ticker": "999999",
        "period": periodicity,
        "fiscal_year": 2024,
        "fiscal_period": fiscal_period,
        "period_end_ms": period_end,
        "report_date_ms": disclosure,
        "currency": "CNY",
    }
    row.update(dict.fromkeys(items, "0"))
    row[items[0]] = "123.45"
    return json.dumps(
        {
            "code": 0,
            "message": "ok",
            "request_id": "synthetic-fuyao-request-001",
            "data": {"timestamp": period_end, "item": [row]},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_process_parameters(**overrides: object) -> dict[str, object]:
    """Return one valid fixed child query with optional synthetic overrides."""
    parameters: dict[str, object] = {
        "instrument_id": _INSTRUMENT,
        "statement_type": "income",
        "periodicity": "annual",
        "start_date": None,
        "end_date": None,
        "limit": 1,
        "run_key": "e" * 64,
    }
    parameters.update(overrides)
    return parameters


def test_fuyao_job_is_disabled_and_manual_windows_are_bounded(
    offline_runtime: Path,
) -> None:
    """The factory returns a non-runnable job and payload helpers reject wide windows."""
    from opendata.pipeline.fuyao_financial_run_job import (
        _RESULT_FIELDS,
        build_fuyao_financial_backfill_payload,
        build_fuyao_financial_job,
        build_fuyao_financial_manual_payload,
    )
    from opendata.pipeline.process_executor import (
        _FUYAO_RESULT_FIELDS,
        FuyaoProcessDeploymentBinding,
    )
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler
    from opendata.pipeline.run_identity import TaskDefinition

    binding = FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(offline_runtime / "bundle.json"),
        financial_bundle_sha256="a" * 64,
        source_policy_path=str(offline_runtime / "policy.json"),
        source_policy_sha256="b" * 64,
        gate_database_path=str(offline_runtime / "gate.sqlite"),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL_SCOPE,
        raw_artifact_root=str(offline_runtime / "raw"),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )
    job = build_fuyao_financial_job(task, deployment_binding=binding)

    assert job.reviewed is False
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert isinstance(job.fetch, ProcessFetchHandler)
    assert job.commit_payload_type is bytes
    assert job.validate_commit_payload is None
    assert job.max_commit_payload_bytes == 5 * 1024 * 1024
    assert "synthetic-fuyao-test-key" not in repr(binding)
    assert _RESULT_FIELDS == _FUYAO_RESULT_FIELDS
    assert len(_RESULT_FIELDS) == 20

    manual = build_fuyao_financial_manual_payload(
        _INSTRUMENT,
        statement_type="income",
        periodicity="annual",
        limit=1,
        idempotency_key="synthetic-manual-001",
    )
    backfill = build_fuyao_financial_backfill_payload(
        _INSTRUMENT,
        statement_type="income",
        periodicity="annual",
        start_date="2020-01-01",
        end_date="2026-10-28",
        idempotency_key="synthetic-backfill-001",
    )
    assert manual["partition"] == {"instrument_id": _INSTRUMENT}
    assert backfill["backfill_start"] == "2020-01-01"
    assert "source_policy" not in manual
    assert "source_attempt_grant_id" not in backfill
    max_window = build_fuyao_financial_backfill_payload(
        _INSTRUMENT,
        statement_type="income",
        periodicity="annual",
        start_date="2016-10-30",
        end_date="2026-10-28",
        idempotency_key="synthetic-exact-window",
    )
    assert max_window["backfill_start"] == "2016-10-30"
    with pytest.raises(ValueError):
        build_fuyao_financial_backfill_payload(
            _INSTRUMENT,
            statement_type="income",
            periodicity="annual",
            start_date="2016-10-29",
            end_date="2026-10-28",
            idempotency_key="test-window",
        )


def test_child_window_uses_exact_3650_day_endpoint_limit() -> None:
    """Child validation accepts 3,650 elapsed days and refuses 3,651."""
    from opendata.pipeline.fuyao_financial_process import (
        FuyaoFinancialProcessError,
        _validate_parameters,
    )

    parameters: dict[str, object] = {
        "instrument_id": _INSTRUMENT,
        "statement_type": "income",
        "periodicity": "annual",
        "start_date": "2016-10-30",
        "end_date": "2026-10-28",
        "limit": None,
        "run_key": "a" * 64,
    }
    assert _validate_parameters(parameters).start_date == date(2016, 10, 30)
    parameters["start_date"] = "2016-10-29"
    with pytest.raises(FuyaoFinancialProcessError) as error:
        _validate_parameters(parameters)
    assert str(error.value) == "fuyao_financial_process_failed"


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        pytest.param("shape", "fuyao_financial_process_failed", id="closed-shape"),
        pytest.param("instrument", "fuyao_financial_process_failed", id="instrument"),
        pytest.param("statement", "fuyao_financial_process_failed", id="statement"),
        pytest.param("periodicity", "fuyao_financial_process_failed", id="periodicity"),
        pytest.param("run-key", "fuyao_financial_process_failed", id="run-key"),
        pytest.param("unpaired-date", "fuyao_financial_process_failed", id="unpaired-date"),
        pytest.param("bool-limit", "fuyao_financial_process_failed", id="bool-is-not-int"),
        pytest.param("limit-low", "fuyao_financial_process_failed", id="limit-low"),
        pytest.param("limit-high", "fuyao_financial_process_failed", id="limit-high"),
        pytest.param("date-format", "fuyao_financial_process_failed", id="date-format"),
        pytest.param("invalid-date", "fuyao_financial_process_failed", id="invalid-date"),
        pytest.param("dates-and-limit", "fuyao_financial_process_failed", id="dates-and-limit"),
        pytest.param("reversed-window", "fuyao_financial_process_failed", id="reversed-window"),
        pytest.param("wide-window", "fuyao_financial_process_failed", id="wide-window"),
    ],
)
def test_child_revalidates_closed_request_before_reading_deployment(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_error: str,
) -> None:
    """Malformed IPC queries fail with one fixed code before configuration lookup."""
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
    )

    monkeypatch.delenv(FUYAO_PROCESS_API_KEY_ENV, raising=False)
    monkeypatch.delenv(FUYAO_PROCESS_BINDING_ENV, raising=False)
    parameters: object = _valid_process_parameters()
    if case == "shape":
        parameters = {
            key: value for key, value in _valid_process_parameters().items() if key != "limit"
        }
    else:
        assert isinstance(parameters, dict)
        mutations: dict[str, dict[str, object]] = {
            "instrument": {"instrument_id": "600000"},
            "statement": {"statement_type": "profit"},
            "periodicity": {"periodicity": "monthly"},
            "run-key": {"run_key": "not-a-run-key"},
            "unpaired-date": {"start_date": "2020-01-01"},
            "bool-limit": {"limit": True},
            "limit-low": {"limit": 0},
            "limit-high": {"limit": 21},
            "date-format": {"start_date": "2020/01/01", "end_date": "2020-01-02", "limit": None},
            "invalid-date": {"start_date": "2020-02-30", "end_date": "2020-03-01", "limit": None},
            "dates-and-limit": {
                "start_date": "2020-01-01",
                "end_date": "2020-01-02",
                "limit": 1,
            },
            "reversed-window": {
                "start_date": "2020-01-02",
                "end_date": "2020-01-01",
                "limit": None,
            },
            "wide-window": {"start_date": "2010-01-01", "end_date": "2020-01-01", "limit": None},
        }
        parameters.update(mutations[case])

    with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
        process_module.execute_fuyao_financial_request(parameters)
    assert str(error.value) == expected_error
    assert "synthetic-fuyao-test-key" not in repr(error.value)


def test_valid_child_request_requires_explicit_closed_deployment_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid business query cannot fall back to ambient process settings."""
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
    )

    monkeypatch.delenv(FUYAO_PROCESS_API_KEY_ENV, raising=False)
    monkeypatch.delenv(FUYAO_PROCESS_BINDING_ENV, raising=False)
    with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
        process_module.execute_fuyao_financial_request(_valid_process_parameters())
    assert str(error.value) == "fuyao_financial_process_failed"


def test_bundle_instrument_mismatch_fails_before_gate_or_transport(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-instrument pin cannot be widened by a request parameter."""
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
    )

    gate_engine, binding = _synthetic_binding(offline_runtime)
    try:
        monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, binding.api_key)
        monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, _binding_document(binding))
        parameters = _valid_process_parameters(instrument_id="600000.SH")
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module.execute_fuyao_financial_request(parameters)
        assert str(error.value) == "fuyao_financial_process_failed"
        assert not Path(binding.raw_artifact_root).exists()
        with gate_engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT COUNT(*) FROM od_source_attempt_reservations"))
                == 0
            )
    finally:
        gate_engine.dispose()


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param("decision", id="decision"),
        pytest.param("conditions", id="conditions"),
        pytest.param("allowed_hosts", id="host"),
    ],
)
def test_child_rejects_operator_policy_that_is_not_exactly_allowed(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    """A pinned policy must allow the fixed origin without conditions."""
    from opendata.data.request_budget import GrantDecision
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
    )

    gate_engine, binding = _synthetic_binding(offline_runtime)
    try:
        source_policy, policy = process_module._load_exact_policy(binding)
        updates: dict[str, object] = {
            "decision": GrantDecision.DENIED,
            "conditions": ("synthetic-restriction",),
            "allowed_hosts": (),
        }
        test_policy = policy.model_copy(update={refusal: updates[refusal]})
        monkeypatch.setattr(
            process_module,
            "_load_exact_policy",
            lambda _binding: (source_policy, test_policy),
        )
        monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, binding.api_key)
        monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, _binding_document(binding))
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module.execute_fuyao_financial_request(_valid_process_parameters())
        assert str(error.value) == "fuyao_financial_process_failed"
        assert not Path(binding.raw_artifact_root).exists()
        with gate_engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT COUNT(*) FROM od_source_attempt_reservations"))
                == 0
            )
    finally:
        gate_engine.dispose()


@pytest.mark.parametrize(
    "failure",
    ["wrong-pin", "missing-file", "malformed-document", "no-matching-policy"],
)
def test_policy_loader_rejects_unpinned_or_nonmatching_policy(
    offline_runtime: Path,
    failure: str,
) -> None:
    """Only one exact file whose independent SHA matches can supply the policy."""
    from opendata.pipeline import fuyao_financial_process as process_module

    gate_engine, binding = _synthetic_binding(offline_runtime)
    policy_path = Path(binding.source_policy_path)
    try:
        if failure == "wrong-pin":
            tested_binding = replace(binding, source_policy_sha256="f" * 64)
        elif failure == "missing-file":
            tested_binding = replace(
                binding,
                source_policy_path=str(offline_runtime / "missing-policy.json"),
            )
        elif failure == "malformed-document":
            malformed = b"not-json"
            policy_path.write_bytes(malformed)
            tested_binding = replace(
                binding,
                source_policy_sha256=hashlib.sha256(malformed).hexdigest(),
            )
        else:
            document = json.loads(policy_path.read_text(encoding="utf-8"))
            document["policies"][0]["source"] = "other-provider"
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            policy_path.write_bytes(encoded)
            tested_binding = replace(
                binding,
                source_policy_sha256=hashlib.sha256(encoded).hexdigest(),
            )
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module._load_exact_policy(tested_binding)
        assert str(error.value) == "fuyao_financial_process_failed"
        assert str(policy_path) not in repr(error.value)
    finally:
        gate_engine.dispose()


@pytest.mark.parametrize(
    "path_kind",
    ["relative", "missing", "directory", "symlink", "header", "valid"],
)
def test_gate_file_validation_rejects_unsafe_paths_and_accepts_sqlite_header(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """The child opens only an existing regular, non-symlink SQLite file path."""
    from opendata.pipeline import fuyao_financial_process as process_module

    valid_header_file = tmp_path / "valid-header.sqlite"
    valid_header_file.write_bytes(b"SQLite format 3\x00" + bytes(32))
    if path_kind == "relative":
        path = "relative/gate.sqlite"
    elif path_kind == "missing":
        path = str(tmp_path / "missing.sqlite")
    elif path_kind == "directory":
        path = str(tmp_path)
    elif path_kind == "symlink":
        link = tmp_path / "gate-link.sqlite"
        link.symlink_to(valid_header_file)
        path = str(link)
    elif path_kind == "header":
        malformed = tmp_path / "not-sqlite.sqlite"
        malformed.write_bytes(b"not a sqlite database at all")
        path = str(malformed)
    else:
        path = str(valid_header_file)

    if path_kind == "valid":
        process_module._validate_existing_gate_database(path)
    else:
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module._validate_existing_gate_database(path)
        assert str(error.value) == "fuyao_financial_process_failed"
        assert path not in repr(error.value)


def test_job_payload_and_run_request_validators_reject_scope_drift(
    offline_runtime: Path,
) -> None:
    """Manual/backfill requests bind one instrument, exact fields, dates and run key."""
    from opendata.pipeline.fuyao_financial_run_job import (
        _build_process_parameters,
        _request_payload,
        build_fuyao_financial_backfill_payload,
        build_fuyao_financial_manual_payload,
    )
    from opendata.pipeline.run_identity import RunRequest

    manual_payload = build_fuyao_financial_manual_payload(
        _INSTRUMENT,
        statement_type="income",
        periodicity="annual",
        limit=1,
        idempotency_key="synthetic-job-request-001",
    )
    manual = RunRequest(run_type="manual", **manual_payload)
    parameters = _build_process_parameters(manual, "a" * 64)
    assert set(parameters) == {
        "instrument_id",
        "statement_type",
        "periodicity",
        "start_date",
        "end_date",
        "limit",
        "run_key",
    }
    assert parameters["instrument_id"] == _INSTRUMENT
    assert parameters["run_key"] == "a" * 64

    backfill_payload = build_fuyao_financial_backfill_payload(
        _INSTRUMENT,
        statement_type="cashflow",
        periodicity="quarterly",
        start_date="2020-01-01",
        end_date="2020-01-02",
        idempotency_key="synthetic-job-backfill-001",
    )
    backfill = RunRequest(
        run_type="backfill",
        query=backfill_payload["query"],
        partition=backfill_payload["partition"],
        shard=backfill_payload["shard"],
        idempotency_key=backfill_payload["idempotency_key"],
        backfill_start=date.fromisoformat(backfill_payload["backfill_start"]),
        backfill_end=date.fromisoformat(backfill_payload["backfill_end"]),
    )
    backfill_parameters = _build_process_parameters(backfill, "b" * 64)
    assert backfill_parameters["start_date"] == "2020-01-01"
    assert backfill_parameters["end_date"] == "2020-01-02"
    assert "backfill_start" not in _request_payload(manual)
    assert _request_payload(backfill)["backfill_start"] == "2020-01-01"

    no_key_scheduled = RunRequest(
        run_type="scheduled",
        query=manual.query,
        partition=manual.partition,
        occurrence=_NOW,
    )
    assert "idempotency_key" not in _request_payload(no_key_scheduled)

    invalid_requests: list[tuple[RunRequest, object]] = [
        (
            RunRequest(
                run_type="manual",
                query=manual.query,
                partition={"instrument_id": _INSTRUMENT, "extra": "unexpected"},
                idempotency_key="synthetic-invalid-extra-partition",
            ),
            "c" * 64,
        ),
        (
            RunRequest(
                run_type="manual",
                query=manual.query,
                partition=manual.partition,
                shard="child-1",
                idempotency_key="synthetic-invalid-shard",
            ),
            "d" * 64,
        ),
        (
            RunRequest(
                run_type="manual",
                query={**manual.query, "unexpected": "field"},
                partition=manual.partition,
                idempotency_key="synthetic-invalid-query",
            ),
            "e" * 64,
        ),
        (manual, "bad-run-key"),
        (
            RunRequest(
                run_type="backfill",
                query=backfill.query,
                partition=backfill.partition,
                idempotency_key="synthetic-mismatched-backfill",
                backfill_start=date(2020, 1, 2),
                backfill_end=date(2020, 1, 2),
            ),
            "f" * 64,
        ),
    ]
    for request, run_key in invalid_requests:
        with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
            _build_process_parameters(request, run_key)
    with pytest.raises(TypeError, match="request must be an exact RunRequest"):
        _build_process_parameters(object(), "e" * 64)

    with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
        build_fuyao_financial_backfill_payload(
            _INSTRUMENT,
            statement_type="income",
            periodicity="annual",
            start_date="2020-01-01",
            end_date="2020-01-02",
            idempotency_key=object(),
        )
    with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
        build_fuyao_financial_manual_payload(
            "600000",
            statement_type="income",
            periodicity="annual",
            limit=1,
            idempotency_key="synthetic-invalid-instrument",
        )
    with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
        build_fuyao_financial_manual_payload(
            _INSTRUMENT,
            statement_type="income",
            periodicity="annual",
            limit=1,
            idempotency_key=" ",
        )


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            {"statement_type": "profit", "periodicity": "annual", "limit": 1},
            id="statement-token",
        ),
        pytest.param(
            {"statement_type": "income", "periodicity": "monthly", "limit": 1},
            id="periodicity-token",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2020-01-01",
                "limit": None,
            },
            id="unpaired-date",
        ),
        pytest.param(
            {"statement_type": "income", "periodicity": "annual", "limit": True},
            id="bool-limit",
        ),
        pytest.param(
            {"statement_type": "income", "periodicity": "annual", "limit": 0},
            id="empty-limit",
        ),
        pytest.param(
            {"statement_type": "income", "periodicity": "annual", "limit": 21},
            id="oversized-limit",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2020/01/01",
                "end_date": "2020-01-02",
                "limit": None,
            },
            id="date-format",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2020-02-30",
                "end_date": "2020-03-01",
                "limit": None,
            },
            id="invalid-calendar-date",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2020-01-02",
                "end_date": "2020-01-01",
                "limit": None,
            },
            id="reversed-window",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2010-01-01",
                "end_date": "2020-01-01",
                "limit": None,
            },
            id="overlong-window",
        ),
        pytest.param(
            {
                "statement_type": "income",
                "periodicity": "annual",
                "start_date": "2020-01-01",
                "end_date": "2020-01-02",
                "limit": 1,
            },
            id="date-window-with-limit",
        ),
    ],
)
def test_public_query_builder_rejects_invalid_bounds(
    query: dict[str, object],
) -> None:
    """Public manual construction applies the same closed query constraints."""
    from opendata.pipeline.fuyao_financial_run_job import (
        build_fuyao_financial_manual_payload,
    )

    with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
        build_fuyao_financial_manual_payload(
            _INSTRUMENT,
            statement_type=query.get("statement_type"),
            periodicity=query.get("periodicity"),
            start_date=query.get("start_date"),
            end_date=query.get("end_date"),
            limit=query.get("limit"),
            idempotency_key="synthetic-invalid-query",
        )


def test_run_request_payload_rejects_incomplete_backfill_snapshot() -> None:
    """A backfill dispatcher payload cannot omit its persisted date range."""
    from opendata.pipeline.fuyao_financial_run_job import _request_payload
    from opendata.pipeline.run_identity import RunRequest

    malformed = RunRequest.model_construct(
        run_type="backfill",
        query={},
        partition={},
        shard=None,
        idempotency_key=None,
        backfill_start=None,
        backfill_end=None,
    )
    with pytest.raises(ValueError, match="fuyao_financial_process_request_invalid"):
        _request_payload(malformed)


def test_job_factory_and_process_fetch_handler_remain_fail_closed(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job stays disabled and the handler forwards only a server-owned key."""
    from opendata.pipeline.fuyao_financial_run_job import (
        FuyaoFinancialProcessFetchHandler,
        build_fuyao_financial_job,
        build_fuyao_financial_manual_payload,
    )
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_OPERATION_ID,
        FuyaoProcessDeploymentBinding,
        ReviewedProcessExecutor,
    )
    from opendata.pipeline.run_identity import RunRequest, TaskDefinition
    from opendata.pipeline.run_service import RunExecutionContext

    binding = FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(offline_runtime / "bundle.json"),
        financial_bundle_sha256="a" * 64,
        source_policy_path=str(offline_runtime / "policy.json"),
        source_policy_sha256="b" * 64,
        gate_database_path=str(offline_runtime / "gate.sqlite"),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL_SCOPE,
        raw_artifact_root=str(offline_runtime / "raw"),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )
    with pytest.raises(TypeError, match="executor must be an exact ReviewedProcessExecutor"):
        FuyaoFinancialProcessFetchHandler(object(), binding)
    job = build_fuyao_financial_job(task, deployment_binding=binding)
    request = RunRequest(
        run_type="manual",
        **build_fuyao_financial_manual_payload(
            _INSTRUMENT,
            statement_type="income",
            periodicity="annual",
            limit=1,
            idempotency_key="synthetic-handler-run",
        ),
    )
    with pytest.raises(TypeError, match="context must be an exact RunExecutionContext"):
        job.fetch(request, object())

    checkpoint_calls = 0
    seen: dict[str, object] = {}

    def checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1

    def return_wrong_process_result(
        _executor: ReviewedProcessExecutor,
        received_binding: FuyaoProcessDeploymentBinding,
        parameters: object,
        *,
        timeout_seconds: float,
        checkpoint: Any = None,
        cancelled: Any = None,
    ) -> object:
        seen["binding_api_key"] = received_binding.api_key
        seen["parameters"] = parameters
        seen["timeout_seconds"] = timeout_seconds
        assert cancelled is None
        assert checkpoint is not None
        checkpoint()
        return object()

    monkeypatch.setattr(ReviewedProcessExecutor, "run_fuyao", return_wrong_process_result)
    context = RunExecutionContext(
        run_key="c" * 64,
        worker="synthetic-handler-test",
        fencing_token=1,
        _checkpoint=checkpoint,
    )
    with pytest.raises(ValueError, match="fuyao_financial_process_result_invalid"):
        job.fetch(request, context)
    assert checkpoint_calls >= 2
    assert seen["binding_api_key"] == _SYNTHETIC_API_KEY
    assert seen["timeout_seconds"] > 0
    received_parameters = seen["parameters"]
    assert isinstance(received_parameters, dict)
    assert set(received_parameters) == {
        "instrument_id",
        "statement_type",
        "periodicity",
        "start_date",
        "end_date",
        "limit",
        "run_key",
    }
    assert received_parameters["run_key"] == "c" * 64
    assert FUYAO_PROCESS_OPERATION_ID == "fuyao-financial-observation-v1"


@pytest.mark.parametrize("invalid_field", ["identity", "enabled", "scheduled", "timezone"])
def test_job_factory_refuses_changed_or_runnable_task_metadata(
    offline_runtime: Path,
    invalid_field: str,
) -> None:
    """The factory accepts only the exact disabled, unscheduled fixed task."""
    from opendata.pipeline.fuyao_financial_run_job import build_fuyao_financial_job
    from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding
    from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition

    binding = FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(offline_runtime / "bundle.json"),
        financial_bundle_sha256="a" * 64,
        source_policy_path=str(offline_runtime / "policy.json"),
        source_policy_sha256="b" * 64,
        gate_database_path=str(offline_runtime / "gate.sqlite"),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL_SCOPE,
        raw_artifact_root=str(offline_runtime / "raw"),
        retention_class="restricted",
        source_history_limit="unknown",
    )
    task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )
    if invalid_field == "identity":
        invalid_task = task.model_copy(update={"dataset_id": "other-dataset"})
    elif invalid_field == "enabled":
        invalid_task = task.model_copy(update={"enabled": True})
    elif invalid_field == "scheduled":
        invalid_task = task.model_copy(
            update={
                "schedule": ScheduleMetadata(kind="interval", expression="PT1H"),
            }
        )
    else:
        invalid_task = task.model_copy(update={"timezone": "Not/A_Real_Zone"})

    with pytest.raises(ValueError, match="fuyao_financial_task_definition_invalid"):
        build_fuyao_financial_job(invalid_task, deployment_binding=binding)

    with pytest.raises(TypeError, match="task must be an exact TaskDefinition"):
        build_fuyao_financial_job(object(), deployment_binding=binding)
    with pytest.raises(TypeError, match="binding must be an exact FuyaoProcessDeploymentBinding"):
        build_fuyao_financial_job(task, deployment_binding=object())

    invalid_binding = replace(binding)
    object.__setattr__(invalid_binding, "source_policy_sha256", "bad-pin")
    with pytest.raises(ValueError, match="Fuyao deployment binding is invalid"):
        build_fuyao_financial_job(task, deployment_binding=invalid_binding)


@pytest.mark.parametrize(
    "trace",
    [
        (),
        (SimpleNamespace(gate_status="DENIED"),),
        (
            SimpleNamespace(
                gate_status="RESERVED_NEW",
                request_budget_reserved=False,
                reserved_at_us=1,
            ),
        ),
        (
            SimpleNamespace(
                gate_status="RESERVED_NEW",
                request_budget_reserved=True,
                reserved_at_us=None,
            ),
        ),
    ],
)
def test_capture_preparation_requires_a_successfully_reserved_local_attempt(
    offline_runtime: Path,
    trace: tuple[object, ...],
) -> None:
    """A response without a verified local reservation cannot create raw evidence."""
    from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
    from opendata.pipeline import fuyao_financial_process as process_module

    gate_engine, binding = _synthetic_binding(offline_runtime)
    try:
        fetch_result = SimpleNamespace(raw_body=b"safe synthetic response", attempt_trace=trace)
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module._prepare_capture(
                fetch_result,
                bundle=None,
                binding=binding,
                credentials=FuyaoCredentials(binding.api_key),
                run_key="f" * 64,
            )
        assert str(error.value) == "fuyao_financial_process_failed"
        assert not Path(binding.raw_artifact_root).exists()
    finally:
        gate_engine.dispose()


@pytest.mark.parametrize(
    ("grant_decision", "rate", "capacity", "tokens", "expected_denial"),
    [
        pytest.param("DENIED", 1_000_000, 5_000_000, 5_000_000, "grant_not_allowed", id="grant"),
        pytest.param(
            "ALLOWED",
            1,
            1_000_000,
            0,
            "source_rate_limited",
            id="capacity",
        ),
    ],
)
def test_gate_denial_and_exhausted_capacity_fail_before_transport(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    grant_decision: str,
    rate: int,
    capacity: int,
    tokens: int,
    expected_denial: str,
) -> None:
    """A denied or empty synthetic gate never sends HTTP or writes raw evidence."""
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
    )

    gate_engine, binding = _synthetic_binding(
        offline_runtime,
        grant_decision=grant_decision,
        rate_microtokens_per_second=rate,
        capacity_microtokens=capacity,
        tokens_micro=tokens,
    )
    try:
        sent_requests = _install_mock_process_fetcher(monkeypatch, _response_body())
        monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, binding.api_key)
        monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, _binding_document(binding))
        parameters: dict[str, object] = {
            "instrument_id": _INSTRUMENT,
            "statement_type": "income",
            "periodicity": "annual",
            "start_date": None,
            "end_date": None,
            "limit": 1,
            "run_key": "c" * 64,
        }
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module.execute_fuyao_financial_request(parameters)

        assert str(error.value) == "fuyao_financial_process_failed"
        assert binding.api_key not in str(error.value)
        assert not sent_requests
        assert not Path(binding.raw_artifact_root).exists()
        with gate_engine.connect() as connection:
            attempt = (
                connection.execute(
                    sa.text(
                        "SELECT outcome, denial_code FROM od_source_attempt_reservations "
                        "ORDER BY decided_at_us DESC LIMIT 1"
                    )
                )
                .mappings()
                .one()
            )
        assert attempt["outcome"] == "DENIED"
        assert attempt["denial_code"] == expected_denial
    finally:
        gate_engine.dispose()


@pytest.mark.parametrize("surface", ["raw_body", "prepared_capture", "result_json"])
@pytest.mark.parametrize(
    "encoding",
    [
        "literal",
        "base64",
        "base64_unpadded",
        "urlsafe_base64",
        "urlsafe_base64_unpadded",
        "hex",
        "hex_upper",
    ],
)
def test_secret_echo_encodings_are_rejected_before_raw_or_result_output(
    offline_runtime: Path,
    surface: str,
    encoding: str,
) -> None:
    """Literal, Base64 and hex echoes fail with only the fixed error code."""
    from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding

    secret = "synthetic\U0010ffff"
    secret_bytes = secret.encode("utf-8")
    forms = {
        "literal": secret_bytes,
        "base64": base64.b64encode(secret_bytes),
        "base64_unpadded": base64.b64encode(secret_bytes).rstrip(b"="),
        "urlsafe_base64": base64.urlsafe_b64encode(secret_bytes),
        "urlsafe_base64_unpadded": base64.urlsafe_b64encode(secret_bytes).rstrip(b"="),
        "hex": secret_bytes.hex().encode("ascii"),
        "hex_upper": secret_bytes.hex().upper().encode("ascii"),
    }
    assert forms["base64"] != forms["urlsafe_base64"]
    echoed = forms[encoding]
    if surface == "raw_body":
        binding = FuyaoProcessDeploymentBinding(
            api_key=secret,
            financial_bundle_path=str(offline_runtime / "bundle.json"),
            financial_bundle_sha256="a" * 64,
            source_policy_path=str(offline_runtime / "policy.json"),
            source_policy_sha256="b" * 64,
            gate_database_path=str(offline_runtime / "gate.sqlite"),
            source_attempt_grant_id=_GRANT_ID,
            source_attempt_expected_revision=1,
            source_attempt_principal_scope=_PRINCIPAL_SCOPE,
            raw_artifact_root=str(offline_runtime / "raw-echo"),
            retention_class="restricted",
            source_history_limit="unknown",
        )
        payload = SimpleNamespace(raw_body=b'{"echo":"' + echoed + b'"}')
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module._prepare_capture(
                payload,
                bundle=None,
                binding=binding,
                credentials=FuyaoCredentials(secret),
                run_key="d" * 64,
            )
        assert not Path(binding.raw_artifact_root).exists()
    else:
        if surface == "result_json":
            echoed_text = echoed.decode("utf-8", errors="strict")
            payload_bytes = json.dumps(
                {"prepared_capture_b64": echoed_text},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            payload_bytes = b'{"prepared_capture":"' + echoed + b'"}'
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module._reject_secret_echo(payload_bytes, secret)

    assert str(error.value) == "fuyao_financial_process_failed"
    error_bytes = repr(error.value).encode("utf-8")
    assert secret_bytes not in error_bytes
    assert all(encoded_secret not in error_bytes for encoded_secret in forms.values())


@pytest.mark.parametrize(
    ("statement_type", "periodicity", "fail_after_publish"),
    [
        pytest.param("income", "annual", False, id="income-annual"),
        pytest.param("income", "quarterly", False, id="income-quarterly"),
        pytest.param("balance", "annual", False, id="balance-annual"),
        pytest.param("balance", "quarterly", False, id="balance-quarterly"),
        pytest.param("cashflow", "annual", False, id="cashflow-annual"),
        pytest.param("cashflow", "quarterly", False, id="cashflow-quarterly"),
        pytest.param("income", "annual", True, id="rollback-after-store-write"),
    ],
)
def test_mocked_fuyao_capture_commits_or_rolls_back_by_report_family(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    statement_type: str,
    periodicity: str,
    fail_after_publish: bool,
) -> None:
    """MockTransport and SQLite cover statements, cadences, and SQL rollback.

    The executor method is monkeypatched to invoke the fixed handler in-process,
    so this test does not exercise subprocess environment isolation or IPC.
    The rollback case deliberately raises after the archive publisher inserted
    rows; the same-connection RunLedger transaction must remove those writes,
    while the already-written raw evidence remains as an orphaned artifact.
    """
    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        ThsFinancialObservationFetcher,
    )
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.financial_observation_store import CAPTURES, OBSERVATIONS
    from opendata.pipeline.fuyao_financial_run_job import (
        _build_process_parameters,
        _decode_process_result,
        build_fuyao_financial_job,
        build_fuyao_financial_manual_payload,
        normalize_fuyao_financial_capture,
        publish_prepared_fuyao_financial_capture,
    )
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
        FuyaoProcessDeploymentBinding,
        ProcessExecutionResult,
        ReviewedProcessExecutor,
    )
    from opendata.pipeline.run_identity import (
        RunRequest,
        ScheduleMetadata,
        TaskDefinition,
        build_run_identity,
    )
    from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
    from opendata.pipeline.run_service import (
        NormalizedRecord,
        ReviewedJobRegistry,
        RunExecutionContext,
        RunService,
    )

    gate_path = offline_runtime / "source-gate.sqlite"
    gate_engine = _gate_engine(gate_path)
    bundle_path = offline_runtime / "financial-bundle.json"
    bundle_sha256 = _write_pinned_bundle(bundle_path)
    policy_path = offline_runtime / "source-policy.json"
    policy_sha256 = _write_pinned_policy(policy_path)
    raw_root = offline_runtime / "raw-evidence"
    binding = FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(bundle_path),
        financial_bundle_sha256=bundle_sha256,
        source_policy_path=str(policy_path),
        source_policy_sha256=policy_sha256,
        gate_database_path=str(gate_path),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL_SCOPE,
        raw_artifact_root=str(raw_root),
        retention_class="restricted",
        source_history_limit="unknown",
    )

    disabled_task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
    )
    preparation_job = build_fuyao_financial_job(
        disabled_task,
        deployment_binding=binding,
    )
    active_task = TaskDefinition(
        job_id=disabled_task.job_id,
        version=disabled_task.version,
        dataset_id=disabled_task.dataset_id,
        provider=disabled_task.provider,
        model=disabled_task.model,
        enabled=True,
        schedule=ScheduleMetadata(
            kind="interval",
            expression="PT1H",
            description="Synthetic manual-run fixture; no scheduler is registered.",
        ),
    )
    request_payload = build_fuyao_financial_manual_payload(
        _INSTRUMENT,
        statement_type=statement_type,
        periodicity=periodicity,
        limit=1,
        idempotency_key=f"synthetic-{statement_type}-{periodicity}-run",
    )
    request = RunRequest(run_type="manual", **request_payload)

    warehouse_path = offline_runtime / "test_financial_archive_warehouse.sqlite"
    warehouse = sa.create_engine(
        f"sqlite:///{warehouse_path}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )

    @event.listens_for(warehouse, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    RUN_LEDGER_METADATA.create_all(warehouse)
    _migrate(warehouse, _ARCHIVE_MIGRATION, "fuyao_financial_job_archive_migration_test")
    ledger = RunLedger(warehouse)
    try:
        run_identity = build_run_identity(active_task, request)
        response = _response_body(statement_type, periodicity)
        sent_requests: list[httpx.Request] = []
        process_results: list[object] = []

        def mock_response(http_request: httpx.Request) -> httpx.Response:
            sent_requests.append(http_request)
            return httpx.Response(200, content=response)

        mock_transport = httpx.MockTransport(mock_response)

        def build_mock_fetcher(
            *,
            credentials: Any,
            source_attempt_gate: Any,
            bundle: Any,
        ) -> ThsFinancialObservationFetcher:
            return ThsFinancialObservationFetcher(
                credentials=credentials,
                source_attempt_gate=source_attempt_gate,
                instrument_registry=bundle.instrument_registry,
                unit_profiles=bundle.unit_profiles,
                mock_transport=mock_transport,
            )

        def run_fuyao_in_process_for_test(
            _executor: ReviewedProcessExecutor,
            _binding: FuyaoProcessDeploymentBinding,
            parameters: Any,
            *,
            timeout_seconds: float,
            checkpoint: Any = None,
            cancelled: Any = None,
        ) -> ProcessExecutionResult:
            # Deliberately bypass Popen and closed-env handling; executor tests
            # own those boundaries, while this test covers the run/commit seam.
            assert timeout_seconds > 0
            assert cancelled is None
            if checkpoint is not None:
                checkpoint()
            value = process_module.execute_fuyao_financial_request(parameters)
            process_results.append(value)
            return ProcessExecutionResult(
                value=value,
                stdout_discarded_bytes=0,
                stderr_discarded_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
            )

        monkeypatch.setattr(process_module, "_build_fetcher", build_mock_fetcher)
        monkeypatch.setattr(
            ReviewedProcessExecutor,
            "run_fuyao",
            run_fuyao_in_process_for_test,
        )
        monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, _SYNTHETIC_API_KEY)
        monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, _binding_document(binding))
        publisher = publish_prepared_fuyao_financial_capture
        if fail_after_publish:
            original_publisher = publisher

            def publish_then_fail(
                connection: Any,
                task: Any,
                request: Any,
                batch: Any,
                idempotency_key: str,
            ) -> Any:
                original_publisher(connection, task, request, batch, idempotency_key)
                raise RuntimeError("synthetic failure after archive inserts")

            publisher = publish_then_fail
        active_job = replace(
            preparation_job,
            task=active_task,
            reviewed=True,
            publish=publisher,
        )
        service = RunService(
            ledger,
            ReviewedJobRegistry([active_job]),
            clock=lambda: _NOW,
        )
        queued = service.submit(active_task.job_id, active_task.version, request)
        assert queued.run_key == run_identity.idempotency_key

        outcome = service.run_once("synthetic-worker", timedelta(seconds=60))

        assert outcome is not None
        expected_status = "failed" if fail_after_publish else "succeeded"
        assert outcome.record is not None and outcome.record.status == expected_status
        if fail_after_publish:
            assert outcome.record.error_text == "execution_failed:RuntimeError"
            assert outcome.publication is None
        else:
            assert outcome.publication is not None
        assert len(process_results) == 1
        result = process_results[0]
        parameters = _build_process_parameters(request, run_identity.idempotency_key)
        for invalid_shape in (None, []):
            with pytest.raises(ValueError, match="fuyao_financial_process_result_invalid"):
                _decode_process_result(
                    invalid_shape,
                    parameters=parameters,
                    binding=binding,
                )
        capture = _decode_process_result(result, parameters=parameters, binding=binding)
        assert capture.coverage_assessment == "not_assessed"
        assert capture.attempt_association_status == "local_trace_matched_unverified"
        assert capture.prepared_capture._capture_values
        if outcome.publication is not None:
            assert outcome.publication.record_count == len(capture.observation_ids)
        assert len(sent_requests) == 1
        assert sent_requests[0].url.params["thscode"] == _INSTRUMENT
        assert sent_requests[0].url.params["period"] == periodicity
        assert (
            _SYNTHETIC_API_KEY.encode()
            not in json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
        )
        if statement_type == "income" and periodicity == "annual" and not fail_after_publish:
            assert type(result) is dict
            invalid_results = [
                {**result, "instrument_id": "600000.SH"},
                {**result, "prepared_capture_b64": "%%%"},
                {
                    **result,
                    "observation_ids": [capture.observation_ids[0]] * 2,
                    "observation_count": 2,
                },
                {**result, "observation_ids": ["invalid-id"], "observation_count": 1},
                {**result, "observation_count": True},
                {**result, "attempt_association_status": "caller_declared_unverified"},
                {**result, "financial_bundle_sha256": "f" * 64},
                {**result, "unit_mapping_sha256_declared": "invalid-digest"},
                {**result, "unit_mapping_version_declared": "invalid\nversion"},
            ]
            for invalid_result in invalid_results:
                with pytest.raises(ValueError, match="fuyao_financial_process_result_invalid"):
                    _decode_process_result(
                        invalid_result,
                        parameters=parameters,
                        binding=binding,
                    )

            execution_context = RunExecutionContext(
                run_key=run_identity.idempotency_key,
                worker="synthetic-normalizer-test",
                fencing_token=1,
                _checkpoint=lambda: None,
            )
            normalized = normalize_fuyao_financial_capture(
                capture,
                request,
                execution_context,
            )
            assert normalized.commit_context is None
            assert type(normalized.commit_payload) is bytes
            assert normalized.commit_payload == capture.prepared_payload
            assert (
                normalized.quality["source_policy_decision_status"]
                == "operator_policy_allowed_unverified"
            )
            assert "authorization_status" not in normalized.quality
            with pytest.raises(TypeError, match="context must be an exact RunExecutionContext"):
                normalize_fuyao_financial_capture(capture, request, object())
            with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                normalize_fuyao_financial_capture(object(), request, execution_context)
            invalid_captures = (
                replace(capture, instrument_id="600000.SH"),
                replace(capture, prepared_payload=b""),
                replace(capture, observation_ids=("0" * 64,)),
            )
            for invalid_capture in invalid_captures:
                with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                    normalize_fuyao_financial_capture(
                        invalid_capture,
                        request,
                        execution_context,
                    )
            prepared_batch = replace(
                normalized,
                commit_payload=None,
                commit_context=capture.prepared_capture,
            )
            with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                publish_prepared_fuyao_financial_capture(
                    None,
                    object(),
                    request,
                    prepared_batch,
                    run_identity.idempotency_key,
                )
            with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                publish_prepared_fuyao_financial_capture(
                    None,
                    active_task,
                    request,
                    replace(prepared_batch, commit_context=None),
                    run_identity.idempotency_key,
                )
            with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                publish_prepared_fuyao_financial_capture(
                    None,
                    active_task,
                    request,
                    prepared_batch,
                    "invalid-run-key",
                )
            expected_observation_id = capture.observation_ids[0]
            invalid_batches = [
                replace(
                    prepared_batch,
                    records=(
                        NormalizedRecord(
                            business_key="0" * 64,
                            payload={"observation_id": "0" * 64},
                        ),
                    ),
                ),
                replace(
                    prepared_batch,
                    records=(
                        NormalizedRecord(
                            business_key=expected_observation_id,
                            payload={"observation_id": "wrong-observation"},
                        ),
                    ),
                ),
                replace(
                    prepared_batch,
                    provenance={
                        key: value
                        for key, value in prepared_batch.provenance.items()
                        if key != "source_history_limit"
                    },
                ),
                replace(
                    prepared_batch,
                    provenance={
                        **prepared_batch.provenance,
                        "capture_manifest_sha256": "f" * 64,
                    },
                ),
            ]
            for invalid_batch in invalid_batches:
                with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                    publish_prepared_fuyao_financial_capture(
                        None,
                        active_task,
                        request,
                        invalid_batch,
                        run_identity.idempotency_key,
                    )
            from opendata.pipeline import fuyao_financial_run_job as job_module

            monkeypatch.setattr(
                job_module,
                "persist_prepared_financial_observation_capture",
                lambda _connection, *, prepared: object(),
            )
            with pytest.raises(ValueError, match="fuyao_financial_capture_invalid"):
                publish_prepared_fuyao_financial_capture(
                    None,
                    active_task,
                    request,
                    prepared_batch,
                    run_identity.idempotency_key,
                )
        with warehouse.connect() as connection:
            expected_captures = 0 if fail_after_publish else 1
            expected_observations = 0 if fail_after_publish else len(capture.observation_ids)
            assert connection.scalar(select(func.count()).select_from(CAPTURES)) == (
                expected_captures
            )
            assert connection.scalar(select(func.count()).select_from(OBSERVATIONS)) == (
                expected_observations
            )
        assert raw_root.exists()
        assert any(path.is_file() for path in raw_root.rglob("*"))
    finally:
        gate_engine.dispose()
        warehouse.dispose()
