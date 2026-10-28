"""Offline integration of Fuyao preflight with the real SQLite attempt gate.

Every row in this module is synthetic and test-owned. A matching declaration
is not real source authorization, and the mocked transport never contacts
Fuyao or claims that data was collected.
"""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import V2_OBJECT_SQL, V2_REVISION
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    GrantRevocationStatus,
    SourceAttemptGate,
)
from opendata.data.source_policy import SourcePolicyDocument, SourceUsePolicy
from opendata.pipeline.process_executor import FuyaoProcessDeploymentBinding

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine


_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = _ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
_MIGRATION_NAME = "fuyao_manual_real_gate_integration_0015"
_SOURCE = "ths"
_MODEL = "financial_statement"
_OPERATION = "query"
_GRANT_ID = "synthetic-fuyao-preflight-grant"
_PRINCIPAL = "synthetic-fuyao-preflight-worker"
_QUOTA_KEY = "synthetic-fuyao-preflight-quota"
_INSTRUMENT_ID = "999999.BJ"
_SYNTHETIC_KEY = "synthetic-key-never-sent"
_POLICY_BODY = b"synthetic source-policy pin; no legal meaning"
_BUNDLE_SHA256 = "a" * 64
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)
_GATE_TABLES = (
    "alembic_version_source_gate",
    "od_source_gate_clock",
    "od_source_gate_policies",
    "od_source_gate_source_bindings",
    "od_source_gate_state",
    "od_source_grants",
    "od_source_grant_hosts",
    "od_source_grant_host_sets",
    "od_source_grant_events",
    "od_source_attempt_reservations",
    "od_source_attempt_scope_v2",
)
_GATE_SNAPSHOT_QUERIES = {
    "alembic_version_source_gate": "SELECT * FROM alembic_version_source_gate ORDER BY rowid",
    "od_source_gate_clock": "SELECT * FROM od_source_gate_clock ORDER BY rowid",
    "od_source_gate_policies": "SELECT * FROM od_source_gate_policies ORDER BY rowid",
    "od_source_gate_source_bindings": (
        "SELECT * FROM od_source_gate_source_bindings ORDER BY rowid"
    ),
    "od_source_gate_state": "SELECT * FROM od_source_gate_state ORDER BY rowid",
    "od_source_grants": "SELECT * FROM od_source_grants ORDER BY rowid",
    "od_source_grant_hosts": "SELECT * FROM od_source_grant_hosts ORDER BY rowid",
    "od_source_grant_host_sets": "SELECT * FROM od_source_grant_host_sets ORDER BY rowid",
    "od_source_grant_events": "SELECT * FROM od_source_grant_events ORDER BY rowid",
    "od_source_attempt_reservations": (
        "SELECT * FROM od_source_attempt_reservations ORDER BY rowid"
    ),
    "od_source_attempt_scope_v2": "SELECT * FROM od_source_attempt_scope_v2 ORDER BY rowid",
}


def _load_migration() -> ModuleType:
    """Load the existing SQLite-only gate migration without Alembic env setup."""
    spec = importlib.util.spec_from_file_location(_MIGRATION_NAME, _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate_engine(tmp_path: Path) -> Iterator[Engine]:
    """Apply the existing synthetic gate migration to a fresh private SQLite file."""
    database_path = (tmp_path / "fuyao-manual-gate.sqlite").resolve()
    engine = sa.create_engine(f"sqlite:///{database_path}", poolclass=NullPool)
    migration = _load_migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            text(
                "CREATE TABLE alembic_version_source_gate ("
                " version_num VARCHAR(32) NOT NULL, CONSTRAINT "
                "alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
            )
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_gate(
    engine: Engine,
    *,
    model: str = _MODEL,
    conditions_json: str = "[]",
    malformed_timestamps: bool = False,
) -> None:
    """Seed a source-policy bucket and one explicitly synthetic Fuyao grant."""
    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    digest = hashlib.sha256(b"synthetic fixture; not rights evidence").hexdigest()
    with engine.begin() as connection:
        if malformed_timestamps:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, "
                "configured_by, configured_at_us) VALUES "
                "(:quota, 1, 1000000, 5000000, 'synthetic:test-policy', "
                ":digest, 'offline-test', 1)"
            ),
            {"quota": _QUOTA_KEY, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, "
                "configured_by, configured_at_us) VALUES "
                "(:source, :quota, 'synthetic:test-binding', :digest, 'offline-test', 1)"
            ),
            {"source": _SOURCE, "quota": _QUOTA_KEY, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, 5000000, 0, 1, 1)"
            ),
            {"quota": _QUOTA_KEY},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant, :source, :model, :operation, :principal, 'ALLOWED', 1, 0, "
                ":expires, :conditions, 'synthetic:fixture-reference', :digest, "
                "'Synthetic declaration; not legal authorization.', 'offline-test', 1, "
                ":created, :updated)"
            ),
            {
                "grant": _GRANT_ID,
                "source": _SOURCE,
                "model": model,
                "operation": _OPERATION,
                "principal": _PRINCIPAL,
                "expires": _MAX_TIMESTAMP_US,
                "conditions": conditions_json,
                "digest": digest,
                "created": 2 if malformed_timestamps else 1,
                "updated": 1,
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": _GRANT_ID, "host": host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-fixture')"
            ),
            {"grant": _GRANT_ID},
        )
        if malformed_timestamps:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")


def _gate_snapshot(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Read all source-gate rows, including the clock, quota and attempt ledgers."""
    with engine.connect() as connection:
        return {
            table: tuple(
                tuple(row)
                for row in connection.exec_driver_sql(_GATE_SNAPSHOT_QUERIES[table]).all()
            )
            for table in _GATE_TABLES
        }


def _preflight_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    """Import composition from an empty working directory with only synthetic inputs."""
    monkeypatch.chdir(tmp_path)
    from opendata.pipeline import fuyao_manual_composition as composition

    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    policy = SourceUsePolicy(
        policy_id="synthetic-fuyao-policy",
        source=_SOURCE,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        principal_user_id=17,
        product="synthetic fixture",
        purpose="offline integration test",
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic reference only",
        allowed_hosts=(host,),
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        conditions=(),
        task_attempts=1,
        source_attempts=1,
    )
    document = SourcePolicyDocument(version=1, policies=(policy,))
    monkeypatch.setattr(
        composition,
        "load_fuyao_financial_bundle",
        lambda **_kwargs: SimpleNamespace(
            instrument_id=_INSTRUMENT_ID,
            bundle_sha256=_BUNDLE_SHA256,
        ),
    )
    monkeypatch.setattr(composition, "_read_atomic_regular_file", lambda _path: _POLICY_BODY)
    monkeypatch.setattr(composition, "parse_source_policy_document", lambda _body: document)
    return composition


def _binding(tmp_path: Path, engine: Engine) -> FuyaoProcessDeploymentBinding:
    """Create a path-pinned binding that points only at test-owned files."""
    database_path = Path(engine.url.database or "").resolve(strict=True)
    return FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_KEY,
        financial_bundle_path=str(tmp_path / "synthetic-bundle.json"),
        financial_bundle_sha256=_BUNDLE_SHA256,
        source_policy_path=str(tmp_path / "synthetic-policy.json"),
        source_policy_sha256=hashlib.sha256(_POLICY_BODY).hexdigest(),
        gate_database_path=str(database_path),
        source_attempt_grant_id=_GRANT_ID,
        source_attempt_expected_revision=1,
        source_attempt_principal_scope=_PRINCIPAL,
        raw_artifact_root=str(tmp_path / "synthetic-raw"),
        retention_class="unknown",
        source_history_limit="unknown",
    )


def _enabled_config(
    composition: ModuleType,
    tmp_path: Path,
    engine: Engine,
    gate: SourceAttemptGate,
) -> Any:
    """Compose one explicit synthetic query around the actual gate object."""
    return composition.FuyaoManualCompositionConfig(
        preflight_enabled=True,
        deployment_binding=_binding(tmp_path, engine),
        query=composition.FuyaoManualQuery(
            instrument_id=_INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            idempotency_key="synthetic-manual-run-001",
            limit=1,
        ),
        source_attempt_gate=gate,
    )


def test_real_gate_match_is_read_only_and_only_advisory(
    gate_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real persisted grant matches while inspection leaves all gate rows intact."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    composition = _preflight_module(monkeypatch, tmp_path)
    config = _enabled_config(composition, tmp_path, gate_engine, gate)
    before = _gate_snapshot(gate_engine)

    report = composition.prepare_fuyao_manual_preflight(config, now=_NOW)

    assert report.status == "preflight_only"
    assert report.gate_grant_status == "matched"
    assert report.dispatch_allowed is False
    assert report.scheduler_started is False
    assert report.worker_started is False
    assert _gate_snapshot(gate_engine) == before
    assert _SYNTHETIC_KEY not in repr(report)


@pytest.mark.parametrize(
    ("state", "expected_status", "model", "conditions", "malformed_timestamps"),
    [
        ("scope mismatch", "rejected", "other_model", "[]", False),
        ("revoked", "rejected", _MODEL, "[]", False),
        ("corrupt conditions", "indeterminate", _MODEL, '{"unexpected":true}', False),
        ("contradictory timestamps", "indeterminate", _MODEL, "[]", True),
    ],
)
def test_real_gate_scope_revocation_and_corruption_fail_closed(
    gate_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    expected_status: str,
    model: str,
    conditions: str,
    malformed_timestamps: bool,
) -> None:
    """Persisted scope or integrity problems cannot become a preflight match."""
    _seed_gate(
        gate_engine,
        model=model,
        conditions_json=conditions,
        malformed_timestamps=malformed_timestamps,
    )
    gate = SourceAttemptGate(gate_engine)
    if state == "revoked":
        assert (
            gate.revoke_grant(
                _GRANT_ID,
                expected_revision=1,
                actor="offline-test",
                reason_code="synthetic-test-revoke",
            ).status
            is GrantRevocationStatus.REVOKED
        )
    composition = _preflight_module(monkeypatch, tmp_path)
    config = _enabled_config(composition, tmp_path, gate_engine, gate)
    before = _gate_snapshot(gate_engine)

    report = composition.prepare_fuyao_manual_preflight(config, now=_NOW)

    assert report.gate_grant_status == expected_status
    assert report.dispatch_allowed is False
    assert report.scheduler_started is False
    assert report.worker_started is False
    assert _gate_snapshot(gate_engine) == before


def test_disabled_default_does_not_inspect_an_injected_real_gate(
    gate_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default-off composition returns before touching an injected gate."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    composition = _preflight_module(monkeypatch, tmp_path)
    monkeypatch.setattr(
        SourceAttemptGate,
        "inspect_grant",
        lambda *_args, **_kwargs: pytest.fail("disabled preflight touched the gate"),
    )
    before = _gate_snapshot(gate_engine)

    report = composition.prepare_fuyao_manual_preflight(
        composition.FuyaoManualCompositionConfig(source_attempt_gate=gate)
    )

    assert report.status == "disabled"
    assert report.gate_grant_status == "not_checked"
    assert report.dispatch_allowed is False
    assert report.scheduler_started is False
    assert report.worker_started is False
    assert _gate_snapshot(gate_engine) == before


def test_match_then_revoke_denies_real_transport_before_mock_send(
    gate_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An advisory match is rechecked at reservation after revocation, before send."""
    _seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    composition = _preflight_module(monkeypatch, tmp_path)
    config = _enabled_config(composition, tmp_path, gate_engine, gate)
    before_inspection = _gate_snapshot(gate_engine)
    report = composition.prepare_fuyao_manual_preflight(config, now=_NOW)
    assert report.gate_grant_status == "matched"
    assert _gate_snapshot(gate_engine) == before_inspection

    revoked = gate.revoke_grant(
        _GRANT_ID,
        expected_revision=1,
        actor="offline-test",
        reason_code="synthetic-test-revoke-after-preflight",
    )
    assert (revoked.status, revoked.revision) == (GrantRevocationStatus.REVOKED, 2)

    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    budget_grant = RequestGrant(
        source=_SOURCE,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic local test declaration",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=(host,),
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    budget = RequestBudget(task_attempts=1, source_attempts=1, grants=(budget_grant,))
    binding = SourceAttemptBinding(
        grant_id=_GRANT_ID,
        expected_revision=1,
        principal_scope=_PRINCIPAL,
        run_key="synthetic-manual-run-001",
    )
    sent: list[httpx.Request] = []

    def observe_only(request: httpx.Request) -> httpx.Response:
        """Record any attempted send; no socket or external handler is available."""
        sent.append(request)
        return httpx.Response(200, json={"code": 0, "message": "synthetic"})

    client = FuyaoHttpClient(
        credentials=FuyaoCredentials(_SYNTHETIC_KEY),
        mock_transport=httpx.MockTransport(observe_only),
        rate_limiter=FuyaoRateLimiter(rate_per_second=1000.0, burst=10),
        max_attempts=1,
        sleep=lambda _seconds: None,
        source_attempt_gate=gate,
    )
    try:
        with (
            request_execution_scope(
                source=_SOURCE,
                canonical_model=_MODEL,
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=binding,
            ),
            pytest.raises(RequestAuthorizationError, match="source attempt gate"),
        ):
            client.get("/api/financial-statements", params={"instrument": _INSTRUMENT_ID})
    finally:
        client.close()

    with gate_engine.connect() as connection:
        attempts = connection.execute(
            text("SELECT outcome, denial_code FROM od_source_attempt_reservations")
        ).all()
        token_balance = connection.execute(
            text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
            {"quota": _QUOTA_KEY},
        ).scalar_one()
    assert sent == []
    assert attempts == [("DENIED", "grant_not_allowed")]
    assert token_balance == 5_000_000
    assert budget.attempts_used == 1
