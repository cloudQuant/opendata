"""Offline fail-closed edge tests for the rights-bound source gate v3 path."""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from opendata.data.source_attempt_gate import (
    AttemptLookupAbsent,
    AttemptLookupDenied,
    AttemptLookupIndeterminate,
    AttemptLookupReasonCode,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceAttemptScopeV3,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Engine


_SUPPORT_PATH = Path(__file__).with_name("test_source_attempt_scope_v3.py")
_SUPPORT_SPEC = importlib.util.spec_from_file_location(
    "source_attempt_scope_v3_edge_test_support", _SUPPORT_PATH
)
assert _SUPPORT_SPEC is not None and _SUPPORT_SPEC.loader is not None
_SUPPORT = importlib.util.module_from_spec(_SUPPORT_SPEC)
_SUPPORT_SPEC.loader.exec_module(_SUPPORT)

NOW_US: int = _SUPPORT.NOW_US
MAX_TIMESTAMP_US: int = _SUPPORT.MAX_TIMESTAMP_US
RIGHTS_REFERENCE: str = _SUPPORT.RIGHTS_REFERENCE


@pytest.fixture
def gate_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """Reuse the pinned v1/v2/v3 test schema in a fresh owned SQLite file."""
    engine = _SUPPORT._new_engine(tmp_path, "source-gate-v3-edges.sqlite")
    _SUPPORT._install_v1_schema(engine)
    _SUPPORT._install_scope_v3_schema(engine)
    yield engine
    engine.dispose()


def _scope() -> SourceAttemptScopeV3:
    """Return the existing suite's complete synthetic v3 scope."""
    return _SUPPORT._scope_v3()


@pytest.mark.parametrize(
    ("edge", "expected_message"),
    [
        ("wrong-attempt-type", "attempt must be a TransportAttempt"),
        ("missing-run-key", "v3 attempt scope requires a run_key"),
        ("invalid-rights-digest", "request_summary_sha256 must be a lowercase SHA-256"),
        ("invalid-budget-revision", "budget_policy_revision is outside the supported range"),
    ],
)
def test_v3_scope_rejects_incomplete_rights_and_budget_pins(
    edge: str,
    expected_message: str,
) -> None:
    """V3 validates attempt lineage, evidence digests, and budget revisions before storage."""
    scope = _scope()
    if edge == "wrong-attempt-type":
        with pytest.raises(SourceAttemptInputError, match=expected_message):
            replace(scope, attempt=object())
    elif edge == "missing-run-key":
        missing_run = replace(scope.attempt, run_key=None)
        with pytest.raises(SourceAttemptInputError, match=expected_message):
            replace(scope, attempt=missing_run)
    elif edge == "invalid-rights-digest":
        with pytest.raises(SourceAttemptInputError, match=expected_message):
            replace(scope, request_summary_sha256="not-a-sha256")
    else:
        with pytest.raises(SourceAttemptInputError, match=expected_message):
            replace(scope, budget_policy_revision=0)


def _seed_edge_grant(
    engine: Engine,
    scope: SourceAttemptScopeV3,
    *,
    not_before_us: int = 0,
    expires_at_us: int = MAX_TIMESTAMP_US,
    conditions_json: str = "[]",
    sealed_host: str | None = None,
    include_binding: bool = True,
    include_bucket: bool = True,
) -> None:
    """Insert one valid synthetic grant with selectable rights/budget boundaries."""
    evidence_sha256 = hashlib.sha256(b"synthetic edge evidence").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota_key, 1, 1000000, 1000000, "
                "'offline:budget-policy', :digest, 'offline-test', 1)"
            ),
            {
                "quota_key": scope.budget_quota_key,
                "digest": scope.budget_policy_sha256,
            },
        )
        if include_binding:
            connection.execute(
                text(
                    "INSERT INTO od_source_gate_source_bindings "
                    "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                    "configured_at_us) VALUES (:source, :quota_key, 'offline:source-binding', "
                    ":digest, 'offline-test', 1)"
                ),
                {
                    "source": scope.attempt.source,
                    "quota_key": scope.budget_quota_key,
                    "digest": scope.budget_binding_sha256,
                },
            )
        if include_bucket:
            connection.execute(
                text(
                    "INSERT INTO od_source_gate_state "
                    "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                    "VALUES (:quota_key, 1000000, 0, :refill, 1)"
                ),
                {"quota_key": scope.budget_quota_key, "refill": NOW_US},
            )
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant_id, :source, :model, 'query', :principal, 'ALLOWED', 1, "
                ":not_before, :expires, :conditions, :reference, :digest, "
                "'Synthetic declaration for offline edge tests.', 'offline-test', 1, 1, 1)"
            ),
            {
                "grant_id": scope.attempt.grant_id,
                "source": scope.attempt.source,
                "model": scope.attempt.canonical_model,
                "principal": scope.attempt.principal_scope,
                "not_before": not_before_us,
                "expires": expires_at_us,
                "conditions": conditions_json,
                "reference": RIGHTS_REFERENCE,
                "digest": evidence_sha256,
            },
        )
        host = sealed_host or scope.attempt.host
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"),
            {"grant_id": scope.attempt.grant_id, "host": host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant_id, 1, 1, 1, 'offline-test', 'synthetic-edge-test')"
            ),
            {"grant_id": scope.attempt.grant_id},
        )


def _rewrite_immutable_row(
    engine: Engine,
    *,
    table: str,
    trigger: str,
    attempt_id: str,
    changes: dict[str, object],
) -> None:
    """Temporarily lift a test-owned immutable trigger to model persisted corruption."""
    update_statements = {
        "od_source_attempt_scope_v3": {
            "scope_json": "UPDATE od_source_attempt_scope_v3 SET scope_json = :value "
            "WHERE attempt_id = :attempt_id",
            "scope_sha256": "UPDATE od_source_attempt_scope_v3 SET scope_sha256 = :value "
            "WHERE attempt_id = :attempt_id",
            "recorded_at_us": "UPDATE od_source_attempt_scope_v3 SET recorded_at_us = :value "
            "WHERE attempt_id = :attempt_id",
        },
        "od_source_attempt_reservations": {
            "denial_code": "UPDATE od_source_attempt_reservations SET denial_code = :value "
            "WHERE attempt_id = :attempt_id",
            "policy_revision": "UPDATE od_source_attempt_reservations "
            "SET policy_revision = :value WHERE attempt_id = :attempt_id",
        },
    }
    trigger_drop_statements = {
        "trg_od_source_attempt_scope_v3_immutable_update": (
            "DROP TRIGGER trg_od_source_attempt_scope_v3_immutable_update"
        ),
        "trg_od_source_attempt_immutable_update": (
            "DROP TRIGGER trg_od_source_attempt_immutable_update"
        ),
    }
    assert table in update_statements
    assert trigger in {
        "trg_od_source_attempt_scope_v3_immutable_update",
        "trg_od_source_attempt_immutable_update",
    }
    assert set(changes) <= set(update_statements[table])
    with engine.begin() as connection:
        trigger_sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
            {"name": trigger},
        ).scalar_one()
        connection.exec_driver_sql(trigger_drop_statements[trigger])
        for column, value in changes.items():
            connection.execute(
                text(update_statements[table][column]),
                {"value": value, "attempt_id": attempt_id},
            )
        connection.exec_driver_sql(trigger_sql)


@pytest.mark.parametrize(
    ("edge", "expected_denial"),
    [
        ("not-yet-valid", "grant_not_yet_valid"),
        ("expired-at-boundary", "grant_expired"),
        ("unresolved-condition", "grant_conditions_unresolved"),
        ("sealed-host-mismatch", "grant_host_mismatch"),
    ],
)
def test_v3_rights_boundaries_are_persisted_as_denials_without_quota_debit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
    expected_denial: str,
) -> None:
    """V3 records fixed rights denials at validity, condition, and host boundaries."""
    scope = _scope()
    grant_options: dict[str, object] = {}
    if edge == "not-yet-valid":
        grant_options.update(not_before_us=NOW_US + 1, expires_at_us=NOW_US + 2)
    elif edge == "expired-at-boundary":
        grant_options["expires_at_us"] = NOW_US
    elif edge == "unresolved-condition":
        grant_options["conditions_json"] = '["manual-rights-review"]'
    else:
        grant_options["sealed_host"] = "other.sina.example"
    _seed_edge_grant(gate_engine, scope, **grant_options)
    gate = SourceAttemptGate(gate_engine)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    result = gate.reserve_attempt_v3(scope)
    lookup = gate.lookup_attempt_v3(scope)

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == expected_denial
    assert isinstance(lookup, AttemptLookupDenied)
    assert lookup.denial_code == expected_denial
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :key"),
                {"key": scope.budget_quota_key},
            ).scalar_one()
            == 1_000_000
        )


@pytest.mark.parametrize(
    ("edge", "expected_denial"),
    [
        ("wrong-quota-pin", "budget_scope_mismatch"),
        ("missing-binding", "source_quota_unconfigured"),
        ("missing-bucket", "source_quota_state_unavailable"),
    ],
)
def test_v3_budget_edges_fail_closed_and_remain_lookupable(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
    expected_denial: str,
) -> None:
    """Stale, absent, or incomplete budget state never yields an attempt permit."""
    original_scope = _scope()
    scope = original_scope
    if edge == "wrong-quota-pin":
        _seed_edge_grant(gate_engine, original_scope)
        scope = replace(original_scope, budget_quota_key="other-quota-v1")
    elif edge == "missing-binding":
        _seed_edge_grant(gate_engine, scope, include_binding=False)
    else:
        _seed_edge_grant(gate_engine, scope, include_bucket=False)
    gate = SourceAttemptGate(gate_engine)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    result = gate.reserve_attempt_v3(scope)
    lookup = gate.lookup_attempt_v3(scope)

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == expected_denial
    assert isinstance(lookup, AttemptLookupDenied)
    assert lookup.denial_code == expected_denial
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        if edge == "wrong-quota-pin":
            assert (
                connection.execute(
                    text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :key"),
                    {"key": original_scope.budget_quota_key},
                ).scalar_one()
                == 1_000_000
            )
        elif edge == "missing-bucket":
            assert (
                connection.execute(text("SELECT COUNT(*) FROM od_source_gate_state")).scalar_one()
                == 0
            )


def test_v3_absent_lookup_is_exact_and_does_not_advance_the_clock(gate_engine: Engine) -> None:
    """Only an exact verified empty snapshot reports ABSENT, without mutation."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()

    result = gate.lookup_attempt_v3(scope)

    assert isinstance(result, AttemptLookupAbsent)
    assert result.scope_sha256 == hashlib.sha256(_SUPPORT._scope_v3_bytes(scope)).hexdigest()
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == 0
        )


def test_v3_denied_tombstone_replay_is_duplicate_and_never_a_send_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated denied attempt stays a duplicate while lookup returns its denial."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    first = gate.reserve_attempt_v3(scope)
    replay = gate.reserve_attempt_v3(scope)
    lookup = gate.lookup_attempt_v3(scope)

    assert first.status is ReservationStatus.DENIED
    assert first.denial_code == "grant_not_found"
    assert replay.status is ReservationStatus.DUPLICATE
    assert isinstance(lookup, AttemptLookupDenied)
    assert lookup.denial_code == "grant_not_found"
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")).scalar_one()
            == 1
        )


def test_v3_lookup_and_reservation_reject_divergent_attempt_keys(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attempt id and request/attempt number resolving to different rows is ambiguous."""
    gate = SourceAttemptGate(gate_engine)
    first_scope = _scope()
    _SUPPORT._seed_allowed_grant(gate_engine, first_scope)
    second_attempt = replace(
        first_scope.attempt,
        attempt_id="attempt-2",
        request_id="request-2",
        attempt_no=2,
    )
    second_scope = replace(first_scope, attempt=second_attempt, capture_intent_id="intent-2")
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(first_scope).status is ReservationStatus.RESERVED_NEW
    assert gate.reserve_attempt_v3(second_scope).status is ReservationStatus.DENIED
    conflicting = replace(
        first_scope,
        attempt=replace(
            first_scope.attempt,
            request_id=second_attempt.request_id,
            attempt_no=second_attempt.attempt_no,
        ),
    )

    lookup = gate.lookup_attempt_v3(conflicting)
    replay = gate.reserve_attempt_v3(conflicting)

    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.ID_CONFLICT
    assert replay.status is ReservationStatus.ID_CONFLICT


def test_v3_attempt_identity_mismatch_is_indeterminate_and_not_reusable(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused attempt id with another business scope cannot lookup or reserve again."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _SUPPORT._seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW
    changed = replace(
        scope,
        attempt=replace(scope.attempt, canonical_model="other-financial-model"),
    )

    lookup = gate.lookup_attempt_v3(changed)
    replay = gate.reserve_attempt_v3(changed)

    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCOPE_MISMATCH
    assert replay.status is ReservationStatus.ID_CONFLICT


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("bad-digest", AttemptLookupReasonCode.SCOPE_CORRUPT),
        ("invalid-utf8", AttemptLookupReasonCode.SCOPE_CORRUPT),
        ("rehash-different-json", AttemptLookupReasonCode.SCOPE_MISMATCH),
        ("timestamp-mismatch", AttemptLookupReasonCode.RECORD_CORRUPT),
    ],
)
def test_v3_lookup_never_promotes_corrupt_or_changed_sidecars(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    expected_reason: AttemptLookupReasonCode,
) -> None:
    """Lookup distinguishes malformed persistence from a well-formed changed scope."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _SUPPORT._seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW
    if corruption == "bad-digest":
        changes = {"scope_sha256": "0" * 64}
    elif corruption == "invalid-utf8":
        changes = {"scope_json": b"\xff"}
    elif corruption == "rehash-different-json":
        changed_json = b'{"scope_version":3}'
        changes = {
            "scope_json": changed_json.decode("ascii"),
            "scope_sha256": hashlib.sha256(changed_json).hexdigest(),
        }
    else:
        changes = {"recorded_at_us": NOW_US - 1}
    _rewrite_immutable_row(
        gate_engine,
        table="od_source_attempt_scope_v3",
        trigger="trg_od_source_attempt_scope_v3_immutable_update",
        attempt_id=scope.attempt.attempt_id,
        changes=changes,
    )

    result = gate.lookup_attempt_v3(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is expected_reason


def test_v3_lookup_rejects_corrupted_reserved_budget_snapshot(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable reservation with pins from another budget revision is not reusable."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _SUPPORT._seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW
    _rewrite_immutable_row(
        gate_engine,
        table="od_source_attempt_reservations",
        trigger="trg_od_source_attempt_immutable_update",
        attempt_id=scope.attempt.attempt_id,
        changes={"policy_revision": 2},
    )

    result = gate.lookup_attempt_v3(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.RECORD_CORRUPT


def test_v3_lookup_rejects_unknown_persisted_denial_codes(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manually altered denial code cannot be returned as a trusted decision."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.DENIED
    _rewrite_immutable_row(
        gate_engine,
        table="od_source_attempt_reservations",
        trigger="trg_od_source_attempt_immutable_update",
        attempt_id=scope.attempt.attempt_id,
        changes={"denial_code": "unknown-denial-code"},
    )

    result = gate.lookup_attempt_v3(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.RECORD_CORRUPT


@pytest.mark.parametrize("drift", ["old-marker", "extra-scope-index", "wrong-trigger"])
def test_v3_reserve_and_lookup_fail_closed_on_scope_schema_drift(
    gate_engine: Engine,
    drift: str,
) -> None:
    """V3 refuses stale lineage or altered scope DDL without advancing durable state."""
    with gate_engine.begin() as connection:
        if drift == "old-marker":
            connection.execute(
                text(
                    "UPDATE alembic_version_source_gate "
                    "SET version_num = 'sg0002_source_attempt_scope_v2'"
                )
            )
        elif drift == "extra-scope-index":
            connection.exec_driver_sql(
                "CREATE INDEX test_v3_scope_extra_index "
                "ON od_source_attempt_scope_v3 (scope_sha256)"
            )
        else:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_od_source_attempt_scope_v3_immutable_update"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER trg_od_source_attempt_scope_v3_immutable_update "
                "BEFORE UPDATE ON od_source_attempt_scope_v3 WHEN 1 BEGIN "
                "SELECT RAISE(ABORT, 'source attempt scope v3 rows are immutable'); END"
            )

    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    lookup = gate.lookup_attempt_v3(scope)

    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
    with pytest.raises(SourceAttemptGateUnavailable, match="v3 schema"):
        gate.reserve_attempt_v3(scope)
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == 0
        )
