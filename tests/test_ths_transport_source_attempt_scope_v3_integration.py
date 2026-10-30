"""Offline integration of the THS V3 transport with its SQLite attempt gate.

The test uses synthetic grant and budget rows plus an HTTPX ``MockTransport``.
It verifies transport wiring and durable target identity only; it is not
evidence of a supplier authorization or a per-run lifetime cap.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import V2_OBJECT_SQL, V3_OBJECT_SQL, V3_REVISION
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V3,
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import (
    AttemptLookupReserved,
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptScopeV3,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Engine

SOURCE = "ths"
MODEL = "index_daily"
PRINCIPAL = "fixture-csi500-v3-worker"
RUN_KEY = "fixture-csi500-v3-integration-run"
GRANT_ID = "fixture-csi500-v3-grant"
RIGHTS_REFERENCE = "offline:synthetic-rights-declaration"
QUOTA_KEY = "fixture-csi500-v3-quota"
HOST = "fuyao.aicubes.cn"
PATH = "/api/a-share-index/prices/historical"
QUERY_FIELDS = ("end", "interval", "start", "thscode")
QUERY = {
    "end": "1793203199999",
    "interval": "1d",
    "start": "1793116800000",
    "thscode": "000905.SH",
}
NOW_US = 1_700_000_000_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999
BASE_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic_data"
    / "versions"
    / "20261027-0015_source_attempt_gate.py"
)


def _load_base_migration() -> ModuleType:
    """Load the repository-owned base schema migration without app settings."""
    spec = importlib.util.spec_from_file_location("ths_v3_integration_base", BASE_MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_gate_engine(tmp_path: Path) -> Engine:
    """Create a file-backed SQLite engine accepted by the durable gate."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / 'ths-v3-attempt-gate.sqlite'}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )


def _install_gate_schema(engine: Engine) -> None:
    """Install the repository base schema and exact V2/V3 scope objects."""
    migration = _load_base_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate ( "
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version_source_gate (version_num) "
            "VALUES ('sg0002_source_attempt_scope_v2')"
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)
        for statement in V3_OBJECT_SQL:
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V3_REVISION},
        )


def _pin(query: dict[str, str]) -> SourceAttemptScopeV3Pin:
    """Create explicit synthetic pins for one deterministic transport query."""
    return SourceAttemptScopeV3Pin(
        capture_intent_id="fixture-csi500-v3-intent",
        request_summary_sha256=request_summary_sha256(query, allowed_fields=QUERY_FIELDS),
        request_summary_allowed_fields=QUERY_FIELDS,
        expected_https_host=HOST,
        expected_path=PATH,
        expected_method="GET",
        source_policy_id="fixture-csi500-source-policy",
        source_policy_revision="fixture-1",
        source_policy_sha256="a" * 64,
        budget_binding_sha256="b" * 64,
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256="c" * 64,
    )


def _binding() -> SourceAttemptBinding:
    """Return a synthetic grant binding for the direct transport scope."""
    return SourceAttemptBinding(
        grant_id=GRANT_ID,
        expected_revision=1,
        principal_scope=PRINCIPAL,
        run_key=RUN_KEY,
        expected_rights_evidence_reference=RIGHTS_REFERENCE,
    )


def _local_budget() -> RequestBudget:
    """Build a one-request in-memory grant matching the direct transport scope."""
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=RIGHTS_REFERENCE,
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )


def _seed_matching_grant_and_quota(engine: Engine) -> None:
    """Seed synthetic SQLite rows matching the trusted binding and budget pins."""
    evidence_sha256 = hashlib.sha256(b"synthetic integration fixture").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota_key, 1, 1000000, 1000000, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota_key": QUOTA_KEY,
                "reference": "offline:synthetic-policy",
                "digest": "c" * 64,
                "actor": "offline-test",
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota_key, :reference, :digest, :actor, 1)"
            ),
            {
                "source": SOURCE,
                "quota_key": QUOTA_KEY,
                "reference": "offline:synthetic-source-binding",
                "digest": "b" * 64,
                "actor": "offline-test",
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota_key, 1000000, 0, :refill, 1)"
            ),
            {"quota_key": QUOTA_KEY, "refill": NOW_US},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant_id, :source, :model, 'query', :principal, 'ALLOWED', 1, 0, "
                ":expires, '[]', :reference, :digest, :note, :actor, 1, 1, 1)"
            ),
            {
                "grant_id": GRANT_ID,
                "source": SOURCE,
                "model": MODEL,
                "principal": PRINCIPAL,
                "expires": MAX_TIMESTAMP_US,
                "reference": RIGHTS_REFERENCE,
                "digest": evidence_sha256,
                "note": "Synthetic fixture; not legal evidence.",
                "actor": "offline-test",
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"),
            {"grant_id": GRANT_ID, "host": HOST},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant_id, 1, 1, 1, 'offline-test', 'synthetic-test')"
            ),
            {"grant_id": GRANT_ID},
        )


def test_v3_transport_commits_real_gate_scope_before_mock_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct V3 send follows a committed SQLite reservation and exact sidecar."""
    engine = _new_gate_engine(tmp_path)
    _install_gate_schema(engine)
    gate = SourceAttemptGate(engine)
    _seed_matching_grant_and_quota(engine)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    query = dict(QUERY)
    pin = _pin(query)
    binding = _binding()
    budget = _local_budget()
    observed_scopes: list[SourceAttemptScopeV3] = []
    observed_reservations: list[AttemptReservation] = []
    real_reserve = gate.reserve_attempt_v3

    def reserve_v3(scope: SourceAttemptScopeV3) -> AttemptReservation:
        observed_scopes.append(scope)
        reservation = real_reserve(scope)
        observed_reservations.append(reservation)
        return reservation

    monkeypatch.setattr(gate, "reserve_attempt_v3", reserve_v3)

    def forbid_v1(_self: SourceAttemptGate, _attempt: object) -> AttemptReservation:
        pytest.fail("V3 transport fell back to the V1 attempt gate")

    monkeypatch.setattr(SourceAttemptGate, "reserve_attempt", forbid_v1)
    sent_requests: list[httpx.Request] = []
    observed_sidecars: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        with engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT r.attempt_id, r.outcome, r.host, s.scope_json, s.scope_sha256 "
                        "FROM od_source_attempt_reservations AS r "
                        "JOIN od_source_attempt_scope_v3 AS s USING (attempt_id)"
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "RESERVED"
        assert row["host"] == HOST
        document = json.loads(row["scope_json"])
        observed_sidecars.append(document)
        assert document["attempt_id"] == row["attempt_id"]
        assert document["scope_version"] == 3
        assert document["expected_rights_evidence_reference"] == RIGHTS_REFERENCE
        assert document["request_scheme"] == "https"
        assert document["request_method"] == "GET"
        assert document["request_host"] == HOST
        assert document["request_path"] == PATH
        assert document["request_summary_sha256"] == pin.request_summary_sha256
        assert request.method == document["request_method"]
        assert request.url.scheme == document["request_scheme"]
        assert request.url.host == document["request_host"]
        assert request.url.path == document["request_path"]
        actual_query = dict(request.url.params.multi_items())
        assert actual_query == query
        assert (
            request_summary_sha256(actual_query, allowed_fields=QUERY_FIELDS)
            == document["request_summary_sha256"]
        )
        assert hashlib.sha256(row["scope_json"].encode("utf-8")).hexdigest() == row["scope_sha256"]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "synthetic-provider-request",
                "data": {"item": []},
            },
        )

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
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
            source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
            source_attempt_scope_v3_pin=pin,
        ):
            response = active.get(PATH, params=query)
    finally:
        active.close()

    assert response.status_code == 200
    assert response.envelope.code == 0
    assert len(sent_requests) == 1
    assert len(observed_scopes) == 1
    assert len(observed_reservations) == 1
    assert observed_reservations[0].status is ReservationStatus.RESERVED_NEW
    assert len(observed_sidecars) == 1
    assert budget.attempts_used == 1
    assert budget.attempts_used_for(SOURCE) == 1
    lookup = gate.lookup_attempt_v3(observed_scopes[0])
    assert isinstance(lookup, AttemptLookupReserved)
    assert lookup.attempt_id == observed_scopes[0].attempt.attempt_id
    assert observed_scopes[0].request_path == PATH
    assert observed_scopes[0].request_summary_sha256 == pin.request_summary_sha256
    engine.dispose()
