"""Offline CSI 500 one-shot transport and grant-gate integration.

All grant, policy, credential, and bar values in this module are synthetic.
The real THS wrapper and file-backed gate are exercised with an exact
``httpx.MockTransport``; no supplier request or production storage is used.
"""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import V2_REVISION
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.ths import endpoints as ths_endpoints
from opendata.data.providers.ths.index_daily_publication import build_index_daily_contract
from opendata.data.providers.ths.models import _client as ths_client_module
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
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
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
)
from opendata.data.source_attempt_gate import SourceAttemptGate
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisionRequest,
)
from opendata.pipeline import ths_index_daily_publication as job_module
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_service import RunExecutionContext
from scripts import migrate_source_gate

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from sqlalchemy.engine import Engine

    from opendata.data.protocol import FetchContext


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION_PATH = _REPOSITORY_ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
_MIGRATION_NAME = "ths_csi500_one_shot_sqlite_gate_test"
_SOURCE = "ths"
_MODEL = "index_daily"
_OPERATION = "query"
_GRANT_ID = "synthetic-csi500-index-grant"
_PRINCIPAL = "synthetic-csi500-index-worker"
_QUOTA_KEY = "synthetic-csi500-index-quota"
_HOST = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
_START = date(2026, 10, 21)
_END = date(2026, 10, 22)
_EXPECTED_EVIDENCE = "docs/data-rights-registry.md#0.1"
assert _HOST is not None


def _load_migration() -> ModuleType:
    """Load the standalone gate migration without application database setup."""
    spec = importlib.util.spec_from_file_location(_MIGRATION_NAME, _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_gate_schema(engine: Engine) -> None:
    """Build v1 test storage and upgrade it through the registered gate chain."""
    migration = _load_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    database_path = engine.url.database
    if not database_path:
        raise AssertionError("the source gate fixture must use a file-backed SQLite database")
    migrate_source_gate.main(["--database-path", database_path])


def _seed_policy(engine: Engine) -> None:
    """Create one synthetic source bucket needed by the production reservation."""
    digest = hashlib.sha256(b"synthetic CSI 500 test policy and source binding").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1000000, 5000000, "
                "'synthetic:test-policy', :digest, 'offline-test', 1)"
            ),
            {"quota": _QUOTA_KEY, "digest": digest},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, 'synthetic:test-binding', "
                ":digest, 'offline-test', 1)"
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


@pytest.fixture
def source_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Provide a real gate backed only by a temporary SQLite file."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'ths-csi500-gate.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    try:
        _install_gate_schema(engine)
        gate = SourceAttemptGate(engine)
        _seed_policy(engine)
        yield engine, gate
    finally:
        engine.dispose()


def _provision_grant(gate: SourceAttemptGate, *, evidence_reference: str) -> None:
    """Provision one temporary self-declaration with no real-rights assertion."""
    now = datetime.now(timezone.utc)
    SourceGrantProvisioner(gate).provision(
        SourceGrantProvisionRequest(
            grant_id=_GRANT_ID,
            revision=1,
            source=_SOURCE,
            canonical_model=_MODEL,
            operation=_OPERATION,
            principal_scope=_PRINCIPAL,
            host=_HOST,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            rights_evidence_reference=evidence_reference,
            rights_evidence_sha256=hashlib.sha256(b"synthetic test metadata only").hexdigest(),
            operator_confirms_authorized_use=True,
            operator_attestation_note=(
                "Synthetic test declaration; supplier rights are not asserted."
            ),
            attested_by="offline-test-operator",
            conditions=(),
        )
    )


def _context() -> RunExecutionContext:
    """Build a no-op local context with a stable synthetic run identity."""
    return RunExecutionContext(
        run_key="synthetic-csi500-one-shot-run",
        worker="synthetic-csi500-one-shot-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def _attempt_rows(engine: Engine) -> list[dict[str, object]]:
    """Read only gate rows from this test's temporary database."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT * FROM od_source_attempt_reservations ORDER BY rowid")
        ).mappings()
        return [dict(row) for row in rows]


def _readonly_schema_snapshot_matches(gate: SourceAttemptGate) -> bool:
    """Check the schema through the gate's production read-only SQLite path."""
    connection = gate._open_readonly_connection()
    try:
        return gate._source_gate_snapshot_schema_valid(connection)
    finally:
        connection.close()


def test_real_alembic_migration_marker_matches_the_pinned_schema(
    source_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The registered Alembic migration's SQLite DDL passes the exact schema check."""
    _engine, gate = source_gate

    assert _readonly_schema_snapshot_matches(gate)


def test_legacy_adjacent_parentheses_marker_format_remains_accepted(
    source_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Adjacent PK and table closing parentheses remain a valid marker DDL form."""
    engine, gate = source_gate
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version_source_gate")
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate ( "
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )

    assert _readonly_schema_snapshot_matches(gate)


def test_migration_marker_rejects_a_changed_constraint_name(
    source_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A semantically similar but altered marker DDL still fails closed."""
    engine, gate = source_gate
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version_source_gate")
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate ( "
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT altered_marker_pk PRIMARY KEY (version_num))"
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )

    assert not _readonly_schema_snapshot_matches(gate)


def _run_one_shot(
    monkeypatch: pytest.MonkeyPatch,
    engine: Engine,
    gate: SourceAttemptGate,
    *,
    expected_revision: int = 1,
    observations: dict[str, object] | None = None,
) -> tuple[list[httpx.Request], list[tuple[dict[str, object], int]], object]:
    """Run the actual one-shot job through the real wrapper and injected gate."""
    contract = build_index_daily_contract("000905.SH")
    source_access = job_module.Csi500FuyaoSourceAccess(
        source_attempt_gate=gate,
        source_attempt_binding=SourceAttemptBinding(
            grant_id=_GRANT_ID,
            expected_revision=expected_revision,
            principal_scope=_PRINCIPAL,
        ),
        operator_confirms_user_declared_internal_use=True,
    )
    job = job_module.build_authorized_csi500_index_daily_job(
        DatasetContractRegistry([contract]),
        source_access=source_access,
    )
    payload = job_module.build_csi500_manual_backfill_payload(
        source_access=source_access,
        start_date=_START,
        end_date=_END,
        idempotency_key=f"synthetic-csi500-one-shot-{expected_revision}-001",
    )
    request = RunRequest(run_type="manual", **payload)
    sent: list[httpx.Request] = []
    constructed: list[tuple[dict[str, object], int]] = []

    def handler(outgoing: httpx.Request) -> httpx.Response:
        rows = _attempt_rows(engine)
        assert len(rows) == len(sent) + 1
        row = rows[-1]
        assert row["outcome"] == "RESERVED"
        assert row["attempt_no"] == 1
        assert row["grant_id"] == _GRANT_ID
        assert row["expected_revision"] == 1
        assert row["source"] == _SOURCE
        assert row["canonical_model"] == _MODEL
        assert row["operation"] == _OPERATION
        assert row["principal_scope"] == _PRINCIPAL
        assert row["host"] == _HOST
        assert row["run_key"] == _context().run_key
        assert outgoing.url.path == ths_endpoints.INDEX_PRICES_ENDPOINT
        sent.append(outgoing)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "synthetic-csi500-response",
                "data": {
                    "thscode": "000905.SH",
                    "item": [
                        {
                            "date_ms": ths_endpoints.shanghai_midnight_millis(_START),
                            "open_price": 100.0,
                            "high_price": 102.0,
                            "low_price": 99.0,
                            "close_price": 101.0,
                            "volume": 1_000_000.0,
                            "turnover": 100_000_000.0,
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(
        ths_client_module,
        "credentials",
        lambda: FuyaoCredentials("synthetic-fixture-key", base_url=DEFAULT_FUYAO_API_BASE_URL),
    )
    real_client_class = FuyaoHttpClient

    def client_factory(*, credentials: FuyaoCredentials, **options: object) -> FuyaoHttpClient:
        """Observe wrapper arguments and delegate to the unchanged real client."""
        assert options.get("timeout_seconds") == 30
        assert options.get("source_attempt_gate") is gate
        assert options.get("max_attempts") == 1
        active = real_client_class(
            credentials=credentials,
            mock_transport=httpx.MockTransport(handler),
            rate_limiter=FuyaoRateLimiter(rate_per_second=1_000, burst=10),
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            **options,
        )
        constructed.append((dict(options), active._max_attempts))
        return active

    monkeypatch.setattr(ths_client_module, "FuyaoHttpClient", client_factory)
    if observations is not None:
        observations.update(sent=sent, constructed=constructed)
    raw = job.fetch(request, _context())
    batch = job.normalize(raw, request, _context())
    return sent, constructed, batch


def test_csi500_job_uses_real_wrapper_and_reserves_before_mock_send(
    monkeypatch: pytest.MonkeyPatch,
    source_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The real one-shot path reserves once before its only physical mock send."""
    engine, gate = source_gate
    _provision_grant(gate, evidence_reference=_EXPECTED_EVIDENCE)

    sent, constructed, batch = _run_one_shot(monkeypatch, engine, gate)

    rows = _attempt_rows(engine)
    assert len(sent) == len(rows) == 1
    assert len(constructed) == 1
    assert constructed[0][0]["source_attempt_gate"] is gate
    assert constructed[0][0]["max_attempts"] == 1
    assert constructed[0][1] == 1
    assert batch.provenance["source_history_boundary"] == "UNKNOWN"
    assert batch.provenance["source_rights_basis"] == "USER_DECLARED"


@pytest.mark.parametrize(
    ("evidence_reference", "expected_revision", "denial_code"),
    [
        ("synthetic:test-wrong-evidence-reference", 1, "grant_evidence_reference_mismatch"),
        (_EXPECTED_EVIDENCE, 2, "grant_revision_mismatch"),
    ],
)
def test_csi500_job_denies_mismatched_evidence_or_revision_before_send(
    monkeypatch: pytest.MonkeyPatch,
    source_gate: tuple[Engine, SourceAttemptGate],
    *,
    evidence_reference: str,
    expected_revision: int,
    denial_code: str,
) -> None:
    """The transaction-time gate rejects stale provenance without calling HTTP."""
    engine, gate = source_gate
    _provision_grant(gate, evidence_reference=evidence_reference)

    with pytest.raises(RequestAuthorizationError, match="source attempt gate"):
        observations: dict[str, object] = {}
        sent, constructed, _batch = _run_one_shot(
            monkeypatch,
            engine,
            gate,
            expected_revision=expected_revision,
            observations=observations,
        )

    assert observations["sent"] == []
    constructed = observations["constructed"]
    assert isinstance(constructed, list)
    assert len(constructed) == 1
    assert constructed[0][1] == 1
    rows = _attempt_rows(engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "DENIED"
    assert rows[0]["denial_code"] == denial_code


def test_ordinary_long_backfill_reaches_chunked_provider_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary backfills keep uncapped task windows and split them at the endpoint."""
    start = date(2000, 1, 1)
    end = date(2025, 12, 31)
    contract = build_index_daily_contract("000905.SH")
    job = job_module.build_index_daily_job(
        "000905.SH",
        DatasetContractRegistry([contract]),
    )
    request = job_module.build_index_daily_backfill_request(
        symbol="000905.SH",
        start_date=start,
        end_date=end,
        idempotency_key="synthetic-csi500-long-backfill-001",
    )

    host_grant = RequestGrant(
        source=_SOURCE,
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic-test-only:ordinary-provider-fixture",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=frozenset({_HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(host_grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    requests: list[tuple[int, int]] = []
    real_fetch = ThsIndexDailyFetcher.fetch

    def handler(outgoing: httpx.Request) -> httpx.Response:
        start_ms = int(outgoing.url.params["start"])
        end_ms = int(outgoing.url.params["end"])
        requests.append((start_ms, end_ms))
        items: list[dict[str, object]] = []
        if len(requests) == 3:
            items.append(
                {
                    "date_ms": ths_endpoints.shanghai_midnight_millis(end),
                    "open_price": 100.0,
                    "high_price": 102.0,
                    "low_price": 99.0,
                    "close_price": 101.0,
                    "volume": 1_000_000.0,
                    "turnover": 100_000_000.0,
                }
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": f"synthetic-long-window-{len(requests)}",
                "data": {"thscode": "000905.SH", "item": items},
            },
        )

    monkeypatch.setattr(job_module, "_require_ths_source_authorization", lambda: None)
    monkeypatch.setattr(
        ths_client_module,
        "credentials",
        lambda: FuyaoCredentials("synthetic-fixture-key", base_url=DEFAULT_FUYAO_API_BASE_URL),
    )

    def client_factory(*, credentials: FuyaoCredentials, **options: object) -> FuyaoHttpClient:
        """Use the production client with no network and the test-owned budget."""
        assert "source_attempt_gate" not in options
        assert "max_attempts" not in options
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=httpx.MockTransport(handler),
            rate_limiter=FuyaoRateLimiter(rate_per_second=1_000, burst=10),
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            **options,
        )

    def fetch_with_test_budget(
        fetcher: ThsIndexDailyFetcher,
        *,
        ctx: FetchContext,
        **query: object,
    ) -> object:
        """Supply only the synthetic caller budget missing from the fail-closed job."""
        assert ctx.request_budget is None
        return real_fetch(
            fetcher,
            ctx=replace(ctx, request_budget=budget),
            **query,
        )

    monkeypatch.setattr(ths_client_module, "FuyaoHttpClient", client_factory)
    monkeypatch.setattr(ThsIndexDailyFetcher, "fetch", fetch_with_test_budget)
    raw = job.fetch(
        request,
        RunExecutionContext(
            run_key="synthetic-csi500-long-backfill-run",
            worker="synthetic-csi500-long-backfill-worker",
            fencing_token=1,
            _checkpoint=lambda: None,
        ),
    )
    batch = job.normalize(
        raw,
        request,
        RunExecutionContext(
            run_key="synthetic-csi500-long-backfill-run",
            worker="synthetic-csi500-long-backfill-worker",
            fencing_token=1,
            _checkpoint=lambda: None,
        ),
    )

    assert len(requests) == 3
    assert requests[1][0] == requests[0][1] + 1
    assert requests[2][0] == requests[1][1] + 1
    max_window_ms = ths_endpoints.MAX_WINDOW.days * 86_400_000
    assert all(end_ms >= start_ms for start_ms, end_ms in requests)
    assert all(end_ms - start_ms + 1 <= max_window_ms for start_ms, end_ms in requests)
    assert len(batch.records) == 1
    assert batch.records[0].business_date == end
