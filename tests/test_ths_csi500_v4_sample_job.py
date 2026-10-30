"""CSI 500 V4 integration over a disposable one-shot gate.

The test uses synthetic data and a MockTransport callback. The callback is
ordinary Python, not a network sandbox; offline execution depends on the test
runner's outer network namespace isolation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.ths.endpoints import (
    build_index_prices_request,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.index_daily_publication import build_index_daily_contract
from opendata.data.providers.ths.models import index_daily as ths_index_model
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V4,
    SourceAttemptBinding,
    current_request_scopes,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import (
    OneShotScopeInspectionStatus,
    SourceAttemptGate,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisionRequest,
)
from opendata.pipeline import ths_index_daily_publication as job_module
from opendata.pipeline.run_service import RunExecutionContext
from scripts import migrate_source_gate

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from sqlalchemy.engine import Engine

SAMPLE_DATE = date(2026, 10, 28)
_ALLOWED_QUERY_FIELDS = ("end", "interval", "start", "thscode")
_BASE_GATE_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
)


def _target_identity_sha256(database_path: Path) -> str:
    """Compute the V4 composition's canonical device/inode digest."""
    stat = database_path.stat()
    payload = json.dumps(
        {"device": stat.st_dev, "inode": stat.st_ino},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _install_v4_schema(engine: Engine) -> None:
    """Install the source gate migrations in this disposable test database."""
    spec = importlib.util.spec_from_file_location(
        "ths_csi500_v4_sample_base_migration",
        _BASE_GATE_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    database_path = engine.url.database
    if not database_path:
        raise AssertionError("V4 sample tests require a file-backed SQLite gate")
    assert migrate_source_gate.main(["--database-path", database_path]) == 0


def _sample_spec(
    database_path: Path,
    *,
    business_date: date = SAMPLE_DATE,
) -> job_module.Csi500FuyaoV4SampleSpec:
    """Build one deterministic synthetic identity bound to this SQLite file."""
    builder: Any = job_module.build_csi500_v4_sample_spec
    return builder(**_sample_spec_arguments(database_path, business_date=business_date))


def _sample_spec_arguments(
    database_path: Path,
    *,
    business_date: date = SAMPLE_DATE,
) -> dict[str, object]:
    """Return the synthetic, offline-only builder inputs for one sample."""
    evidence_sha256 = hashlib.sha256(b"offline-only CSI sample evidence").hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "business_date": business_date,
        "target_id": "a" * 32,
        "target_identity_sha256": _target_identity_sha256(database_path),
        "calendar_manifest_sha256": "b" * 64,
        "calendar_artifact_sha256": "c" * 64,
        "authorization_actor": "offline-csi500-v4-test",
        "grant_id": "offline-csi500-v4-grant",
        "grant_revision": 1,
        "principal_scope": "offline-csi500-v4-worker",
        "grant_expires_at_utc": expires_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "rights_evidence_sha256": evidence_sha256,
        "budget_binding_sha256": "d" * 64,
        "budget_quota_key": "csi500-fuyao-sample-204",
        "budget_policy_revision": 1,
        "budget_policy_sha256": "e" * 64,
    }


def _configure_v4_gate(
    engine: Engine,
    gate: SourceAttemptGate,
    spec: job_module.Csi500FuyaoV4SampleSpec,
) -> None:
    """Write fixture-only quota/grant state and lock the exact V4 scope."""
    scope = spec.source_attempt_scope
    binding = spec.source_attempt_binding
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, :revision, 1000000, 5000000, "
                "'synthetic:csi500-v4-policy', :digest, 'offline-test', 1)"
            ),
            {
                "quota": spec.budget_quota_key,
                "revision": spec.budget_policy_revision,
                "digest": spec.budget_policy_sha256,
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES ('ths', :quota, 'synthetic:csi500-v4-binding', "
                ":digest, 'offline-test', 1)"
            ),
            {
                "quota": spec.budget_quota_key,
                "digest": spec.budget_binding_sha256,
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, 5000000, 0, 1, :revision)"
            ),
            {
                "quota": spec.budget_quota_key,
                "revision": spec.budget_policy_revision,
            },
        )

    grant_expiry = datetime.fromisoformat(spec.grant_expires_at_utc.replace("Z", "+00:00"))
    SourceGrantProvisioner(gate).provision(
        SourceGrantProvisionRequest(
            grant_id=binding.grant_id,
            revision=binding.expected_revision,
            source="ths",
            canonical_model="index_daily",
            operation="query",
            principal_scope=binding.principal_scope,
            host=scope.attempt.host,
            not_before=datetime.now(timezone.utc) - timedelta(minutes=1),
            expires_at=grant_expiry,
            rights_evidence_reference=binding.expected_rights_evidence_reference or "",
            rights_evidence_sha256=spec.rights_evidence_sha256,
            operator_confirms_authorized_use=True,
            operator_attestation_note=(
                "Synthetic offline fixture only; no supplier-rights claim is made."
            ),
            attested_by="offline-test-operator",
        )
    )
    gate.lock_one_shot_scope(scope)


@pytest.fixture
def configured_gate(
    tmp_path: Path,
) -> Generator[tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec], None, None]:
    """Yield a disposable V4 SQLite gate with one exact unused lock."""
    database_path = tmp_path / "csi500-v4-sample.sqlite"
    engine = sa.create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )
    try:
        _install_v4_schema(engine)
        gate = SourceAttemptGate(engine)
        sample_spec = _sample_spec(database_path)
        _configure_v4_gate(engine, gate, sample_spec)
        yield engine, gate, sample_spec
    finally:
        engine.dispose()


def _access(
    gate: SourceAttemptGate,
    spec: job_module.Csi500FuyaoV4SampleSpec,
) -> job_module.Csi500FuyaoV4SampleAccess:
    """Build the explicit user-declaration access bound to this locked file."""
    return job_module.Csi500FuyaoV4SampleAccess(
        source_attempt_gate=gate,
        sample_spec=spec,
        operator_confirms_user_declared_internal_use=True,
    )


def _v3_access(
    gate: SourceAttemptGate,
    *,
    business_date: date = SAMPLE_DATE,
) -> job_module.Csi500FuyaoV3SampleAccess:
    """Build a valid V3 carrier to exercise compatibility boundaries offline."""
    return job_module.Csi500FuyaoV3SampleAccess(
        source_attempt_gate=gate,
        source_attempt_binding=SourceAttemptBinding(
            grant_id="offline-v3-grant",
            expected_revision=1,
            principal_scope="offline-v3-worker",
            expected_rights_evidence_reference="docs/data-rights-registry.md#0.1",
        ),
        business_date=business_date,
        capture_intent_id="offline-v3-capture-intent",
        source_policy_id="offline-v3-policy",
        source_policy_revision="1",
        source_policy_sha256="a" * 64,
        budget_binding_sha256="b" * 64,
        budget_quota_key="offline-v3-budget",
        budget_policy_revision=1,
        budget_policy_sha256="c" * 64,
        target_identity_sha256="d" * 64,
        operator_confirms_user_declared_internal_use=True,
    )


def _offline_sample_spec(tmp_path: Path) -> job_module.Csi500FuyaoV4SampleSpec:
    """Create only a regular-file identity fixture, without opening a database."""
    target_path = tmp_path / "csi500-v4-identity-fixture.sqlite"
    target_path.touch()
    return _sample_spec(target_path)


def _context(
    spec: job_module.Csi500FuyaoV4SampleSpec,
    *,
    run_key: str | None = None,
) -> RunExecutionContext:
    """Return a no-op context carrying the stable run key already locked in V4."""
    return RunExecutionContext(
        run_key=spec.run_key if run_key is None else run_key,
        worker="offline-csi500-v4-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    gate: SourceAttemptGate,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Inject a MockTransport-backed Fuyao client at the existing provider seam."""

    @contextmanager
    def gated_client(
        *,
        timeout_seconds: float | None = None,
        source_attempt_gate: SourceAttemptGate | None = None,
        max_attempts: int | None = None,
    ) -> Iterator[FuyaoHttpClient]:
        assert timeout_seconds == 30
        assert source_attempt_gate is gate
        assert max_attempts == 1
        active = FuyaoHttpClient(
            credentials=FuyaoCredentials(
                "offline-fixture-key",
                base_url=DEFAULT_FUYAO_API_BASE_URL,
            ),
            mock_transport=httpx.MockTransport(handler),
            source_attempt_gate=gate,
            max_attempts=1,
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1000, burst=10),
        )
        try:
            yield active
        finally:
            active.close()

    monkeypatch.setattr(ths_index_model, "client", gated_client)


def test_v4_job_reuses_locked_ids_pin_and_exact_query_through_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """The job's only request carries the pre-locked V4 identity unchanged."""
    engine, gate, spec = configured_gate
    access = _access(gate, spec)
    job = job_module.build_csi500_v4_sample_index_daily_job(
        DatasetContractRegistry([build_index_daily_contract("000905.SH")]),
        source_access=access,
    )
    assert job.reviewed is True
    assert job.one_shot_only is True
    assert job.task == spec.task_definition
    assert job.task.enabled is False
    assert job.task.schedule is None

    expected_query = {
        key: str(value)
        for key, value in build_index_prices_request(
            symbol="000905.SH",
            start=spec.business_date,
            end=date.fromordinal(spec.business_date.toordinal() + 1),
        ).items()
    }
    pin = spec.source_attempt_scope_v3_pin
    assert pin.request_summary_allowed_fields == _ALLOWED_QUERY_FIELDS
    assert pin.request_summary_sha256 == request_summary_sha256(
        expected_query,
        allowed_fields=_ALLOWED_QUERY_FIELDS,
    )
    sent: list[httpx.Request] = []
    observed_scopes: list[tuple[object, ...]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        scopes = current_request_scopes()
        observed_scopes.append(scopes)
        assert scopes
        assert all(
            scope.source_attempt_scope_version == SOURCE_ATTEMPT_SCOPE_V4 for scope in scopes
        )
        assert all(scope.source_attempt_binding == spec.source_attempt_binding for scope in scopes)
        assert all(
            scope.source_attempt_scope_v3_pin == spec.source_attempt_scope_v3_pin
            for scope in scopes
        )
        assert all(
            scope.source_attempt_one_shot_scope_v4 == spec.source_attempt_scope for scope in scopes
        )
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "fuyao.aicubes.cn"
        assert request.url.path == "/api/a-share-index/prices/historical"
        assert dict(request.url.params.multi_items()) == expected_query
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "offline-provider-request",
                "data": {
                    "timestamp": shanghai_midnight_millis(spec.business_date),
                    "thscode": "000905.SH",
                    "item": [
                        {
                            "date_ms": shanghai_midnight_millis(spec.business_date),
                            "open_price": 10.0,
                            "high_price": 11.0,
                            "low_price": 9.0,
                            "close_price": 10.5,
                            "volume": 1000.0,
                            "turnover": 10500.0,
                        }
                    ],
                },
            },
        )

    _install_mock_client(monkeypatch, gate, handler)
    raw = job.fetch(spec.request, _context(spec))
    batch = job.normalize(raw, spec.request, _context(spec))

    assert len(sent) == 1
    assert len(observed_scopes) == 1
    assert len(batch.records) == 1
    assert batch.records[0].business_date == spec.business_date
    assert batch.records[0].payload["symbol"] == "000905.SH"
    assert batch.provenance["source_per_request_calendar_day_limit"] == 1
    assert gate.inspect_one_shot_scope(spec.source_attempt_scope).status is (
        OneShotScopeInspectionStatus.CONSUMED
    )
    with pytest.raises(ValueError, match="unused locked scope"):
        _access(gate, spec)
    with engine.connect() as connection:
        reservation = (
            connection.execute(
                text(
                    "SELECT request_id, attempt_id, attempt_no, run_key "
                    "FROM od_source_attempt_reservations"
                )
            )
            .mappings()
            .one()
        )
        assert dict(reservation) == {
            "request_id": spec.request_id,
            "attempt_id": spec.attempt_id,
            "attempt_no": 1,
            "run_key": spec.run_key,
        }
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_permits")).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")).scalar_one()
            == 1
        )


@pytest.mark.parametrize("mismatch", ["request", "run_key"])
def test_v4_job_rejects_mismatched_run_identity_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
    mismatch: str,
) -> None:
    """Request and context identity drift fail before HTTP or gate consumption."""
    _engine, gate, spec = configured_gate
    access = _access(gate, spec)
    job = job_module.build_csi500_v4_sample_index_daily_job(
        DatasetContractRegistry([build_index_daily_contract("000905.SH")]),
        source_access=access,
    )

    def no_provider_call(**_kwargs: object) -> object:
        pytest.fail("identity-mismatched V4 sample reached the provider")

    monkeypatch.setattr(ths_index_model, "client", no_provider_call)
    request = spec.request
    context = _context(spec)
    if mismatch == "request":
        request = job_module.RunRequest(
            run_type="manual",
            query={
                "start_date": "2026-10-29",
                "end_date": "2026-10-29",
            },
            partition={"symbol": "000905.SH"},
            idempotency_key=spec.capture_intent_id,
        )
    else:
        context = _context(spec, run_key="different-v4-run-key")

    with pytest.raises(ValueError, match="locked (request identity|transport identity)"):
        job.fetch(request, context)
    assert gate.inspect_one_shot_scope(spec.source_attempt_scope).status is (
        OneShotScopeInspectionStatus.UNUSED
    )


def test_v4_access_rejects_an_unlocked_scope(tmp_path: Path) -> None:
    """A V4 candidate cannot become a job access until the gate lock exists."""
    database_path = tmp_path / "csi500-v4-unlocked.sqlite"
    engine = sa.create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )
    try:
        _install_v4_schema(engine)
        gate = SourceAttemptGate(engine)
        sample_spec = _sample_spec(database_path)
        with pytest.raises(ValueError, match="unused locked scope"):
            _access(gate, sample_spec)
    finally:
        engine.dispose()


def test_v4_spec_rejects_malformed_fields_and_changed_intent(tmp_path: Path) -> None:
    """The public immutable spec rejects malformed declarations and tampering."""
    spec = _offline_sample_spec(tmp_path)

    with pytest.raises(TypeError, match="exact date"):
        replace(spec, business_date="2026-10-28")
    with pytest.raises(ValueError, match="target_id"):
        replace(spec, target_id="not-a-target-id")
    with pytest.raises(ValueError, match="authorization_actor"):
        replace(spec, authorization_actor="actor with spaces")
    with pytest.raises(ValueError, match="canonical UTC timestamp"):
        replace(spec, grant_expires_at_utc="2026-10-28T00:00:00+00:00")
    with pytest.raises(ValueError, match="canonical UTC timestamp"):
        replace(spec, grant_expires_at_utc="not-a-timestampZ")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(spec, target_identity_sha256="invalid")
    with pytest.raises(ValueError, match="fixed code declaration"):
        replace(spec, source_policy_id="unreviewed-policy")
    with pytest.raises(ValueError, match="do not match its gate scope"):
        replace(spec, budget_binding_sha256="f" * 64)

    # A syntactically valid authorization actor is part of the capture intent;
    # changing it without issuing a new intent digest must not retain the lock.
    with pytest.raises(ValueError, match="capture intent"):
        replace(spec, authorization_actor="different-offline-operator")
    with pytest.raises(ValueError, match="full identity"):
        replace(spec, intent_sha256="f" * 64)


def test_v4_spec_rejects_gate_scope_and_pin_substitution(tmp_path: Path) -> None:
    """A valid nested carrier cannot be substituted under an existing intent."""
    spec = _offline_sample_spec(tmp_path)
    changed_attempt = replace(spec.source_attempt_scope.attempt, attempt_id="f" * 32)
    changed_scope = replace(spec.source_attempt_scope, attempt=changed_attempt)
    with pytest.raises(ValueError, match="request pins do not match"):
        replace(spec, source_attempt_scope=changed_scope)

    changed_pin = replace(
        spec.source_attempt_scope_v3_pin,
        request_summary_sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="request pins do not match"):
        replace(spec, source_attempt_scope_v3_pin=changed_pin)


def test_v4_access_rejects_confirmation_type_and_target_drift(
    tmp_path: Path,
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """Access needs an explicit boolean and the same immutable gate-file identity."""
    _engine, gate, spec = configured_gate
    access = _access(gate, spec)
    with pytest.raises(TypeError, match="exact SourceAttemptGate"):
        replace(access, source_attempt_gate=object())
    with pytest.raises(TypeError, match="exact Csi500FuyaoV4SampleSpec"):
        replace(access, sample_spec=object())
    with pytest.raises(TypeError, match="explicit boolean"):
        replace(access, operator_confirms_user_declared_internal_use=1)
    with pytest.raises(ValueError, match="was not confirmed"):
        replace(access, operator_confirms_user_declared_internal_use=False)

    other_path = tmp_path / "different-target.sqlite"
    other_path.touch()
    other_engine = sa.create_engine(
        f"sqlite:///{other_path}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )
    try:
        other_gate = SourceAttemptGate(other_engine)
        with pytest.raises(ValueError, match="reviewed target identity"):
            _access(other_gate, spec)
    finally:
        other_engine.dispose()


def test_v4_access_rejects_a_different_locked_capture_intent(
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """A different date and capture intent cannot borrow the existing lock."""
    engine, gate, spec = configured_gate
    database_path = engine.url.database
    assert database_path is not None
    other_spec = _sample_spec(
        Path(database_path),
        business_date=date.fromordinal(SAMPLE_DATE.toordinal() + 1),
    )
    with pytest.raises(ValueError, match="unused locked scope"):
        _access(gate, other_spec)


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "error_type", "message"),
    [
        ("business_date", "2026-10-28", TypeError, "exact date"),
        ("target_id", "invalid", ValueError, "target_id"),
        ("grant_revision", 0, ValueError, "grant_revision"),
        ("budget_policy_revision", 0, ValueError, "budget_policy_revision"),
        ("calendar_manifest_sha256", "invalid", ValueError, "lowercase SHA-256"),
        ("grant_expires_at_utc", "", ValueError, "non-empty string"),
        ("grant_expires_at_utc", "x" * 41, ValueError, "too long"),
        ("grant_expires_at_utc", "not-a-timestamp", ValueError, "ISO timestamp"),
        ("grant_expires_at_utc", "2026-10-28T00:00:00", ValueError, "timezone-aware"),
        ("budget_quota_key", "unreviewed-quota", ValueError, "fixed sample quota"),
    ],
)
def test_v4_spec_builder_rejects_invalid_public_inputs(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    """The offline builder fails closed for malformed scope declarations."""
    target_path = tmp_path / "builder-identity-fixture.sqlite"
    target_path.touch()
    arguments = _sample_spec_arguments(target_path)
    arguments[field_name] = invalid_value
    builder: Any = job_module.build_csi500_v4_sample_spec
    with pytest.raises(error_type, match=message):
        builder(**arguments)


def test_v4_spec_rejects_pipeline_identity_and_request_mutations(tmp_path: Path) -> None:
    """The task, request, run and gate identities remain one immutable unit."""
    spec = _offline_sample_spec(tmp_path)
    with pytest.raises(TypeError, match="TaskDefinition"):
        replace(spec, task_definition=object())
    with pytest.raises(TypeError, match="validated pipeline identities"):
        replace(spec, request=object())
    with pytest.raises(TypeError, match="validated pipeline identities"):
        replace(spec, run_identity=object())
    tampered_run_identity = spec.run_identity.model_copy(update={"payload_hash": "f" * 64})
    with pytest.raises(ValueError, match="run identity does not match"):
        replace(spec, run_identity=tampered_run_identity)
    with pytest.raises(TypeError, match="SourceAttemptBinding"):
        replace(spec, source_attempt_binding=object())
    with pytest.raises(TypeError, match="SourceAttemptScopeV3Pin"):
        replace(spec, source_attempt_scope_v3_pin=object())
    with pytest.raises(TypeError, match="SourceAttemptScopeV3"):
        replace(spec, source_attempt_scope=object())

    changed_task = spec.task_definition.model_copy(update={"version": "unexpected-task-version"})
    changed_run = job_module.build_run_identity(changed_task, spec.request)
    with pytest.raises(ValueError, match="differs from its capture intent"):
        replace(spec, task_definition=changed_task, run_identity=changed_run)

    changed_request = spec.request.model_copy(
        update={"query": {"start_date": "2026-10-29", "end_date": "2026-10-29"}}
    )
    changed_run = job_module.build_run_identity(spec.task_definition, changed_request)
    with pytest.raises(ValueError, match="one exact CSI 500 date"):
        replace(spec, request=changed_request, run_identity=changed_run)

    changed_request = spec.request.model_copy(update={"idempotency_key": "different-intent"})
    changed_run = job_module.build_run_identity(spec.task_definition, changed_request)
    with pytest.raises(ValueError, match="request key must equal its capture intent"):
        replace(spec, request=changed_request, run_identity=changed_run)

    changed_attempt = replace(spec.source_attempt_scope.attempt, run_key="different-run-key")
    changed_scope = replace(spec.source_attempt_scope, attempt=changed_attempt)
    with pytest.raises(ValueError, match="run identity differs from its gate scope"):
        replace(spec, source_attempt_scope=changed_scope)


def test_v4_task_payload_and_access_properties_are_redacted_and_narrow(
    tmp_path: Path,
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """Public V4 helpers retain disabled scheduling and narrow declared use."""
    spec = _offline_sample_spec(tmp_path)
    payload = job_module.build_csi500_v4_sample_payload(spec=spec)
    assert payload["partition"] == {"symbol": "000905.SH"}
    assert payload["query"] == {
        "start_date": SAMPLE_DATE.isoformat(),
        "end_date": SAMPLE_DATE.isoformat(),
    }
    assert payload["idempotency_key"] == spec.capture_intent_id
    assert repr(spec) == "Csi500FuyaoV4SampleSpec(<redacted>)"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        job_module.build_csi500_v4_sample_task_definition(capture_intent_id="invalid")
    with pytest.raises(TypeError, match="Csi500FuyaoV4SampleSpec"):
        job_module.build_csi500_v4_sample_payload(spec=object())

    _engine, gate, locked_spec = configured_gate
    access = _access(gate, locked_spec)
    assert access.symbol == "000905.SH"
    assert access.dataset_id == locked_spec.task_definition.dataset_id
    assert access.endpoint_path == "/api/a-share-index/prices/historical"
    assert access.operation == "query"
    assert access.purposes == (
        "automatic_collection",
        "storage_in_opendata",
        "internal_opendata_to_afi_api",
    )
    assert access.maximum_window_calendar_days == 1
    assert access.source_history_boundary == "UNKNOWN"
    assert access.external_distribution_allowed is False
    assert access.authorization_basis == "USER_DECLARED"
    assert access.evidence_reference == "docs/data-rights-registry.md#0.1"
    assert repr(access) == "Csi500FuyaoV4SampleAccess(<redacted>)"


def test_v1_and_v3_access_validation_remains_separate_and_fail_closed(
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """Legacy V1 and opt-in V3 bindings do not silently upgrade one another."""
    _engine, gate, _spec = configured_gate
    binding = SourceAttemptBinding(
        grant_id="offline-v1-grant",
        expected_revision=1,
        principal_scope="offline-v1-worker",
    )
    v1 = job_module.Csi500FuyaoSourceAccess(gate, binding, True)
    assert v1.symbol == "000905.SH"
    assert v1.authorization_basis == "USER_DECLARED"
    assert v1.source_history_boundary == "UNKNOWN"
    assert v1.external_distribution_allowed is False
    assert v1.maximum_window_calendar_days > 1
    assert v1.binding_for_run("offline-v1-run").expected_rights_evidence_reference == (
        "docs/data-rights-registry.md#0.1"
    )
    assert repr(v1) == "Csi500FuyaoSourceAccess(<redacted>)"
    with pytest.raises(ValueError, match="non-empty string"):
        v1.binding_for_run("  ")
    with pytest.raises(TypeError, match="exact SourceAttemptGate"):
        job_module.Csi500FuyaoSourceAccess(object(), binding, True)
    with pytest.raises(TypeError, match="exact SourceAttemptBinding"):
        job_module.Csi500FuyaoSourceAccess(gate, object(), True)
    with pytest.raises(ValueError, match="run_key unset"):
        job_module.Csi500FuyaoSourceAccess(
            gate,
            SourceAttemptBinding("offline-bound-grant", 1, "offline-worker", run_key="prebound"),
            True,
        )
    with pytest.raises(TypeError, match="explicit boolean"):
        job_module.Csi500FuyaoSourceAccess(gate, binding, 1)
    with pytest.raises(ValueError, match="was not confirmed"):
        job_module.Csi500FuyaoSourceAccess(gate, binding, False)

    v3 = _v3_access(gate)
    assert v3.symbol == "000905.SH"
    assert v3.maximum_window_calendar_days == 1
    assert v3.source_history_boundary == "UNKNOWN"
    assert v3.external_distribution_allowed is False
    assert v3.binding_for_run("offline-v3-run").run_key == "offline-v3-run"
    assert v3.pin_for_request(SAMPLE_DATE, SAMPLE_DATE).expected_path == (
        "/api/a-share-index/prices/historical"
    )
    assert repr(v3) == "Csi500FuyaoV3SampleAccess(<redacted>)"
    with pytest.raises(TypeError, match="exact date"):
        replace(v3, business_date="2026-10-28")
    with pytest.raises(TypeError, match="exact SourceAttemptGate"):
        replace(v3, source_attempt_gate=object())
    with pytest.raises(TypeError, match="exact SourceAttemptBinding"):
        replace(v3, source_attempt_binding=object())
    with pytest.raises(ValueError, match="run_key unset"):
        replace(
            v3,
            source_attempt_binding=SourceAttemptBinding(
                "offline-bound-v3-grant",
                1,
                "offline-v3-worker",
                run_key="prebound",
            ),
        )
    with pytest.raises(ValueError, match="was not confirmed"):
        replace(v3, operator_confirms_user_declared_internal_use=1)
    with pytest.raises(ValueError, match="was not confirmed"):
        replace(v3, operator_confirms_user_declared_internal_use=False)
    with pytest.raises(ValueError, match="budget_policy_revision"):
        replace(v3, budget_policy_revision=0)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(v3, budget_policy_sha256="invalid")
    with pytest.raises(TypeError, match="exact dates"):
        v3.pin_for_request("2026-10-28", SAMPLE_DATE)
    with pytest.raises(ValueError, match="one exact business date"):
        v3.pin_for_request(date.fromordinal(SAMPLE_DATE.toordinal() + 1), SAMPLE_DATE)
    with pytest.raises(ValueError, match="non-empty string"):
        v3.binding_for_run("")

    registry = DatasetContractRegistry([build_index_daily_contract("000905.SH")])
    with pytest.raises(TypeError, match="exact Csi500FuyaoSourceAccess"):
        job_module.build_authorized_csi500_index_daily_job(registry, source_access=v3)
    with pytest.raises(TypeError, match="exact Csi500FuyaoV3SampleAccess"):
        job_module.build_csi500_v3_sample_index_daily_job(registry, source_access=v1)
    with pytest.raises(TypeError, match="exact Csi500FuyaoV4SampleAccess"):
        job_module.build_csi500_v4_sample_index_daily_job(registry, source_access=v3)

    v4 = _access(gate, _spec)
    with pytest.raises(ValueError, match="cannot be combined"):
        job_module._build_index_daily_job(
            "000905.SH",
            registry,
            source_access=v1,
            source_access_v3=v3,
            source_access_v4=v4,
        )
    with pytest.raises(ValueError, match="bound only to the CSI 500 dataset"):
        job_module._build_index_daily_job(
            "000300.SH",
            registry,
            source_access=None,
            source_access_v3=None,
            source_access_v4=v4,
        )

    empty_batch = job_module.NormalizedBatch(
        records=(),
        schema_version=job_module.INDEX_SCHEMA_VERSION,
        semantic_version=job_module.INDEX_SEMANTIC_VERSION,
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={},
        provenance={},
    )
    v1_job = job_module.build_authorized_csi500_index_daily_job(registry, source_access=v1)
    v3_job = job_module.build_csi500_v3_sample_index_daily_job(registry, source_access=v3)
    with pytest.raises(ValueError, match="empty index batches"):
        v1_job.publish(None, v1_job.task, _spec.request, empty_batch, _spec.capture_intent_id)
    with pytest.raises(ValueError, match="empty index batches"):
        v3_job.publish(None, v3_job.task, _spec.request, empty_batch, _spec.capture_intent_id)


def test_v4_access_rejects_unavailable_target_and_invalid_inspection_status(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """V4 access does not infer identity or a permit from malformed gate state."""
    engine, gate, spec = configured_gate
    monkeypatch.setattr(gate, "_engine", object())
    with pytest.raises(ValueError, match="target identity is unavailable"):
        _access(gate, spec)

    monkeypatch.setattr(
        gate,
        "_engine",
        SimpleNamespace(url=SimpleNamespace(database="")),
    )
    with pytest.raises(ValueError, match="target identity is unavailable"):
        _access(gate, spec)

    monkeypatch.setattr(gate, "_engine", engine)
    monkeypatch.setattr(
        gate,
        "inspect_one_shot_scope",
        lambda _scope: SimpleNamespace(status="UNUSED"),
    )
    with pytest.raises(ValueError, match="unused locked scope"):
        _access(gate, spec)


@pytest.mark.parametrize(
    ("start_date", "message"),
    [
        ("2026-02-30", "ISO date string"),
        ("20261028", "canonical ISO date string"),
    ],
)
def test_v3_normalizer_rejects_invalid_or_noncanonical_manual_dates(
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
    start_date: str,
    message: str,
) -> None:
    """The preserved V3 parser rejects impossible and alternate-form dates."""
    if start_date == "20261028":
        try:
            date.fromisoformat(start_date)
        except ValueError:
            pytest.skip("this alternate ISO form is accepted only by newer Python runtimes")
    _engine, gate, _spec = configured_gate
    access = _v3_access(gate)
    registry = DatasetContractRegistry([build_index_daily_contract("000905.SH")])
    job = job_module.build_csi500_v3_sample_index_daily_job(registry, source_access=access)
    request = job_module.RunRequest(
        run_type="manual",
        query={"start_date": start_date, "end_date": "2026-10-28"},
        partition={"symbol": "000905.SH"},
        idempotency_key="offline-v3-invalid-date",
    )
    context = RunExecutionContext(
        run_key="offline-v3-invalid-date-run",
        worker="offline-v3-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )
    with pytest.raises(ValueError, match=message):
        job.normalize((), request, context)


def test_v3_normalizer_rejects_nonstring_and_extra_manual_query_values(
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """Manual sample parsing rejects non-string dates and unreviewed query keys."""
    _engine, gate, _spec = configured_gate
    access = _v3_access(gate)
    registry = DatasetContractRegistry([build_index_daily_contract("000905.SH")])
    job = job_module.build_csi500_v3_sample_index_daily_job(registry, source_access=access)
    context = RunExecutionContext(
        run_key="offline-v3-invalid-query-run",
        worker="offline-v3-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )
    nonstring_date = job_module.RunRequest(
        run_type="manual",
        query={"start_date": 20261028, "end_date": "2026-10-28"},
        partition={"symbol": "000905.SH"},
        idempotency_key="offline-v3-nonstring-date",
    )
    with pytest.raises(ValueError, match="start_date must be an ISO date string"):
        job.normalize((), nonstring_date, context)

    extra_query_key = job_module.RunRequest(
        run_type="manual",
        query={
            "start_date": "2026-10-28",
            "end_date": "2026-10-28",
            "include_unreviewed": True,
        },
        partition={"symbol": "000905.SH"},
        idempotency_key="offline-v3-extra-query",
    )
    with pytest.raises(ValueError, match="only one explicit date window"):
        job.normalize((), extra_query_key, context)


def test_v4_normalize_and_publish_reject_drift_before_storage(
    configured_gate: tuple[Engine, SourceAttemptGate, job_module.Csi500FuyaoV4SampleSpec],
) -> None:
    """V4 normalization and publication reject identity drift before the writer."""
    _engine, gate, spec = configured_gate
    registry = DatasetContractRegistry([build_index_daily_contract("000905.SH")])
    job = job_module.build_csi500_v4_sample_index_daily_job(
        registry,
        source_access=_access(gate, spec),
    )
    wrong_context = _context(spec, run_key="different-normalize-run")
    with pytest.raises(ValueError, match="locked transport identity"):
        job.normalize((), spec.request, wrong_context)

    batch = job_module.NormalizedBatch(
        records=(),
        schema_version=job_module.INDEX_SCHEMA_VERSION,
        semantic_version=job_module.INDEX_SEMANTIC_VERSION,
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={},
        provenance={},
    )
    wrong_request = job_module.RunRequest(
        run_type="manual",
        query={"start_date": "2026-10-29", "end_date": "2026-10-29"},
        partition={"symbol": "000905.SH"},
        idempotency_key=spec.capture_intent_id,
    )
    with pytest.raises(ValueError, match="locked request identity"):
        job.publish(None, spec.task_definition, wrong_request, batch, spec.capture_intent_id)

    wrong_task = spec.task_definition.model_copy(update={"version": "changed-version"})
    with pytest.raises(ValueError, match="publication task"):
        job.publish(None, wrong_task, spec.request, batch, spec.capture_intent_id)
    with pytest.raises(ValueError, match="empty index batches"):
        job.publish(None, spec.task_definition, spec.request, batch, spec.capture_intent_id)

    malformed_batch = replace(batch, records=(object(),))
    with pytest.raises(ValueError, match="invalid index record"):
        job.publish(
            None, spec.task_definition, spec.request, malformed_batch, spec.capture_intent_id
        )

    wrong_symbol_record = job_module.NormalizedRecord(
        business_key="2026-10-28",
        business_date=spec.business_date,
        payload={"symbol": "000300.SH"},
    )
    wrong_symbol_batch = replace(batch, records=(wrong_symbol_record,))
    with pytest.raises(ValueError, match="different index symbol"):
        job.publish(
            None, spec.task_definition, spec.request, wrong_symbol_batch, spec.capture_intent_id
        )

    wrong_schema_batch = replace(
        batch,
        records=(
            job_module.NormalizedRecord(
                business_key="2026-10-28",
                business_date=spec.business_date,
                payload={"symbol": "000905.SH"},
            ),
        ),
        schema_version="unreviewed-schema",
    )
    with pytest.raises(ValueError, match="single-index contract"):
        job.publish(
            None, spec.task_definition, spec.request, wrong_schema_batch, spec.capture_intent_id
        )


def test_one_attempt_budget_rejects_a_provider_url_without_a_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded V4 request budget refuses a malformed default source URL."""
    monkeypatch.setattr(job_module, "DEFAULT_FUYAO_API_BASE_URL", "https://")
    with pytest.raises(ValueError, match="default API host is unavailable"):
        job_module._csi500_one_attempt_budget()
