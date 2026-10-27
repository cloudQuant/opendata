"""Offline behavior tests for the SQLite source-attempt reservation gate."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.pool import NullPool, QueuePool

from opendata.data.source_attempt_gate import (
    AttemptReservation,
    GrantRevocationStatus,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateConfigurationError,
    SourceAttemptGateUnavailable,
    TransportAttempt,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "source_attempt_gate_migration_0015_test"
QUOTA_KEY = "sina-quota-v1"
SOURCE = "sina"
MODEL = "financial-statement"
PRINCIPAL_SCOPE = "opendata-worker"
HOST = "api.sina.example"
T0_US = 1_700_000_000_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999
MICROTOKENS_PER_TOKEN = 1_000_000


def _load_migration() -> ModuleType:
    """Load only revision 0015 without importing Alembic's application env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path, filename: str = "source-attempt-gate.sqlite") -> Engine:
    """Create an explicitly owned file-backed SQLite engine with independent connections."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )


def _apply_revision(engine: Engine) -> None:
    """Apply only the SQLite migration to the supplied temporary database."""
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


@pytest.fixture
def gate_engine(tmp_path: Path) -> Engine:
    """Yield a migrated database owned by this test under pytest's temporary path."""
    engine = _new_engine(tmp_path)
    _apply_revision(engine)
    yield engine
    engine.dispose()


def _gate(engine: Engine) -> SourceAttemptGate:
    """Construct the gate only with the test-owned explicit SQLite engine."""
    return SourceAttemptGate(engine)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, gate: SourceAttemptGate, now_us: int) -> None:
    """Replace the SQLite clock read with a deterministic value for this gate instance."""
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: now_us)


def _attempt(
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    attempt_no: int = 1,
    *,
    grant_id: str = "grant-1",
    expected_revision: int = 1,
    source: str = SOURCE,
    canonical_model: str = MODEL,
    operation: str = "query",
    principal_scope: str = PRINCIPAL_SCOPE,
    host: str = HOST,
    run_key: str | None = "run-1",
) -> TransportAttempt:
    """Build one bounded transport identity for an offline reservation attempt."""
    return TransportAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=attempt_no,
        grant_id=grant_id,
        expected_revision=expected_revision,
        source=source,
        canonical_model=canonical_model,
        operation=operation,
        principal_scope=principal_scope,
        host=host,
        run_key=run_key,
    )


def _seed_configuration(
    engine: Engine,
    *,
    decision: str = "ALLOWED",
    grant_scopes: tuple[tuple[str, str], ...] = (("grant-1", MODEL),),
    expires_at_us: int = MAX_TIMESTAMP_US,
    rate_microtokens_per_second: int = 1_000_000,
    capacity_microtokens: int = MICROTOKENS_PER_TOKEN,
    tokens_micro: int = MICROTOKENS_PER_TOKEN,
    remainder: int = 0,
    last_refill_us: int = T0_US,
    highwater_us: int | None = None,
    grant_updated_at_us: int = 1,
) -> None:
    """Insert explicit operator-declared test policy, binding, grant, and bucket rows."""
    digest = hashlib.sha256(b"offline-test-evidence").hexdigest()
    with engine.begin() as connection:
        if highwater_us is not None:
            connection.execute(
                sa.text("UPDATE od_source_gate_clock SET highwater_us = :value WHERE clock_id = 1"),
                {"value": highwater_us},
            )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies (quota_key, policy_revision, "
                "rate_microtokens_per_second, capacity_microtokens, policy_reference, "
                "policy_sha256, configured_by, configured_at_us) VALUES "
                "(:quota_key, 1, :rate, :capacity, :reference, :digest, :actor, :configured_at)"
            ),
            {
                "quota_key": QUOTA_KEY,
                "rate": rate_microtokens_per_second,
                "capacity": capacity_microtokens,
                "reference": "test-policy:source-attempt-gate",
                "digest": digest,
                "actor": "offline-test",
                "configured_at": 1,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings (source, quota_key, binding_reference, "
                "binding_sha256, configured_by, configured_at_us) VALUES "
                "(:source, :quota_key, :reference, :digest, :actor, :configured_at)"
            ),
            {
                "source": SOURCE,
                "quota_key": QUOTA_KEY,
                "reference": "test-binding:source-attempt-gate",
                "digest": digest,
                "actor": "offline-test",
                "configured_at": 1,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state (quota_key, tokens_micro, remainder, "
                "last_refill_us, policy_revision) VALUES "
                "(:quota_key, :tokens, :remainder, :refill, 1)"
            ),
            {
                "quota_key": QUOTA_KEY,
                "tokens": tokens_micro,
                "remainder": remainder,
                "refill": last_refill_us,
            },
        )
        for grant_id, model in grant_scopes:
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grants (grant_id, source, canonical_model, operation, "
                    "principal_scope, decision, revision, not_before_us, expires_at_us, "
                    "conditions_json, rights_evidence_reference, rights_evidence_sha256, "
                    "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                    "created_at_us, updated_at_us) VALUES (:grant_id, :source, :model, :operation, "
                    ":principal, :decision, 1, 0, :expires, '[]', :evidence_ref, :evidence_sha, "
                    ":note, :attested_by, 1, NULL, 1, :grant_updated_at)"
                ),
                {
                    "grant_id": grant_id,
                    "source": SOURCE,
                    "model": model,
                    "operation": "query",
                    "principal": PRINCIPAL_SCOPE,
                    "decision": decision,
                    "expires": expires_at_us,
                    "evidence_ref": "test-evidence:source-rights-declaration",
                    "evidence_sha": digest,
                    "note": "Synthetic test declaration; not legal evidence.",
                    "attested_by": "offline-test",
                    "grant_updated_at": grant_updated_at_us,
                },
            )
            if decision == "ALLOWED":
                connection.execute(
                    sa.text(
                        "INSERT INTO od_source_grant_hosts "
                        "(grant_id, host) VALUES (:grant_id, :host)"
                    ),
                    {"grant_id": grant_id, "host": HOST},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO od_source_grant_host_sets "
                        "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                        "VALUES (:grant_id, 1, 1, :sealed_at_us, 'offline-test', 'rights-reviewed')"
                    ),
                    {"grant_id": grant_id, "sealed_at_us": grant_updated_at_us},
                )


def _read_clock(engine: Engine) -> dict[str, object]:
    """Read the clock row from the temporary test database."""
    with engine.connect() as connection:
        row = (
            connection.execute(sa.text("SELECT * FROM od_source_gate_clock WHERE clock_id = 1"))
            .mappings()
            .one()
        )
        return dict(row)


def _read_state(engine: Engine) -> dict[str, object]:
    """Read the single quota bucket row after a reservation decision."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                sa.text("SELECT * FROM od_source_gate_state WHERE quota_key = :key"),
                {"key": QUOTA_KEY},
            )
            .mappings()
            .one()
        )
        return dict(row)


def _read_grant(engine: Engine) -> dict[str, object]:
    """Read the source grant row after revocation."""
    with engine.connect() as connection:
        row = (
            connection.execute(sa.text("SELECT * FROM od_source_grants WHERE grant_id = 'grant-1'"))
            .mappings()
            .one()
        )
        return dict(row)


def _attempt_rows(engine: Engine) -> list[dict[str, object]]:
    """Return durable attempt tombstones from the temporary test database."""
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT * FROM od_source_attempt_reservations ORDER BY attempt_id")
        )
        return [dict(row) for row in rows.mappings().all()]


def _count_grant_events(engine: Engine, event_type: str = "REVOKED") -> int:
    """Count one kind of audit event in the temporary test database."""
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    "SELECT COUNT(*) FROM od_source_grant_events WHERE event_type = :event_type"
                ),
                {"event_type": event_type},
            ).scalar_one()
        )


def _grant_events(engine: Engine) -> list[dict[str, object]]:
    """Read immutable grant audit events from the temporary test database."""
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT * FROM od_source_grant_events ORDER BY event_id"))
        return [dict(row) for row in rows.mappings().all()]


def _read_grant_hosts(engine: Engine, grant_id: str) -> list[str]:
    """Read the exact host set recorded for one synthetic grant."""
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT host FROM od_source_grant_hosts WHERE grant_id = :grant_id ORDER BY host"
            ),
            {"grant_id": grant_id},
        )
        return [str(row["host"]) for row in rows.mappings().all()]


def _revise_policy(engine: Engine, *, capacity_microtokens: int, configured_at_us: int) -> None:
    """Apply one operator-declared policy revision to the test-only policy row."""
    digest = hashlib.sha256(b"offline-test-policy-revision-2").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE od_source_gate_policies SET policy_revision = 2, "
                "capacity_microtokens = :capacity, policy_reference = :reference, "
                "policy_sha256 = :digest, configured_by = :actor, "
                "configured_at_us = :configured_at "
                "WHERE quota_key = :quota_key"
            ),
            {
                "capacity": capacity_microtokens,
                "reference": "test-policy:source-attempt-gate-revision-2",
                "digest": digest,
                "actor": "offline-test-revision-2",
                "configured_at": configured_at_us,
                "quota_key": QUOTA_KEY,
            },
        )


def test_empty_migrated_database_denies_unknown_grant_and_persists_tombstone(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty schema grants nothing, and its denied attempt identity remains spent."""
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert result == AttemptReservation(ReservationStatus.DENIED, "grant_not_found")
    row = _attempt_rows(gate_engine)[0]
    assert (row["outcome"], row["denial_code"], row["reserved_at_us"]) == (
        "DENIED",
        "grant_not_found",
        None,
    )
    assert tuple(
        row[column]
        for column in (
            "quota_key",
            "policy_revision",
            "rate_microtokens_per_second",
            "capacity_microtokens",
        )
    ) == (None, None, None, None)


def test_unknown_grant_decision_fails_closed_without_debiting_bucket(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted UNKNOWN grant cannot authorize transport or consume quota."""
    _seed_configuration(gate_engine, decision="UNKNOWN")
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "grant_not_allowed"
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN
    row = _attempt_rows(gate_engine)[0]
    assert row["outcome"] == "DENIED"
    assert tuple(
        row[column]
        for column in (
            "quota_key",
            "policy_revision",
            "rate_microtokens_per_second",
            "capacity_microtokens",
        )
    ) == (None, None, None, None)


def test_denial_tombstone_stays_non_permitting_after_configuration_is_added(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding later policy cannot turn a previous denied identity into a permit."""
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)
    attempt = _attempt()

    first = gate.reserve_attempt(attempt)
    assert first.status is ReservationStatus.DENIED
    _seed_configuration(gate_engine)

    replay = gate.reserve_attempt(attempt)

    assert replay.status is ReservationStatus.DUPLICATE
    row = _attempt_rows(gate_engine)[0]
    assert (row["outcome"], row["denial_code"]) == ("DENIED", "grant_not_found")
    assert tuple(
        row[column]
        for column in (
            "quota_key",
            "policy_revision",
            "rate_microtokens_per_second",
            "capacity_microtokens",
        )
    ) == (None, None, None, None)
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN


def test_allowed_grant_without_source_binding_has_no_policy_snapshot(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An authorized source without an explicit binding is denied with a NULL snapshot."""
    digest = hashlib.sha256(b"offline-test-rights-evidence").hexdigest()
    with gate_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants (grant_id, source, canonical_model, operation, "
                "principal_scope, decision, revision, not_before_us, expires_at_us, "
                "conditions_json, rights_evidence_reference, rights_evidence_sha256, "
                "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                "created_at_us, updated_at_us) VALUES ('grant-unbound', :source, :model, "
                "'query', :principal, 'ALLOWED', 1, 0, :expires, '[]', 'test-rights', "
                ":digest, 'Synthetic declaration only.', 'offline-test', 1, NULL, 1, 1)"
            ),
            {
                "source": "sina-unbound",
                "model": MODEL,
                "principal": PRINCIPAL_SCOPE,
                "expires": MAX_TIMESTAMP_US,
                "digest": digest,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES ('grant-unbound', :host)"
            ),
            {"host": HOST},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES ('grant-unbound', 1, 1, 1, 'offline-test', 'rights-reviewed')"
            )
        )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(
        _attempt(
            "attempt-unbound",
            "request-unbound",
            grant_id="grant-unbound",
            source="sina-unbound",
        )
    )

    row = _attempt_rows(gate_engine)[0]
    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_quota_unconfigured",
    )
    assert tuple(
        row[column]
        for column in (
            "quota_key",
            "policy_revision",
            "rate_microtokens_per_second",
            "capacity_microtokens",
        )
    ) == (None, None, None, None)


def test_sealed_host_set_rejects_unlogged_raw_host_expansion(gate_engine: Engine) -> None:
    """A second raw host cannot expand a sealed ALLOWED grant without an audit step."""
    _seed_configuration(gate_engine)
    before_hosts = _read_grant_hosts(gate_engine, "grant-1")
    before_events = _grant_events(gate_engine)
    assert before_hosts == [HOST]
    assert [event["event_type"] for event in before_events] == ["HOSTS_SEALED"]

    with (
        pytest.raises(sa.exc.IntegrityError),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_hosts (grant_id, host) "
                "VALUES ('grant-1', 'extra.sina.example')"
            )
        )

    assert _read_grant_hosts(gate_engine, "grant-1") == before_hosts
    assert _grant_events(gate_engine) == before_events


def test_host_set_seal_rejects_a_declared_count_that_does_not_match_rows(
    gate_engine: Engine,
) -> None:
    """A host seal cannot attest a count different from the persisted grant hosts."""
    _seed_configuration(gate_engine)
    digest = hashlib.sha256(b"offline-test-unsealed-grant").hexdigest()
    with gate_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants (grant_id, source, canonical_model, operation, "
                "principal_scope, decision, revision, not_before_us, expires_at_us, "
                "conditions_json, rights_evidence_reference, rights_evidence_sha256, "
                "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                "created_at_us, updated_at_us) VALUES ('grant-bad-seal', :source, :model, "
                "'query', :principal, 'ALLOWED', 1, 0, :expires, '[]', 'test-rights', "
                ":digest, 'Synthetic declaration only.', 'offline-test', 1, NULL, 1, 1)"
            ),
            {
                "source": SOURCE,
                "model": MODEL,
                "principal": PRINCIPAL_SCOPE,
                "expires": MAX_TIMESTAMP_US,
                "digest": digest,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_hosts (grant_id, host) "
                "VALUES ('grant-bad-seal', :host)"
            ),
            {"host": HOST},
        )
    before_events = _grant_events(gate_engine)

    with (
        pytest.raises(sa.exc.IntegrityError),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES ('grant-bad-seal', 1, 2, 1, 'offline-test', 'rights-reviewed')"
            )
        )

    with gate_engine.connect() as connection:
        seal_count = connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM od_source_grant_host_sets WHERE grant_id = 'grant-bad-seal'"
            )
        ).scalar_one()
    assert seal_count == 0
    assert _grant_events(gate_engine) == before_events


def test_attempt_id_and_request_attempt_number_are_both_idempotency_keys(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exact replay is never a permit; collisions on either unique key conflict."""
    _seed_configuration(gate_engine)
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)
    original = _attempt()

    first = gate.reserve_attempt(original)
    replay = gate.reserve_attempt(original)
    reused_attempt_id = gate.reserve_attempt(_attempt("attempt-1", "different-request", 1))
    reused_request_number = gate.reserve_attempt(_attempt("different-attempt", "request-1", 1))

    assert first.status is ReservationStatus.RESERVED_NEW
    assert replay.status is ReservationStatus.DUPLICATE
    assert reused_attempt_id.status is ReservationStatus.ID_CONFLICT
    assert reused_request_number.status is ReservationStatus.ID_CONFLICT
    assert len(_attempt_rows(gate_engine)) == 1
    assert _read_state(gate_engine)["tokens_micro"] == 0


def test_two_independent_sqlite_connections_cannot_overspend_one_token(
    gate_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two file-backed connections serialize reservations against the same bucket."""
    _seed_configuration(gate_engine)
    second_engine = _new_engine(tmp_path)
    first_gate = _gate(gate_engine)
    second_gate = _gate(second_engine)
    _freeze_clock(monkeypatch, first_gate, T0_US)
    _freeze_clock(monkeypatch, second_gate, T0_US)
    barrier = Barrier(2)
    attempts = (_attempt("attempt-a", "request-a"), _attempt("attempt-b", "request-b"))

    def reserve_after_barrier(
        gate: SourceAttemptGate, attempt: TransportAttempt
    ) -> AttemptReservation:
        barrier.wait(timeout=10)
        return gate.reserve_attempt(attempt)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(reserve_after_barrier, first_gate, attempts[0]),
                executor.submit(reserve_after_barrier, second_gate, attempts[1]),
            ]
            results = [future.result(timeout=20) for future in futures]
    finally:
        second_engine.dispose()

    assert sorted(result.status.value for result in results) == ["DENIED", "RESERVED_NEW"]
    denied = next(result for result in results if result.status is ReservationStatus.DENIED)
    assert denied.denial_code == "source_rate_limited"
    assert len(_attempt_rows(gate_engine)) == 2
    assert _read_state(gate_engine)["tokens_micro"] == 0


def test_distinct_grants_for_one_source_share_the_bound_quota_bucket(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different grant/model scopes cannot bypass a quota bound once per source."""
    second_model = "financial-statement-v2"
    _seed_configuration(
        gate_engine,
        grant_scopes=(("grant-1", MODEL), ("grant-2", second_model)),
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    first = gate.reserve_attempt(_attempt())
    second = gate.reserve_attempt(
        _attempt(
            "attempt-2",
            "request-2",
            grant_id="grant-2",
            canonical_model=second_model,
        )
    )

    assert first.status is ReservationStatus.RESERVED_NEW
    assert (second.status, second.denial_code) == (
        ReservationStatus.DENIED,
        "source_rate_limited",
    )
    assert _read_state(gate_engine)["tokens_micro"] == 0
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 2
    for row in rows:
        assert (
            row["quota_key"],
            row["policy_revision"],
            row["rate_microtokens_per_second"],
            row["capacity_microtokens"],
        ) == (QUOTA_KEY, 1, 1_000_000, MICROTOKENS_PER_TOKEN)


def test_token_bucket_refill_preserves_exact_microtoken_remainder(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fractional refill survives repeated denial and capacity saturation resets it."""
    _seed_configuration(
        gate_engine,
        rate_microtokens_per_second=3,
        capacity_microtokens=MICROTOKENS_PER_TOKEN,
        tokens_micro=0,
        remainder=0,
        last_refill_us=T0_US,
    )
    gate = _gate(gate_engine)
    times = iter((T0_US + 333_333, T0_US + 333_334, T0_US + 400_000_000_000))
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: next(times))

    first = gate.reserve_attempt(_attempt("attempt-a", "request-a"))
    state_after_first = _read_state(gate_engine)
    second = gate.reserve_attempt(_attempt("attempt-b", "request-b"))
    state_after_second = _read_state(gate_engine)
    third = gate.reserve_attempt(_attempt("attempt-c", "request-c"))
    state_after_third = _read_state(gate_engine)

    assert (first.status, first.denial_code, first.retry_after_us) == (
        ReservationStatus.DENIED,
        "source_rate_limited",
        333_333_000_001,
    )
    assert (state_after_first["tokens_micro"], state_after_first["remainder"]) == (0, 999_999)
    assert (second.status, second.denial_code) == (
        ReservationStatus.DENIED,
        "source_rate_limited",
    )
    assert (state_after_second["tokens_micro"], state_after_second["remainder"]) == (1, 2)
    assert third.status is ReservationStatus.RESERVED_NEW
    assert (state_after_third["tokens_micro"], state_after_third["remainder"]) == (0, 0)
    assert state_after_third["last_refill_us"] == T0_US + 400_000_000_000


def test_policy_revision_clamps_old_balance_and_resets_fractional_refill_state(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new policy caps old balance before debit and starts refill at its effective time."""
    effective_now_us = T0_US + 1_000
    _seed_configuration(
        gate_engine,
        capacity_microtokens=3 * MICROTOKENS_PER_TOKEN,
        tokens_micro=2_500_000,
        remainder=987_654,
        last_refill_us=T0_US,
    )
    _revise_policy(
        gate_engine,
        capacity_microtokens=2 * MICROTOKENS_PER_TOKEN,
        configured_at_us=1,
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, effective_now_us)

    result = gate.reserve_attempt(_attempt())

    state = _read_state(gate_engine)
    assert result.status is ReservationStatus.RESERVED_NEW
    assert (
        state["tokens_micro"],
        state["remainder"],
        state["last_refill_us"],
        state["policy_revision"],
    ) == (MICROTOKENS_PER_TOKEN, 0, effective_now_us, 2)
    attempt_row = _attempt_rows(gate_engine)[0]
    assert (
        attempt_row["quota_key"],
        attempt_row["policy_revision"],
        attempt_row["rate_microtokens_per_second"],
        attempt_row["capacity_microtokens"],
    ) == (QUOTA_KEY, 2, 1_000_000, 2 * MICROTOKENS_PER_TOKEN)


def test_policy_revision_does_not_retroactively_refill_old_bucket_state(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy change resets old fractional time before evaluating the new attempt."""
    effective_now_us = T0_US + 1_000_000
    _seed_configuration(
        gate_engine,
        rate_microtokens_per_second=MICROTOKENS_PER_TOKEN,
        tokens_micro=0,
        remainder=999_999,
        last_refill_us=T0_US,
    )
    _revise_policy(
        gate_engine,
        capacity_microtokens=MICROTOKENS_PER_TOKEN,
        configured_at_us=1,
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, effective_now_us)

    result = gate.reserve_attempt(_attempt())

    state = _read_state(gate_engine)
    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_rate_limited",
    )
    assert (
        state["tokens_micro"],
        state["remainder"],
        state["last_refill_us"],
        state["policy_revision"],
    ) == (
        0,
        0,
        effective_now_us,
        2,
    )
    attempt_row = _attempt_rows(gate_engine)[0]
    assert (
        attempt_row["quota_key"],
        attempt_row["policy_revision"],
        attempt_row["rate_microtokens_per_second"],
        attempt_row["capacity_microtokens"],
    ) == (QUOTA_KEY, 2, MICROTOKENS_PER_TOKEN, MICROTOKENS_PER_TOKEN)


def test_policy_snapshot_survives_missing_bucket_state(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated policy is snapshotted even when its quota state is unavailable."""
    _seed_configuration(gate_engine)
    with gate_engine.begin() as connection:
        connection.execute(
            sa.text("DELETE FROM od_source_gate_state WHERE quota_key = :key"),
            {"key": QUOTA_KEY},
        )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    row = _attempt_rows(gate_engine)[0]
    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_quota_state_unavailable",
    )
    assert (
        row["quota_key"],
        row["policy_revision"],
        row["rate_microtokens_per_second"],
        row["capacity_microtokens"],
    ) == (QUOTA_KEY, 1, 1_000_000, MICROTOKENS_PER_TOKEN)


def test_partial_policy_snapshot_is_rejected_by_database_check(gate_engine: Engine) -> None:
    """The database rejects an attempt row with only part of its policy snapshot."""
    _seed_configuration(gate_engine)

    with (
        pytest.raises(sa.exc.IntegrityError, match="CHECK constraint failed"),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "INSERT INTO od_source_attempt_reservations "
                "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
                "canonical_model, operation, principal_scope, host, run_key, outcome, "
                "denial_code, retry_after_us, decided_at_us, reserved_at_us, quota_key, "
                "policy_revision, rate_microtokens_per_second, capacity_microtokens) VALUES "
                "('attempt-partial', 'request-partial', 1, 'grant-1', 1, 'sina', :model, "
                "'query', :principal, :host, NULL, 'DENIED', 'policy_test', NULL, 1, NULL, "
                ":quota_key, 1, :rate, NULL)"
            ),
            {
                "model": MODEL,
                "principal": PRINCIPAL_SCOPE,
                "host": HOST,
                "quota_key": QUOTA_KEY,
                "rate": 1_000_000,
            },
        )


def test_attempt_tombstone_policy_snapshot_is_immutable(gate_engine: Engine) -> None:
    """A raw update cannot rewrite the policy evidence captured with an attempt."""
    _seed_configuration(gate_engine)
    result = _gate(gate_engine).reserve_attempt(_attempt())
    assert result.status is ReservationStatus.RESERVED_NEW
    before = _attempt_rows(gate_engine)[0]

    with (
        pytest.raises(sa.exc.IntegrityError, match="immutable"),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE od_source_attempt_reservations SET policy_revision = 2 "
                "WHERE attempt_id = 'attempt-1'"
            )
        )

    assert _attempt_rows(gate_engine)[0] == before


def test_grant_expiry_is_exclusive_at_the_exact_expiration_time(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At expires_at_us the grant has expired and the denied decision is recorded."""
    expires_at_us = T0_US + 10
    _seed_configuration(gate_engine, expires_at_us=expires_at_us)
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, expires_at_us)

    result = gate.reserve_attempt(_attempt())

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "grant_expired"
    assert _attempt_rows(gate_engine)[0]["outcome"] == "DENIED"
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN


def test_revocation_increments_revision_and_blocks_new_reservations(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed revocation writes one audit event and rejects later attempts."""
    _seed_configuration(
        gate_engine,
        capacity_microtokens=2 * MICROTOKENS_PER_TOKEN,
        tokens_micro=2 * MICROTOKENS_PER_TOKEN,
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)
    first = gate.reserve_attempt(_attempt())

    revoked = gate.revoke_grant(
        "grant-1", expected_revision=1, actor="operator-1", reason_code="rights-withdrawn"
    )
    repeated_revoke = gate.revoke_grant(
        "grant-1", expected_revision=2, actor="operator-1", reason_code="rights-withdrawn"
    )
    later = gate.reserve_attempt(_attempt("attempt-2", "request-2"))
    grant = _read_grant(gate_engine)
    event_row_count = _count_grant_events(gate_engine)

    assert first.status is ReservationStatus.RESERVED_NEW
    assert (revoked.status, revoked.revision) == (GrantRevocationStatus.REVOKED, 2)
    assert (repeated_revoke.status, repeated_revoke.revision) == (
        GrantRevocationStatus.ALREADY_REVOKED,
        2,
    )
    assert (later.status, later.denial_code) == (ReservationStatus.DENIED, "grant_not_allowed")
    assert (grant["decision"], grant["revision"], grant["revoked_at_us"]) == (
        "DENIED",
        2,
        T0_US,
    )
    assert event_row_count == 1


def test_raw_sql_revoke_writes_exactly_one_audit_event(gate_engine: Engine) -> None:
    """The database trigger audits a valid direct ALLOWED-to-DENIED update once."""
    _seed_configuration(gate_engine)
    revoked_at_us = T0_US + 100

    with gate_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE od_source_grants SET decision = 'DENIED', revision = 2, "
                "revoked_at_us = :now_us, revocation_actor = :actor, "
                "revocation_reason_code = :reason, updated_at_us = :now_us "
                "WHERE grant_id = 'grant-1'"
            ),
            {"now_us": revoked_at_us, "actor": "raw-operator", "reason": "rights-withdrawn"},
        )

    events = _grant_events(gate_engine)
    grant = _read_grant(gate_engine)
    revoke_events = [event for event in events if event["event_type"] == "REVOKED"]
    seal_events = [event for event in events if event["event_type"] == "HOSTS_SEALED"]
    assert len(revoke_events) == 1
    assert len(seal_events) == 1
    assert (
        revoke_events[0]["event_type"],
        revoke_events[0]["old_revision"],
        revoke_events[0]["new_revision"],
        revoke_events[0]["actor"],
        revoke_events[0]["reason_code"],
        revoke_events[0]["occurred_at_us"],
    ) == ("REVOKED", 1, 2, "raw-operator", "rights-withdrawn", revoked_at_us)
    assert (grant["decision"], grant["revision"], grant["revocation_actor"]) == (
        "DENIED",
        2,
        "raw-operator",
    )


def test_raw_sql_revoke_cannot_clear_nullable_rights_or_attestation_evidence(
    gate_engine: Engine,
) -> None:
    """A raw revoke that clears evidence is rejected without changing grant or events."""
    _seed_configuration(gate_engine)
    before = _read_grant(gate_engine)
    before_events = _grant_events(gate_engine)

    with (
        pytest.raises(sa.exc.IntegrityError),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            sa.text(
                "UPDATE od_source_grants SET decision = 'DENIED', revision = 2, "
                "revoked_at_us = :now_us, revocation_actor = 'raw-operator', "
                "revocation_reason_code = 'rights-withdrawn', updated_at_us = :now_us, "
                "rights_evidence_reference = NULL, rights_evidence_sha256 = NULL, "
                "operator_attestation_note = NULL, attested_by = NULL, attested_at_us = NULL "
                "WHERE grant_id = 'grant-1'"
            ),
            {"now_us": T0_US + 100},
        )

    after = _read_grant(gate_engine)
    assert after == before
    assert _grant_events(gate_engine) == before_events


def test_clock_rollback_cannot_extend_an_already_expired_grant(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted database-time high water wins when the SQLite clock moves backward."""
    highwater_us = T0_US + 100
    _seed_configuration(
        gate_engine,
        expires_at_us=highwater_us,
        highwater_us=highwater_us,
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (ReservationStatus.DENIED, "grant_expired")
    assert _read_clock(gate_engine)["highwater_us"] == highwater_us
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN


def test_clock_rollback_does_not_refill_or_rewind_bucket_state(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backward clock read leaves refill time and fractional remainder monotonic."""
    highwater_us = T0_US + 100
    _seed_configuration(
        gate_engine,
        expires_at_us=highwater_us + 10,
        rate_microtokens_per_second=MICROTOKENS_PER_TOKEN,
        tokens_micro=0,
        remainder=123_456,
        last_refill_us=highwater_us,
        highwater_us=highwater_us,
    )
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    state = _read_state(gate_engine)
    assert (result.status, result.denial_code, result.retry_after_us) == (
        ReservationStatus.DENIED,
        "source_rate_limited",
        1_000_000,
    )
    assert (state["tokens_micro"], state["remainder"], state["last_refill_us"]) == (
        0,
        123_456,
        highwater_us,
    )
    assert _read_clock(gate_engine)["highwater_us"] == highwater_us


@pytest.mark.parametrize("updated_at_us", (T0_US + 1, MAX_TIMESTAMP_US))
def test_future_grant_updated_at_denies_and_revoke_fails_closed(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    updated_at_us: int,
) -> None:
    """A grant timestamp beyond effective DB time cannot authorize or be revoked as success."""
    _seed_configuration(gate_engine, grant_updated_at_us=updated_at_us)
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (ReservationStatus.DENIED, "grant_invalid")
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN
    before_grant = _read_grant(gate_engine)
    before_events = _grant_events(gate_engine)

    with pytest.raises(SourceAttemptGateUnavailable):
        gate.revoke_grant(
            "grant-1", expected_revision=1, actor="operator-1", reason_code="rights-withdrawn"
        )

    assert _read_grant(gate_engine) == before_grant
    assert _grant_events(gate_engine) == before_events
    assert _count_grant_events(gate_engine, "REVOKED") == 0


def test_grant_updated_at_beyond_supported_range_is_rejected_by_database(
    gate_engine: Engine,
) -> None:
    """The persisted grant timestamp constraint rejects values beyond the UTC range."""
    with pytest.raises(sa.exc.IntegrityError, match="CHECK constraint failed"):
        _seed_configuration(gate_engine, grant_updated_at_us=MAX_TIMESTAMP_US + 1)


def test_database_error_rolls_back_clock_bucket_and_attempt_row(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed attempt insert raises unavailable and rolls all prior writes back."""
    _seed_configuration(gate_engine)
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)
    before_clock = _read_clock(gate_engine)["highwater_us"]

    def fail_attempt_insert(
        _connection: Connection,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "INSERT INTO OD_SOURCE_ATTEMPT_RESERVATIONS" in statement.upper():
            raise sa.exc.OperationalError(statement, parameters, RuntimeError("injected failure"))

    event.listen(gate_engine, "before_cursor_execute", fail_attempt_insert)
    try:
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(_attempt())
    finally:
        event.remove(gate_engine, "before_cursor_execute", fail_attempt_insert)

    assert _attempt_rows(gate_engine) == []
    assert _read_state(gate_engine)["tokens_micro"] == MICROTOKENS_PER_TOKEN
    assert _read_clock(gate_engine)["highwater_us"] == before_clock


def test_unknown_commit_ack_never_returns_a_reusable_reservation(
    gate_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost commit acknowledgement raises unavailable; replay returns only DUPLICATE."""
    _seed_configuration(gate_engine)
    gate = _gate(gate_engine)
    _freeze_clock(monkeypatch, gate, T0_US)
    attempt = _attempt()

    def commit_then_lose_ack(connection: Connection) -> None:
        connection.commit()
        raise SourceAttemptGateUnavailable("injected lost commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(gate, "_commit", commit_then_lose_ack)
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(attempt)

    replay = gate.reserve_attempt(attempt)

    assert replay.status is ReservationStatus.DUPLICATE
    row = _attempt_rows(gate_engine)[0]
    assert row["outcome"] == "RESERVED"
    assert _read_state(gate_engine)["tokens_micro"] == 0


def test_non_sqlite_engine_is_rejected_before_use() -> None:
    """The public gate constructor fails closed for unverified database dialects."""
    engine = sa.create_engine("sqlite://")
    engine.dialect.name = "postgresql"
    with pytest.raises(SourceAttemptGateConfigurationError, match="SQLite only"):
        SourceAttemptGate(engine)
    engine.dispose()


@pytest.mark.parametrize(
    "url",
    (
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file::memory:?cache=shared&uri=true",
        "sqlite:///temporary.sqlite?mode=MEMORY&cache=shared",
    ),
)
def test_gate_rejects_memory_backed_sqlite_urls(url: str) -> None:
    """The source gate requires durable SQLite storage rather than process memory."""
    engine = sa.create_engine(url)
    try:
        with pytest.raises(SourceAttemptGateConfigurationError):
            SourceAttemptGate(engine)
    finally:
        engine.dispose()


def test_gate_accepts_an_explicit_file_backed_sqlite_url(tmp_path: Path) -> None:
    """A normal temporary SQLite file URL satisfies the durable-storage guard."""
    engine = _new_engine(tmp_path, "durable-gate.sqlite")
    try:
        _apply_revision(engine)
        assert SourceAttemptGate(engine)._engine is engine
    finally:
        engine.dispose()


@pytest.mark.parametrize("creator_target", ("memory", "other-file"))
def test_gate_rejects_file_url_whose_creator_opens_another_database(
    tmp_path: Path,
    creator_target: str,
) -> None:
    """A plausible file URL cannot authorize writes to memory or a different SQLite file."""
    expected_path = tmp_path / "declared-source-gate.sqlite"
    expected_path.touch()
    actual_path = tmp_path / "creator-source-gate.sqlite"

    def custom_creator() -> sqlite3.Connection:
        if creator_target == "memory":
            return sqlite3.connect(":memory:")
        return sqlite3.connect(actual_path)

    engine = sa.create_engine(
        f"sqlite:///{expected_path}",
        creator=custom_creator,
        poolclass=QueuePool,
    )
    try:
        gate = SourceAttemptGate(engine)
        _apply_revision(engine)
        _seed_configuration(engine)
        before_state = _read_state(engine)
        before_grant = _read_grant(engine)
        before_events = _grant_events(engine)

        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(_attempt())

        assert _attempt_rows(engine) == []
        assert _read_state(engine) == before_state
        assert _read_grant(engine) == before_grant
        assert _grant_events(engine) == before_events
    finally:
        engine.dispose()
