"""Offline integration of source-grant provisioning with the THS Fuyao send gate.

Every credential and rights record in this module is synthetic. The client is
bound to an exact ``httpx.MockTransport`` and its grant, policy, source
binding, and quota live only in a file-backed SQLite database under
``tmp_path``. No application settings or environment credentials are loaded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import V2_OBJECT_SQL, V2_REVISION
from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
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
    SourceGrantInspectionReason,
    SourceGrantInspectionRequest,
    SourceGrantInspectionStatus,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisioningStatus,
    SourceGrantProvisionRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from sqlalchemy.engine import Engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "source_grant_provisioning_transport_integration_0015_test"
SOURCE = "ths"
MODEL = "financial_statement"
OPERATION = "query"
PRINCIPAL = "synthetic-local-researcher"
GRANT_ID = "synthetic-ths-financial-grant"
QUOTA_KEY = "quota-test"
HOST = "fuyao.aicubes.cn"
RUN_KEY = "synthetic-integration-run"
MICROTOKEN = 1_000_000
CAPACITY_MICROTOKENS = 3 * MICROTOKEN
EVIDENCE_REFERENCE = "synthetic-test-only:operator-declaration"
EVIDENCE_SHA256 = hashlib.sha256(b"synthetic test evidence only").hexdigest()
_SUCCESS = json.dumps(
    {
        "code": 0,
        "message": "success",
        "request_id": "synthetic-mock-response",
        "data": {"item": []},
    }
).encode()


def _load_migration() -> ModuleType:
    """Load only the standalone SQLite source-gate migration."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_gate_schema(engine: Engine) -> None:
    """Install migration 0015 and its exact v2 marker into owned test storage."""
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


def _provision_request(**overrides: object) -> SourceGrantProvisionRequest:
    """Create a synthetic self-declaration with explicit aware UTC bounds."""
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "grant_id": GRANT_ID,
        "revision": 1,
        "source": SOURCE,
        "canonical_model": MODEL,
        "operation": OPERATION,
        "principal_scope": PRINCIPAL,
        "host": HOST,
        "not_before": now - timedelta(seconds=30),
        "expires_at": now + timedelta(days=45),
        "rights_evidence_reference": EVIDENCE_REFERENCE,
        "rights_evidence_sha256": EVIDENCE_SHA256,
        "operator_confirms_authorized_use": True,
        "operator_attestation_note": "Synthetic test declaration; no supplier rights are asserted.",
        "attested_by": "synthetic-test-operator",
        "conditions": (),
    }
    values.update(overrides)
    return SourceGrantProvisionRequest(**values)  # type: ignore[arg-type]


def _epoch_microseconds(value: datetime) -> int:
    """Convert an aware UTC timestamp to integer epoch microseconds exactly."""
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def _provision_source_grant(gate: SourceAttemptGate) -> None:
    """Provision a 45-day synthetic grant and verify the exact persisted UTC bounds."""
    request = _provision_request()
    result = SourceGrantProvisioner(gate).provision(request)
    assert result.status is SourceGrantProvisioningStatus.PROVISIONED
    assert result.not_before_us == _epoch_microseconds(request.not_before)
    assert result.expires_at_us == _epoch_microseconds(request.expires_at)
    assert result.expires_at_us - result.not_before_us > 7 * 24 * 60 * 60 * 1_000_000


def _seed_policy_binding_and_quota(engine: Engine, *, include_quota_state: bool = True) -> None:
    """Seed the same explicit policy, source binding, and bucket shape as gate tests."""
    digest = hashlib.sha256(b"synthetic test policy and binding").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1, :capacity, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": QUOTA_KEY,
                "capacity": CAPACITY_MICROTOKENS,
                "reference": "synthetic-test-only:source-budget-policy",
                "digest": digest,
                "actor": "synthetic-test-operator",
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, :reference, :digest, :actor, 1)"
            ),
            {
                "source": SOURCE,
                "quota": QUOTA_KEY,
                "reference": "synthetic-test-only:ths-source-binding",
                "digest": digest,
                "actor": "synthetic-test-operator",
            },
        )
        if include_quota_state:
            connection.execute(
                text(
                    "INSERT INTO od_source_gate_state "
                    "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                    "VALUES (:quota, :tokens, 0, 1, 1)"
                ),
                {"quota": QUOTA_KEY, "tokens": CAPACITY_MICROTOKENS},
            )


def _inspection_request(**overrides: object) -> SourceGrantInspectionRequest:
    """Build the exact production consumer tuple for read-only inspection."""
    values: dict[str, object] = {
        "grant_id": GRANT_ID,
        "revision": 1,
        "source": SOURCE,
        "canonical_model": MODEL,
        "operation": OPERATION,
        "principal_scope": PRINCIPAL,
        "host": HOST,
    }
    values.update(overrides)
    return SourceGrantInspectionRequest(**values)  # type: ignore[arg-type]


def _request_budget(*, model: str = MODEL) -> RequestBudget:
    """Build one matching in-memory consumer grant with a small local ceiling."""
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic-test-only:consumer-scope",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts={HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,))


def _client(
    gate: SourceAttemptGate,
    handler: Any,
    *,
    max_attempts: int = 1,
) -> FuyaoHttpClient:
    """Construct the THS Fuyao consumer with only an exact in-memory transport."""
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-fixture-key", base_url=f"https://{HOST}"),
        mock_transport=httpx.MockTransport(handler),
        max_attempts=max_attempts,
        backoff_base_seconds=0,
        backoff_jitter_seconds=0,
        sleep=lambda _seconds: None,
        rate_limiter=FuyaoRateLimiter(rate_per_second=1_000, burst=1_000),
        source_attempt_gate=gate,
    )


def _run_financial_statement_query(
    client: FuyaoHttpClient,
    *,
    model: str = MODEL,
    budget: RequestBudget | None = None,
) -> Any:
    """Invoke the actual THS transport under its trusted source-attempt binding."""
    effective_budget = budget if budget is not None else _request_budget(model=model)
    with request_execution_scope(
        source=SOURCE,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        budget=effective_budget,
        source_attempt_binding=SourceAttemptBinding(
            grant_id=GRANT_ID,
            expected_revision=1,
            principal_scope=PRINCIPAL,
            run_key=RUN_KEY,
        ),
    ):
        return client.get("/v1/financial-statement", params={"symbol": "600000.SH"})


def _attempt_rows(engine: Engine) -> list[dict[str, object]]:
    """Read only the test-owned durable send-decision rows."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT * FROM od_source_attempt_reservations ORDER BY rowid")
        ).mappings()
        return [dict(row) for row in rows]


def _quota_balance(engine: Engine) -> int:
    """Read the only synthetic source quota bucket."""
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
                {"quota": QUOTA_KEY},
            ).scalar_one()
        )


@pytest.fixture
def provisioned_gate(
    tmp_path: Path,
) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Yield a temporary gate with a provisioned grant and explicit quota policy."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'source-grant-provisioning-integration.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    try:
        _install_gate_schema(engine)
        gate = SourceAttemptGate(engine)
        _seed_policy_binding_and_quota(engine)
        _provision_source_grant(gate)
        yield engine, gate
    finally:
        engine.dispose()


def test_provision_inspect_and_every_mock_send_reserves_and_debits_quota(
    provisioned_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The provisioned real consumer tuple reserves and spends quota per physical send."""
    engine, gate = provisioned_gate
    inspection = gate.inspect_grant(_inspection_request())
    assert inspection.status is SourceGrantInspectionStatus.MATCHED
    assert inspection.reason is SourceGrantInspectionReason.GRANT_MATCHED

    sent: list[httpx.Request] = []
    balances_at_send: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rows = _attempt_rows(engine)
        assert len(rows) == len(sent) + 1
        reservation = rows[-1]
        assert reservation["outcome"] == "RESERVED"
        assert reservation["attempt_no"] == len(sent) + 1
        assert reservation["grant_id"] == GRANT_ID
        assert reservation["source"] == SOURCE
        assert reservation["canonical_model"] == MODEL
        assert reservation["operation"] == OPERATION
        assert reservation["principal_scope"] == PRINCIPAL
        assert reservation["host"] == HOST
        assert reservation["run_key"] == RUN_KEY
        assert request.url.host == HOST
        assert request.url.path == "/v1/financial-statement"
        balances_at_send.append(_quota_balance(engine))
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ReadTimeout("synthetic mock timeout", request=request)
        return httpx.Response(200, content=_SUCCESS)

    client = _client(gate, handler, max_attempts=2)
    try:
        result = _run_financial_statement_query(client)
    finally:
        client.close()

    rows = _attempt_rows(engine)
    assert result.status_code == 200
    assert result.envelope.code == 0
    assert len(sent) == len(rows) == 2
    assert [row["attempt_no"] for row in rows] == [1, 2]
    assert {row["outcome"] for row in rows} == {"RESERVED"}
    assert balances_at_send == [2 * MICROTOKEN, MICROTOKEN]
    assert _quota_balance(engine) == MICROTOKEN


def test_revoked_provisioned_grant_creates_denial_and_never_calls_transport(
    provisioned_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A grant revoked after inspection cannot reach even the mock send callback."""
    engine, gate = provisioned_gate
    inspection = gate.inspect_grant(_inspection_request())
    assert inspection.status is SourceGrantInspectionStatus.MATCHED
    assert (
        gate.revoke_grant(
            GRANT_ID,
            expected_revision=1,
            actor="synthetic-test-operator",
            reason_code="test-revocation",
        ).status
        is GrantRevocationStatus.REVOKED
    )
    sent: list[httpx.Request] = []
    client = _client(
        gate,
        lambda request: sent.append(request) or httpx.Response(200, content=_SUCCESS),
    )
    try:
        with pytest.raises(RequestAuthorizationError, match="source attempt gate"):
            _run_financial_statement_query(client)
    finally:
        client.close()

    rows = _attempt_rows(engine)
    assert sent == []
    assert len(rows) == 1
    assert rows[0]["outcome"] == "DENIED"
    assert rows[0]["denial_code"] == "grant_not_allowed"


def test_provisioned_grant_scope_mismatch_never_calls_transport(
    provisioned_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A locally matching consumer scope still cannot widen the persisted grant."""
    engine, gate = provisioned_gate
    inspection = gate.inspect_grant(_inspection_request())
    assert inspection.status is SourceGrantInspectionStatus.MATCHED
    mismatch_model = "financial_statement_preview"
    sent: list[httpx.Request] = []
    client = _client(
        gate,
        lambda request: sent.append(request) or httpx.Response(200, content=_SUCCESS),
    )
    try:
        with pytest.raises(RequestAuthorizationError, match="source attempt gate"):
            _run_financial_statement_query(client, model=mismatch_model)
    finally:
        client.close()

    rows = _attempt_rows(engine)
    assert sent == []
    assert len(rows) == 1
    assert rows[0]["canonical_model"] == mismatch_model
    assert rows[0]["outcome"] == "DENIED"
    assert rows[0]["denial_code"] == "grant_scope_mismatch"


def test_missing_quota_state_denies_provisioned_grant_before_mock_send(
    tmp_path: Path,
) -> None:
    """A valid grant without an initialized quota bucket cannot authorize a send."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'source-grant-missing-quota.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    client: FuyaoHttpClient | None = None
    try:
        _install_gate_schema(engine)
        gate = SourceAttemptGate(engine)
        _seed_policy_binding_and_quota(engine, include_quota_state=False)
        _provision_source_grant(gate)
        inspection = gate.inspect_grant(_inspection_request())
        assert inspection.status is SourceGrantInspectionStatus.MATCHED

        sent: list[httpx.Request] = []
        client = _client(
            gate,
            lambda request: sent.append(request) or httpx.Response(200, content=_SUCCESS),
        )
        with pytest.raises(RequestAuthorizationError, match="source attempt gate"):
            _run_financial_statement_query(client)
        rows = _attempt_rows(engine)
        assert sent == []
        assert len(rows) == 1
        assert rows[0]["outcome"] == "DENIED"
        assert rows[0]["denial_code"] == "source_quota_state_unavailable"
    finally:
        if client is not None:
            client.close()
        engine.dispose()
