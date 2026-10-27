"""Boundary behavior for persisted source-gate authorization and rate policy."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.pool import NullPool

from opendata.data.source_attempt_gate import (
    GrantRevocationStatus,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from sqlalchemy.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
REVISION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
T0_US = 1_700_000_000_000_000
FAR_FUTURE_US = 253_402_300_799_999_999
TOKEN_MICRO = 1_000_000
QUOTA_KEY = "sina-quota-v1"
SOURCE = "sina"
MODEL = "financial-statement"
PRINCIPAL = "opendata-worker"
HOST = "api.sina.example"


def _load_revision() -> Any:
    """Load the one SQLite revision without importing the Alembic application env."""
    spec = importlib.util.spec_from_file_location("source_gate_boundary_revision", REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(engine: Engine) -> None:
    """Create the isolated gate schema in the test-owned SQLite file."""
    revision = _load_revision()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.upgrade()


@pytest.fixture
def gate_db(tmp_path: Path) -> Engine:
    """Provide a disposable SQLite file with only the source-gate revision applied."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'source-gate-boundaries.sqlite'}",
        poolclass=NullPool,
        connect_args={"timeout": 10},
    )
    try:
        _migrate(engine)
        yield engine
    finally:
        engine.dispose()


def _seed(
    engine: Engine,
    *,
    conditions_json: str = "[]",
    source: str = SOURCE,
    model: str = MODEL,
    operation: str = "query",
    principal: str = PRINCIPAL,
    hosts: tuple[str, ...] = (HOST,),
    attested_by: str = "offline-test",
    attested_at_us: int = 1,
    grant_revision: int = 1,
    not_before_us: int = 0,
    expires_at_us: int = FAR_FUTURE_US,
    decision: str = "ALLOWED",
    policy_revision: int = 1,
    policy_configured_by: str = "offline-test",
    policy_configured_at_us: int = 1,
    rate: int = TOKEN_MICRO,
    capacity: int = TOKEN_MICRO,
    tokens: int = TOKEN_MICRO,
    state_revision: int = 1,
    last_refill_us: int = T0_US,
    binding_configured_by: str = "offline-test",
    binding_configured_at_us: int = 1,
    state_remainder: int = 0,
    ignore_check_constraints: bool = False,
    policy_reference: str = "synthetic:boundary-test",
    policy_sha256: str | None = None,
    binding_reference: str = "synthetic:boundary-binding",
    binding_sha256: str | None = None,
    rights_evidence_reference: str = "synthetic-evidence",
    rights_evidence_sha256: str | None = None,
    operator_attestation_note: str = "Synthetic declaration; not legal evidence.",
    grant_created_at_us: int = 1,
    grant_updated_at_us: int = 1,
    revoked_at_us: int | None = None,
    seal_at_us: int | None = None,
    seal_by: str = "offline-test",
    seal_reason: str = "rights-reviewed",
    seal_enabled: bool = True,
) -> None:
    """Insert an explicitly synthetic grant, quota policy, binding, and bucket."""
    digest = hashlib.sha256(b"synthetic offline gate fixture").hexdigest()
    policy_sha256 = digest if policy_sha256 is None else policy_sha256
    binding_sha256 = digest if binding_sha256 is None else binding_sha256
    rights_evidence_sha256 = digest if rights_evidence_sha256 is None else rights_evidence_sha256
    with engine.begin() as connection:
        if ignore_check_constraints:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, capacity_microtokens, "
                "policy_reference, policy_sha256, configured_by, configured_at_us) "
                "VALUES (:quota, :revision, :rate, :capacity, :reference, :digest, :actor, :at)"
            ),
            {
                "quota": QUOTA_KEY,
                "revision": policy_revision,
                "rate": rate,
                "capacity": capacity,
                "reference": policy_reference,
                "digest": policy_sha256,
                "actor": policy_configured_by,
                "at": policy_configured_at_us,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, :reference, :digest, :actor, :at)"
            ),
            {
                "source": SOURCE,
                "quota": QUOTA_KEY,
                "reference": binding_reference,
                "digest": binding_sha256,
                "actor": binding_configured_by,
                "at": binding_configured_at_us,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, :tokens, :remainder, :refill, :revision)"
            ),
            {
                "quota": QUOTA_KEY,
                "tokens": tokens,
                "remainder": state_remainder,
                "refill": last_refill_us,
                "revision": state_revision,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, revoked_at_us, created_at_us, updated_at_us) "
                "VALUES ('grant-1', :source, :model, :operation, :principal, :decision, "
                ":revision, :not_before, :expires, :conditions, :evidence_reference, "
                ":evidence_sha, :attestation_note, :attested_by, :attested_at, "
                ":revoked_at, :created_at, :updated_at)"
            ),
            {
                "source": source,
                "model": model,
                "operation": operation,
                "principal": principal,
                "decision": decision,
                "revision": grant_revision,
                "not_before": not_before_us,
                "expires": expires_at_us,
                "conditions": conditions_json,
                "evidence_reference": rights_evidence_reference,
                "evidence_sha": rights_evidence_sha256,
                "attestation_note": operator_attestation_note,
                "attested_by": attested_by,
                "attested_at": attested_at_us,
                "created_at": grant_created_at_us,
                "updated_at": grant_updated_at_us,
                "revoked_at": revoked_at_us,
            },
        )
        for host in hosts:
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES ('grant-1', :host)"
                ),
                {"host": host},
            )
        if decision == "ALLOWED" and hosts and seal_enabled:
            effective_seal_at = max(1, grant_updated_at_us) if seal_at_us is None else seal_at_us
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grant_host_sets "
                    "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                    "VALUES ('grant-1', :revision, :host_count, :sealed_at, :sealed_by, :reason)"
                ),
                {
                    "revision": grant_revision,
                    "host_count": len(hosts),
                    "sealed_at": effective_seal_at,
                    "sealed_by": seal_by,
                    "reason": seal_reason,
                },
            )


def _attempt(
    *,
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    source: str = SOURCE,
    model: str = MODEL,
    operation: str = "query",
    principal: str = PRINCIPAL,
    host: str = HOST,
    grant_id: str = "grant-1",
    revision: int = 1,
    attempt_no: int = 1,
    run_key: str | None = None,
) -> TransportAttempt:
    """Build one transport identity whose individual authorization fields can vary."""
    return TransportAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=attempt_no,
        grant_id=grant_id,
        expected_revision=revision,
        source=source,
        canonical_model=model,
        operation=operation,
        principal_scope=principal,
        host=host,
        run_key=run_key,
    )


def _fixed_time(monkeypatch: pytest.MonkeyPatch, gate: SourceAttemptGate, now_us: int) -> None:
    """Fix only the SQLite clock read so expiry and quota tests use exact boundaries."""
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: now_us)


def _attempt_rows(engine: Engine) -> list[Mapping[str, object]]:
    """Read persisted decisions from the isolated test database."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                sa.text("SELECT * FROM od_source_attempt_reservations ORDER BY attempt_id")
            )
            .mappings()
            .all()
        )


def _state(engine: Engine) -> Mapping[str, object]:
    """Read the single synthetic bucket after a decision."""
    with engine.connect() as connection:
        return (
            connection.execute(
                sa.text("SELECT * FROM od_source_gate_state WHERE quota_key = :key"),
                {"key": QUOTA_KEY},
            )
            .mappings()
            .one()
        )


@pytest.mark.parametrize(
    ("conditions_json", "denial_code"),
    (
        ("{broken", "grant_conditions_invalid"),
        ("{}", "grant_conditions_invalid"),
        ('["redistribution"]', "grant_conditions_unresolved"),
        ('["  "]', "grant_conditions_invalid"),
        ("[1]", "grant_conditions_invalid"),
    ),
)
def test_malformed_or_unresolved_grant_conditions_fail_closed(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    conditions_json: str,
    denial_code: str,
) -> None:
    """Unknown or malformed grant conditions never authorize and do not spend quota."""
    _seed(gate_db, conditions_json=conditions_json)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (ReservationStatus.DENIED, denial_code)
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO
    assert _attempt_rows(gate_db)[0]["outcome"] == "DENIED"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source", "other-source"),
        ("model", "other-model"),
        ("operation", "store"),
        ("principal", "other-worker"),
    ),
)
def test_grant_scope_must_match_every_attempt_dimension(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    """A grant for another source, model, operation, or principal is not reusable."""
    _seed(gate_db)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)
    attempt_fields = {
        "source": SOURCE,
        "model": MODEL,
        "operation": "query",
        "principal": PRINCIPAL,
    }
    attempt_fields[field] = value

    result = gate.reserve_attempt(_attempt(**attempt_fields))

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_scope_mismatch",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_grant_host_match_is_exact_but_attempt_hostname_is_canonicalized(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DNS case and a terminal dot normalize while sibling hosts remain denied."""
    _seed(gate_db)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    canonicalized = gate.reserve_attempt(_attempt(host="API.SINA.EXAMPLE."))
    sibling = gate.reserve_attempt(
        _attempt(attempt_id="attempt-2", request_id="request-2", host="sub.api.sina.example")
    )

    assert canonicalized.status is ReservationStatus.RESERVED_NEW
    assert (sibling.status, sibling.denial_code) == (
        ReservationStatus.DENIED,
        "grant_host_mismatch",
    )
    assert _state(gate_db)["tokens_micro"] == 0


@pytest.mark.parametrize(
    ("seal_by", "seal_reason", "seal_at_us"),
    (
        ("operator name", "rights-reviewed", 1),
        ("offline-test", "human approval", 1),
        ("offline-test", "rights-reviewed", T0_US + 1),
    ),
)
def test_invalid_or_future_host_set_seal_does_not_authorize(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    seal_by: str,
    seal_reason: str,
    seal_at_us: int,
) -> None:
    """Seal actor, reason, and time are revalidated when the grant is used."""
    _seed(gate_db, seal_by=seal_by, seal_reason=seal_reason, seal_at_us=seal_at_us)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_hosts_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_unsealed_grant_host_set_fails_closed(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ALLOWED grant without a frozen host set cannot authorize transport."""
    _seed(gate_db, seal_enabled=False)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)
    result = gate.reserve_attempt(_attempt())
    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_hosts_unsealed",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(("column", "value"), (("revision", 2), ("host_count", 2)))
def test_tampered_host_set_seal_is_detected_before_host_authorization(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    value: int,
) -> None:
    """Runtime validation catches a seal whose revision or host count no longer matches."""
    _seed(gate_db)
    with gate_db.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER trg_od_source_grant_host_set_immutable_update"))
        if column == "revision":
            connection.execute(
                sa.text("UPDATE od_source_grant_host_sets SET revision = :value"),
                {"value": value},
            )
        else:
            connection.execute(
                sa.text("UPDATE od_source_grant_host_sets SET host_count = :value"),
                {"value": value},
            )
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_hosts_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize("persisted_host", ("API.SINA.EXAMPLE.", "api bad"))
def test_noncanonical_persisted_host_is_rejected(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch, persisted_host: str
) -> None:
    """The sealed host set itself must use canonical exact DNS names."""
    _seed(gate_db, hosts=(persisted_host,))
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_hosts_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    "declaration",
    (
        {"attested_by": "operator name"},
        {"attested_at_us": T0_US + 1},
        {"rights_evidence_reference": "   "},
        {"rights_evidence_sha256": "A" * 64},
        {"operator_attestation_note": "   "},
    ),
)
def test_grant_declaration_fields_are_revalidated_at_use_time(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    declaration: dict[str, object],
) -> None:
    """Database-accepted attestation values outside the gate grammar fail closed."""
    _seed(gate_db, ignore_check_constraints=True, **declaration)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_evidence_incomplete",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    ("not_before_us", "expires_at_us"),
    (
        (-1, FAR_FUTURE_US),
        (0, 0),
        (T0_US + 20, T0_US + 19),
    ),
)
def test_corrupt_persisted_grant_window_is_invalid(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    not_before_us: int,
    expires_at_us: int,
) -> None:
    """Out-of-range or inverted stored validity windows cannot authorize a request."""
    _seed(
        gate_db,
        not_before_us=not_before_us,
        expires_at_us=expires_at_us,
        ignore_check_constraints=True,
    )
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    ("grant_created_at_us", "grant_updated_at_us", "seal_at_us", "revoked_at_us"),
    (
        (T0_US + 1, 1, 1, None),
        (1, T0_US + 1, T0_US + 1, None),
        (1, 1, 1, T0_US),
    ),
)
def test_grant_lifecycle_timestamps_must_be_ordered_and_current(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    grant_created_at_us: int,
    grant_updated_at_us: int,
    seal_at_us: int,
    revoked_at_us: int | None,
) -> None:
    """Future, inverted, or revoked grant lifecycle values cannot authorize."""
    _seed(
        gate_db,
        grant_created_at_us=grant_created_at_us,
        grant_updated_at_us=grant_updated_at_us,
        revoked_at_us=revoked_at_us,
        seal_at_us=seal_at_us,
        ignore_check_constraints=True,
    )
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "grant_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_grant_not_before_boundary_is_inclusive(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant becomes eligible exactly at not_before_us, with no early debit."""
    _seed(gate_db, not_before_us=T0_US + 20)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US + 19)
    early = gate.reserve_attempt(_attempt())
    assert (early.status, early.denial_code) == (
        ReservationStatus.DENIED,
        "grant_not_yet_valid",
    )
    _fixed_time(monkeypatch, gate, T0_US + 20)

    boundary = gate.reserve_attempt(_attempt(attempt_id="attempt-2", request_id="request-2"))

    assert boundary.status is ReservationStatus.RESERVED_NEW
    assert _state(gate_db)["tokens_micro"] == 0


@pytest.mark.parametrize(
    "policy_metadata",
    (
        {"policy_configured_by": "operator name"},
        {"policy_configured_at_us": T0_US + 1},
        {"policy_sha256": "A" * 64},
        {"policy_reference": "   "},
    ),
)
def test_malformed_or_not_yet_effective_policy_is_not_used(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    policy_metadata: dict[str, object],
) -> None:
    """Policy metadata must meet gate validation and not postdate the trusted clock."""
    _seed(gate_db, ignore_check_constraints=True, **policy_metadata)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_policy_unconfigured",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    ("state_revision", "last_refill_us", "denial_code"),
    (
        (2, T0_US, "source_policy_revision_invalid"),
        (1, T0_US + 1, "source_quota_state_unavailable"),
    ),
)
def test_stale_or_future_bucket_state_never_authorizes(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    state_revision: int,
    last_refill_us: int,
    denial_code: str,
) -> None:
    """A bucket from a future policy revision or future clock cannot be spent."""
    _seed(gate_db, state_revision=state_revision, last_refill_us=last_refill_us)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (ReservationStatus.DENIED, denial_code)
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_minimum_and_maximum_policy_rates_have_exact_microsecond_boundaries(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ceiling retry math at the minimum rate and one-microsecond max-rate refill hold."""
    _seed(gate_db, rate=1, tokens=0)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    minimum_rate = gate.reserve_attempt(_attempt())

    assert (minimum_rate.status, minimum_rate.retry_after_us) == (
        ReservationStatus.DENIED,
        1_000_000_000_000,
    )
    with gate_db.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE od_source_gate_policies SET policy_revision = 2, "
                "rate_microtokens_per_second = 1000000000000, configured_at_us = 1 "
                "WHERE quota_key = :quota"
            ),
            {"quota": QUOTA_KEY},
        )
        connection.execute(
            sa.text(
                "UPDATE od_source_gate_state SET policy_revision = 2, last_refill_us = :at, "
                "remainder = 0 WHERE quota_key = :quota"
            ),
            {"quota": QUOTA_KEY, "at": T0_US},
        )
    _fixed_time(monkeypatch, gate, T0_US + 1)

    maximum_rate = gate.reserve_attempt(_attempt(attempt_id="attempt-2", request_id="request-2"))

    assert maximum_rate.status is ReservationStatus.RESERVED_NEW
    assert _state(gate_db)["tokens_micro"] == 0
    assert _state(gate_db)["policy_revision"] == 2


def test_missing_clock_or_storage_failure_never_returns_a_reservation(gate_db: Engine) -> None:
    """A missing persisted clock is translated to unavailable before any tombstone exists."""
    gate = SourceAttemptGate(gate_db)
    with gate_db.begin() as connection:
        connection.execute(sa.text("DROP TABLE od_source_gate_clock"))

    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt(_attempt())

    assert _attempt_rows(gate_db) == []


def test_corrupt_persisted_clock_highwater_fails_closed(gate_db: Engine) -> None:
    """An out-of-range stored clock cannot be repaired into an authorization time."""
    _seed(gate_db)
    with gate_db.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            sa.text("UPDATE od_source_gate_clock SET highwater_us = -1 WHERE clock_id = 1")
        )

    gate = SourceAttemptGate(gate_db)
    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt(_attempt())

    with gate_db.connect() as connection:
        assert (
            connection.execute(
                sa.text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == -1
        )
    assert _attempt_rows(gate_db) == []
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize("julianday_value", (0.0, None))
def test_invalid_sqlite_wall_clock_fails_closed_before_reservation(
    gate_db: Engine, julianday_value: float | None
) -> None:
    """A zero or NULL SQLite clock cannot advance the gate or create a permit."""
    _seed(gate_db)

    def install_invalid_clock(dbapi_connection: sqlite3.Connection, _record: object) -> None:
        dbapi_connection.create_function("julianday", 1, lambda _value: julianday_value)

    gate = SourceAttemptGate(gate_db)
    event.listen(gate_db, "connect", install_invalid_clock)
    try:
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(_attempt())
    finally:
        event.remove(gate_db, "connect", install_invalid_clock)

    assert _attempt_rows(gate_db) == []
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_stale_or_missing_revoke_requests_do_not_mutate_the_grant(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compare-and-revoke rejects stale and unknown ids while leaving one audit trail."""
    _seed(gate_db)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    stale = gate.revoke_grant(
        "grant-1", expected_revision=2, actor="operator-1", reason_code="rights-withdrawn"
    )
    missing = gate.revoke_grant(
        "missing-grant", expected_revision=1, actor="operator-1", reason_code="rights-withdrawn"
    )
    with gate_db.connect() as connection:
        grant = (
            connection.execute(
                sa.text("SELECT decision, revision, revoked_at_us FROM od_source_grants")
            )
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                sa.text("SELECT event_type FROM od_source_grant_events ORDER BY event_id")
            )
            .scalars()
            .all()
        )

    assert stale.status is GrantRevocationStatus.STALE_REVISION
    assert missing.status is GrantRevocationStatus.NOT_FOUND
    assert (grant["decision"], grant["revision"], grant["revoked_at_us"]) == ("ALLOWED", 1, None)
    assert events == ["HOSTS_SEALED"]


def test_stored_grant_revision_is_revalidated_at_reservation(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An out-of-range persisted grant revision fails closed before quota lookup."""
    _seed(gate_db, grant_revision=0, ignore_check_constraints=True)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    corrupt = gate.reserve_attempt(_attempt())

    assert (corrupt.status, corrupt.denial_code) == (ReservationStatus.DENIED, "grant_invalid")
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


def test_caller_revision_must_match_current_grant_revision(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale expected revision cannot reuse a valid grant or debit quota."""
    _seed(gate_db)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    stale = gate.reserve_attempt(_attempt(revision=2))

    assert (stale.status, stale.denial_code) == (
        ReservationStatus.DENIED,
        "grant_revision_mismatch",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    "binding_metadata",
    (
        {"binding_configured_by": "operator name"},
        {"binding_configured_at_us": T0_US + 1},
        {"binding_sha256": "A" * 64},
        {"binding_reference": "   "},
    ),
)
def test_invalid_or_future_source_binding_does_not_select_a_quota(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    binding_metadata: dict[str, object],
) -> None:
    """Quota mappings with invalid actors or future activation are rejected."""
    _seed(gate_db, ignore_check_constraints=True, **binding_metadata)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_quota_unconfigured",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    ("rate", "capacity"),
    ((0, TOKEN_MICRO), (1, TOKEN_MICRO - 1)),
)
def test_out_of_range_stored_policy_is_never_arithmetic_input(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    rate: int,
    capacity: int,
) -> None:
    """Persisted policy numbers are revalidated before bucket math despite bad storage."""
    _seed(gate_db, rate=rate, capacity=capacity, ignore_check_constraints=True)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_policy_unconfigured",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO


@pytest.mark.parametrize(
    ("tokens", "state_remainder", "state_revision", "last_refill_us"),
    (
        (-1, 0, 1, T0_US),
        (0, 1_000_000, 1, T0_US),
        (0, 0, 0, T0_US),
        (0, 0, 1, -1),
    ),
)
def test_corrupt_bucket_fields_are_reported_unavailable(
    gate_db: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tokens: int,
    state_remainder: int,
    state_revision: int,
    last_refill_us: int,
) -> None:
    """Corrupt persisted bucket fields cannot become a fresh or enlarged allowance."""
    _seed(
        gate_db,
        tokens=tokens,
        state_remainder=state_remainder,
        state_revision=state_revision,
        last_refill_us=last_refill_us,
        ignore_check_constraints=True,
    )
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_quota_state_unavailable",
    )
    assert _attempt_rows(gate_db)[0]["outcome"] == "DENIED"


def test_bucket_balance_above_current_capacity_is_not_silently_clamped(
    gate_db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-revision balance over capacity is treated as corrupt rather than repaired."""
    _seed(gate_db, tokens=TOKEN_MICRO + 1)
    gate = SourceAttemptGate(gate_db)
    _fixed_time(monkeypatch, gate, T0_US)

    result = gate.reserve_attempt(_attempt())

    assert (result.status, result.denial_code) == (
        ReservationStatus.DENIED,
        "source_quota_state_invalid",
    )
    assert _state(gate_db)["tokens_micro"] == TOKEN_MICRO + 1


@pytest.mark.parametrize(
    "overrides",
    (
        {"attempt_no": 0},
        {"attempt_no": True},
        {"revision": 0},
        {"operation": "delete"},
        {"host": "https://api.sina.example/path"},
        {"host": "api.sina.example:443"},
        {"run_key": "run key"},
    ),
)
def test_attempt_identity_rejects_unbounded_or_ambiguous_inputs(
    overrides: dict[str, object],
) -> None:
    """Caller identities must be bounded and hosts must be exact DNS names."""
    with pytest.raises(SourceAttemptInputError):
        _attempt(**overrides)


def test_reservation_rejects_non_transport_attempt_before_opening_storage(gate_db: Engine) -> None:
    """The public reservation boundary rejects an untyped identity without side effects."""
    gate = SourceAttemptGate(gate_db)

    with pytest.raises(SourceAttemptInputError):
        gate.reserve_attempt(object())  # type: ignore[arg-type]

    assert _attempt_rows(gate_db) == []


def test_writer_initialization_failure_is_translated_to_unavailable(
    gate_db: Engine,
) -> None:
    """A failed SQLite write-lock acquisition cannot return or persist a permit."""
    gate = SourceAttemptGate(gate_db)

    def fail_begin_immediate(
        _connection: sa.engine.Connection,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.upper() == "BEGIN IMMEDIATE":
            raise sa.exc.OperationalError(
                statement, parameters, RuntimeError("injected lock error")
            )

    event.listen(gate_db, "before_cursor_execute", fail_begin_immediate)
    try:
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(_attempt())
    finally:
        event.remove(gate_db, "before_cursor_execute", fail_begin_immediate)

    assert _attempt_rows(gate_db) == []


def test_file_url_with_memory_creator_is_rejected_before_reservation(tmp_path: Path) -> None:
    """The opened SQLite main DB must match the file identity recorded at construction."""
    declared_file = tmp_path / "declared-source-gate.sqlite"
    declared_file.touch()
    engine = sa.create_engine(
        f"sqlite:///{declared_file}",
        creator=lambda: sqlite3.connect(":memory:"),
        poolclass=NullPool,
    )
    try:
        gate = SourceAttemptGate(engine)
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_attempt(_attempt())
    finally:
        engine.dispose()


def test_main_database_identity_is_rechecked_after_writer_lock(tmp_path: Path) -> None:
    """Replacing the named DB between initial identity check and lock is rejected."""
    database_file = tmp_path / "source-gate-replaced.sqlite"
    displaced_file = tmp_path / "source-gate-original.sqlite"
    database_file.touch()
    engine = sa.create_engine(f"sqlite:///{database_file}", poolclass=NullPool)
    database_list_reads = 0

    def replace_database_before_lock(
        _connection: sa.engine.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal database_list_reads
        if statement.upper().startswith("PRAGMA DATABASE_LIST"):
            database_list_reads += 1
        if statement.upper() == "BEGIN IMMEDIATE":
            database_file.rename(displaced_file)
            database_file.touch()

    try:
        gate = SourceAttemptGate(engine)
        event.listen(engine, "before_cursor_execute", replace_database_before_lock)
        try:
            with pytest.raises(SourceAttemptGateUnavailable):
                gate.reserve_attempt(_attempt())
        finally:
            event.remove(engine, "before_cursor_execute", replace_database_before_lock)
    finally:
        engine.dispose()

    assert displaced_file.exists()
    assert database_file.exists()
    assert database_list_reads == 2
