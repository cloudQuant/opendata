"""Offline behavior tests for the rights-reference-bound attempt-scope v3 API."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import (
    V2_OBJECT_SQL,
    V2_REVISION,
    V3_OBJECT_SQL,
    V3_REVISION,
    V3_SCOPE_JSON_FIELDS,
    validate_migration_state,
)
from opendata.data.source_attempt_gate import (
    AttemptLookupDenied,
    AttemptLookupIndeterminate,
    AttemptLookupReasonCode,
    AttemptLookupReserved,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceAttemptScopeV2,
    SourceAttemptScopeV3,
    SourceGrantInspectionRequest,
    SourceGrantInspectionStatus,
    TransportAttempt,
    _scope_v2_bytes,
    _scope_v3_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from sqlalchemy.engine import Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
SOURCE = "sina"
MODEL = "financial-statement"
PRINCIPAL = "offline-worker"
HOST = "api.sina.example"
QUOTA_KEY = "sina-quota-v1"
RIGHTS_REFERENCE = "offline-rights-record-1"
NOW_US = 1_700_000_000_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999


def _load_base_migration() -> ModuleType:
    """Load the repository-owned v1 schema migration without app settings."""
    spec = importlib.util.spec_from_file_location("source_gate_scope_v3_base", BASE_MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_scope_v3_migration() -> ModuleType:
    """Load the new isolated Alembic revision without application configuration."""
    path = (
        REPOSITORY_ROOT / "alembic_source_gate" / "versions" / "sg0003_source_attempt_scope_v3.py"
    )
    spec = importlib.util.spec_from_file_location("source_attempt_scope_v3_revision_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path, filename: str = "source-gate-v3.sqlite") -> Engine:
    """Create an isolated file-backed SQLite database for one gate test."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )


def _install_v1_schema(engine: Engine) -> None:
    """Install the pinned source-attempt base schema into a test-owned file."""
    migration = _load_base_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def _install_scope_v3_schema(engine: Engine, *, include_v3: bool = True) -> None:
    """Install exact test sidecars and a v2 or v3 marker without backfilling rows."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate "
            "( version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version_source_gate (version_num) "
            "VALUES ('sg0002_source_attempt_scope_v2')"
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)
        if include_v3:
            for statement in V3_OBJECT_SQL:
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql(
                "UPDATE alembic_version_source_gate SET version_num = :revision",
                {"revision": V3_REVISION},
            )


@pytest.fixture
def gate_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """Yield a file-backed database with both immutable v2 and v3 sidecars."""
    engine = _new_engine(tmp_path)
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine)
    yield engine
    engine.dispose()


def _attempt(
    *,
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    attempt_no: int = 1,
    grant_id: str = "grant-1",
    principal_scope: str = PRINCIPAL,
    expected_reference: str | None = RIGHTS_REFERENCE,
) -> TransportAttempt:
    """Build one bounded synthetic transport identity."""
    return TransportAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=attempt_no,
        grant_id=grant_id,
        expected_revision=1,
        source=SOURCE,
        canonical_model=MODEL,
        operation="query",
        principal_scope=principal_scope,
        host=HOST,
        run_key="run-1",
        expected_rights_evidence_reference=expected_reference,
    )


def _scope_v3(attempt: TransportAttempt | None = None) -> SourceAttemptScopeV3:
    """Return a complete exact-reference scope with bounded synthetic pins."""
    return SourceAttemptScopeV3(
        attempt=attempt or _attempt(),
        request_scheme="https",
        request_method="GET",
        request_host=HOST,
        request_path="/v1/financial",
        capture_intent_id="intent-1",
        request_summary_sha256="a" * 64,
        source_policy_id="sina-financial-policy",
        source_policy_revision="rev-1",
        source_policy_sha256="b" * 64,
        budget_binding_sha256="c" * 64,
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256="d" * 64,
    )


def _scope_v2(attempt: TransportAttempt | None = None) -> SourceAttemptScopeV2:
    """Return a complete v2 scope to check compatibility on a v3 marker."""
    return SourceAttemptScopeV2(
        attempt=attempt or replace(_attempt(), expected_rights_evidence_reference=None),
        capture_intent_id="intent-1",
        request_summary_sha256="a" * 64,
        source_policy_id="sina-financial-policy",
        source_policy_revision="rev-1",
        source_policy_sha256="b" * 64,
        budget_binding_sha256="c" * 64,
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256="d" * 64,
    )


def _seed_allowed_grant(engine: Engine, scope: SourceAttemptScopeV3) -> None:
    """Create one internally consistent synthetic grant and shared quota bucket."""
    evidence_sha256 = hashlib.sha256(b"synthetic evidence body").hexdigest()
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
                "quota_key": scope.budget_quota_key,
                "reference": "offline:budget-policy",
                "digest": scope.budget_policy_sha256,
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
                "source": scope.attempt.source,
                "quota_key": scope.budget_quota_key,
                "reference": "offline:source-binding",
                "digest": scope.budget_binding_sha256,
                "actor": "offline-test",
            },
        )
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
                "(:grant_id, :source, :model, 'query', :principal, 'ALLOWED', 1, 0, :expires, "
                "'[]', :reference, :digest, :note, :actor, 1, 1, 1)"
            ),
            {
                "grant_id": scope.attempt.grant_id,
                "source": scope.attempt.source,
                "model": scope.attempt.canonical_model,
                "principal": scope.attempt.principal_scope,
                "expires": MAX_TIMESTAMP_US,
                "reference": RIGHTS_REFERENCE,
                "digest": evidence_sha256,
                "note": "Synthetic declaration for offline tests.",
                "actor": "offline-test",
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"),
            {"grant_id": scope.attempt.grant_id, "host": scope.attempt.host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant_id, 1, 1, 1, 'offline-test', 'synthetic-test')"
            ),
            {"grant_id": scope.attempt.grant_id},
        )


def test_v3_scope_requires_exact_rights_reference_and_binds_it_in_canonical_identity() -> None:
    """V3 refuses an absent reference and serializes its exact non-secret value."""
    with pytest.raises(SourceAttemptInputError, match="expected rights evidence reference"):
        _scope_v3(replace(_attempt(), expected_rights_evidence_reference=None))

    encoded = _scope_v3_bytes(_scope_v3())
    document = json.loads(encoded)
    assert document["expected_rights_evidence_reference"] == RIGHTS_REFERENCE
    assert document["request_scheme"] == "https"
    assert document["request_method"] == "GET"
    assert document["request_host"] == HOST
    assert document["request_path"] == "/v1/financial"
    assert tuple(sorted(document)) == V3_SCOPE_JSON_FIELDS
    assert document["scope_version"] == 3
    assert encoded == (
        b'{"attempt_id":"attempt-1","attempt_no":1,"budget_binding_sha256":"'
        + b"c" * 64
        + b'","budget_policy_revision":1,"budget_policy_sha256":"'
        + b"d" * 64
        + b'","budget_quota_key":"sina-quota-v1","canonical_model":'
        b'"financial-statement","capture_intent_id":"intent-1",'
        b'"expected_grant_revision":1,"expected_rights_evidence_reference":'
        b'"offline-rights-record-1","grant_id":"grant-1",'
        b'"host":"api.sina.example","operation":"query",'
        b'"principal_scope":"offline-worker","request_host":"api.sina.example",'
        b'"request_id":"request-1","request_method":"GET",'
        b'"request_path":"/v1/financial","request_scheme":"https",'
        b'"request_summary_sha256":"'
        + b"a"
        * 64
        + b'","run_key":"run-1","scope_version":3,"source":"sina",'
        b'"source_policy_id":"sina-financial-policy",'
        b'"source_policy_revision":"rev-1","source_policy_sha256":"' + b"b" * 64 + b'"}'
    )
    assert _scope_v2_bytes(_scope_v2()) == (
        b'{"attempt_id":"attempt-1","attempt_no":1,"budget_binding_sha256":"'
        + b"c" * 64
        + b'","budget_policy_revision":1,"budget_policy_sha256":"'
        + b"d" * 64
        + b'","budget_quota_key":"sina-quota-v1","canonical_model":'
        b'"financial-statement","capture_intent_id":"intent-1",'
        b'"expected_grant_revision":1,"grant_id":"grant-1",'
        b'"host":"api.sina.example","operation":"query",'
        b'"principal_scope":"offline-worker","request_id":"request-1",'
        b'"request_summary_sha256":"'
        + b"a"
        * 64
        + b'","run_key":"run-1","scope_version":2,"source":"sina",'
        b'"source_policy_id":"sina-financial-policy",'
        b'"source_policy_revision":"rev-1","source_policy_sha256":"' + b"b" * 64 + b'"}'
    )


@pytest.mark.parametrize(
    "path",
    [
        "relative/path",
        "//api.sina.example/path",
        "/v1//financial",
        "/v1/./financial",
        "/v1/../financial",
        "/v1/%2e%2e/financial",
        "/v1/financial?format=json",
        "/v1/financial#fragment",
        "/v1\\financial",
        "/café",
        "/" + "a" * 2048,
    ],
)
def test_v3_scope_rejects_noncanonical_or_aliased_paths(path: str) -> None:
    """V3 path pins use one bounded ASCII origin-form spelling without aliases."""
    with pytest.raises(SourceAttemptInputError, match="request path"):
        replace(_scope_v3(), request_path=path)


def test_v3_scope_requires_exact_https_get_and_attempt_host_binding() -> None:
    """Request-target fields are canonical and the persisted host equals the grant host."""
    with pytest.raises(SourceAttemptInputError, match="scheme"):
        replace(_scope_v3(), request_scheme="http")
    with pytest.raises(SourceAttemptInputError, match="method"):
        replace(_scope_v3(), request_method="POST")
    with pytest.raises(SourceAttemptInputError, match="attempt host"):
        replace(_scope_v3(), request_host="other.sina.example")


def test_v3_matching_grant_and_budget_pins_reserve_once_and_lookup_exact_scope(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching reference and current budget pins produce one non-reusable row."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    reservation = gate.reserve_attempt_v3(scope)
    lookup = gate.lookup_attempt_v3(scope)

    assert reservation.status is ReservationStatus.RESERVED_NEW
    assert isinstance(lookup, AttemptLookupReserved)
    assert lookup.attempt_id == scope.attempt.attempt_id
    assert lookup.reserved_at_us == NOW_US
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.DUPLICATE
    with gate_engine.connect() as connection:
        sidecar = (
            connection.execute(
                text(
                    "SELECT scope_json, scope_sha256 FROM od_source_attempt_scope_v3 "
                    "WHERE attempt_id = :attempt_id"
                ),
                {"attempt_id": scope.attempt.attempt_id},
            )
            .mappings()
            .one()
        )
    assert json.loads(sidecar["scope_json"])["expected_rights_evidence_reference"] == (
        RIGHTS_REFERENCE
    )
    assert (
        hashlib.sha256(sidecar["scope_json"].encode("utf-8")).hexdigest() == sidecar["scope_sha256"]
    )


def test_v3_reference_mismatch_denies_before_bucket_debit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed exact grant reference is durably denied without consuming quota."""
    gate = SourceAttemptGate(gate_engine)
    matching_scope = _scope_v3()
    _seed_allowed_grant(gate_engine, matching_scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    mismatched_scope = replace(
        matching_scope,
        attempt=replace(
            matching_scope.attempt,
            expected_rights_evidence_reference="offline-rights-record-other",
        ),
    )

    result = gate.reserve_attempt_v3(mismatched_scope)
    lookup = gate.lookup_attempt_v3(mismatched_scope)

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "grant_evidence_reference_mismatch"
    assert isinstance(lookup, AttemptLookupDenied)
    assert lookup.denial_code == "grant_evidence_reference_mismatch"
    with gate_engine.connect() as connection:
        tokens = connection.execute(
            text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota_key"),
            {"quota_key": QUOTA_KEY},
        ).scalar_one()
    assert tokens == 1_000_000


@pytest.mark.parametrize(
    "pin_name",
    [
        "binding",
        "policy_revision",
        "policy_sha256",
    ],
)
def test_v3_stale_budget_pins_deny_without_debit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    pin_name: str,
) -> None:
    """Binding or policy drift is checked under the reservation write lock."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    if pin_name == "binding":
        stale_scope = replace(scope, budget_binding_sha256="e" * 64)
    elif pin_name == "policy_revision":
        stale_scope = replace(scope, budget_policy_revision=2)
    else:
        stale_scope = replace(scope, budget_policy_sha256="f" * 64)

    result = gate.reserve_attempt_v3(stale_scope)

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "budget_scope_mismatch"
    with gate_engine.connect() as connection:
        tokens = connection.execute(
            text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota_key"),
            {"quota_key": QUOTA_KEY},
        ).scalar_one()
    assert tokens == 1_000_000


def test_v3_detects_a_committed_budget_policy_update_atomically(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's old pins cannot debit a bucket after policy state changes."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    with gate_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE od_source_gate_policies SET policy_revision = 2, "
                "policy_sha256 = :digest WHERE quota_key = :quota_key"
            ),
            {"digest": "e" * 64, "quota_key": QUOTA_KEY},
        )

    result = gate.reserve_attempt_v3(scope)

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "budget_scope_mismatch"
    with gate_engine.connect() as connection:
        tokens = connection.execute(
            text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota_key"),
            {"quota_key": QUOTA_KEY},
        ).scalar_one()
    assert tokens == 1_000_000


def test_v3_changed_reference_conflicts_with_an_existing_attempt_identity(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The expected reference participates in duplicate classification and lookup."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW
    changed = replace(
        scope,
        attempt=replace(scope.attempt, expected_rights_evidence_reference="other-reference"),
    )

    assert gate.reserve_attempt_v3(changed).status is ReservationStatus.ID_CONFLICT
    lookup = gate.lookup_attempt_v3(changed)
    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCOPE_MISMATCH


def test_v3_changed_request_path_conflicts_with_an_existing_attempt_identity(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact request path participates in durable duplicate identity and lookup."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt_v3(scope).status is ReservationStatus.RESERVED_NEW
    changed = replace(scope, request_path="/v1/other")

    assert gate.reserve_attempt_v3(changed).status is ReservationStatus.ID_CONFLICT
    lookup = gate.lookup_attempt_v3(changed)
    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCOPE_MISMATCH


def test_v3_sidecar_failure_rolls_back_attempt_bucket_and_clock(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v3 sidecar and any quota debit cannot commit separately from the base row."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    insert_scope = gate._insert_attempt_scope_v3

    def fail_after_insert(*args: object) -> None:
        insert_scope(*args)  # type: ignore[arg-type]
        raise SQLAlchemyError("synthetic sidecar acknowledgement failure")

    monkeypatch.setattr(gate, "_insert_attempt_scope_v3", fail_after_insert)
    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt_v3(scope)

    with gate_engine.connect() as connection:
        attempt_count = connection.execute(
            text("SELECT COUNT(*) FROM od_source_attempt_reservations")
        ).scalar_one()
        sidecar_count = connection.execute(
            text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")
        ).scalar_one()
        tokens = connection.execute(
            text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota_key"),
            {"quota_key": QUOTA_KEY},
        ).scalar_one()
        highwater = connection.execute(
            text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
        ).scalar_one()
    assert attempt_count == sidecar_count == 0
    assert tokens == 1_000_000
    assert highwater == 0


def test_v2_rows_remain_verifiable_and_are_never_backfilled_as_v3(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v2 API remains usable under sg0003 while v3 treats old rows as legacy."""
    gate = SourceAttemptGate(gate_engine)
    v2_scope = _scope_v2()
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    v2_result = gate.reserve_attempt_v2(v2_scope)
    v2_lookup = gate.lookup_attempt_v2(v2_scope)
    v3_scope = _scope_v3(
        replace(v2_scope.attempt, expected_rights_evidence_reference=RIGHTS_REFERENCE)
    )
    v3_lookup = gate.lookup_attempt_v3(v3_scope)

    assert v2_result.status is ReservationStatus.DENIED
    assert isinstance(v2_lookup, AttemptLookupDenied)
    assert isinstance(v3_lookup, AttemptLookupIndeterminate)
    assert v3_lookup.reason_code is AttemptLookupReasonCode.LEGACY_SCOPE_UNVERIFIABLE
    assert gate.reserve_attempt_v3(v3_scope).status is ReservationStatus.ID_CONFLICT
    with gate_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")).scalar_one()
            == 0
        )


@pytest.mark.parametrize("include_v3", [False, True])
def test_grant_inspection_schema_accepts_exact_v2_and_v3_revisions(
    tmp_path: Path,
    include_v3: bool,
) -> None:
    """Read-only grant inspection keeps its v2 behavior after the additive v3 migration."""
    engine = _new_engine(tmp_path, f"grant-inspection-sg000{3 if include_v3 else 2}.sqlite")
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine, include_v3=include_v3)
    gate = SourceAttemptGate(engine)
    readonly_connection = gate._open_readonly_connection()
    try:
        assert gate._source_gate_snapshot_schema_valid(readonly_connection)
    finally:
        readonly_connection.close()
        engine.dispose()


def test_grant_inspection_still_matches_a_valid_grant_under_v3_marker(
    gate_engine: Engine,
) -> None:
    """The advisory V2 grant inspection remains available after sg0003 upgrade."""
    scope = _scope_v3()
    _seed_allowed_grant(gate_engine, scope)
    gate = SourceAttemptGate(gate_engine)
    request = SourceGrantInspectionRequest(
        grant_id=scope.attempt.grant_id,
        revision=scope.attempt.expected_revision,
        source=scope.attempt.source,
        canonical_model=scope.attempt.canonical_model,
        operation=scope.attempt.operation,
        principal_scope=scope.attempt.principal_scope,
        host=scope.attempt.host,
    )

    result = gate.inspect_grant(request)

    assert result.status is SourceGrantInspectionStatus.MATCHED


def test_grant_inspection_fails_closed_on_incomplete_v3_schema(tmp_path: Path) -> None:
    """A v3 marker with a missing immutable trigger is not treated as valid."""
    engine = _new_engine(tmp_path, "grant-inspection-incomplete-v3.sqlite")
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER trg_od_source_attempt_scope_v3_immutable_delete")

    gate = SourceAttemptGate(engine)
    readonly_connection = gate._open_readonly_connection()
    try:
        assert not gate._source_gate_snapshot_schema_valid(readonly_connection)
    finally:
        readonly_connection.close()
        engine.dispose()


def test_v3_api_fails_closed_without_v3_migration(tmp_path: Path) -> None:
    """A v2-only marker cannot silently authorize a v3 reservation or lookup."""
    engine = _new_engine(tmp_path, "source-gate-v2-only.sqlite")
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine, include_v3=False)
    gate = SourceAttemptGate(engine)
    scope = _scope_v3()
    with pytest.raises(SourceAttemptGateUnavailable, match="v3 schema"):
        gate.reserve_attempt_v3(scope)
    lookup = gate.lookup_attempt_v3(scope)
    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
    engine.dispose()


def test_v3_api_fails_closed_on_unexpected_quota_trigger(gate_engine: Engine) -> None:
    """An extra quota-table trigger cannot alter the full pinned V3 schema."""
    with gate_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_od_source_gate_state_quota_bypass "
            "AFTER UPDATE ON od_source_gate_state BEGIN "
            "UPDATE od_source_gate_state SET tokens_micro = OLD.tokens_micro "
            "WHERE quota_key = NEW.quota_key; END"
        )

    gate = SourceAttemptGate(gate_engine)
    scope = _scope_v3()
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


def test_scope_v3_migration_is_additive_and_downgrade_requires_empty_sidecar(
    tmp_path: Path,
) -> None:
    """The revision validates exact v2, adds no backfill, and refuses evidence loss."""
    engine = _new_engine(tmp_path, "source-gate-v3-migration.sqlite")
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine, include_v3=False)
    migration = _load_scope_v3_migration()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V2_REVISION},
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V3_REVISION},
        )
        assert validate_migration_state(connection) == V3_REVISION
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM od_source_attempt_scope_v3"
            ).scalar_one()
            == 0
        )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO od_source_attempt_reservations "
            "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
            "canonical_model, operation, principal_scope, host, run_key, outcome, denial_code, "
            "retry_after_us, decided_at_us, reserved_at_us, quota_key, policy_revision, "
            "rate_microtokens_per_second, capacity_microtokens) VALUES "
            "('migration-attempt', 'migration-request', 1, 'migration-grant', 1, 'sina', "
            "'financial-statement', 'query', 'offline-worker', 'api.sina.example', 'run-1', "
            "'DENIED', 'grant_not_found', NULL, 1, NULL, NULL, NULL, NULL, NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO od_source_attempt_scope_v3 "
            "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
            "VALUES ('migration-attempt', 3, '{}', :digest, 1)",
            {"digest": "a" * 64},
        )
        with (
            Operations.context(MigrationContext.configure(connection)),
            pytest.raises(RuntimeError, match="evidence must be archived"),
        ):
            migration.downgrade()

    engine.dispose()


def test_scope_v3_migration_can_downgrade_an_empty_sidecar(tmp_path: Path) -> None:
    """An exact, empty v3 sidecar can be removed without altering the v2 schema."""
    engine = _new_engine(tmp_path, "source-gate-v3-empty-downgrade.sqlite")
    _install_v1_schema(engine)
    _install_scope_v3_schema(engine, include_v3=False)
    migration = _load_scope_v3_migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V3_REVISION},
        )

    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V2_REVISION},
        )
        assert validate_migration_state(connection) == V2_REVISION
        assert (
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name = 'od_source_attempt_scope_v3'"
            ).scalar_one_or_none()
            is None
        )
    engine.dispose()


def test_v3_attempt_does_not_consume_another_grants_shared_bucket(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different grant owners still serialize against the same pinned quota key."""
    gate = SourceAttemptGate(gate_engine)
    first_scope = _scope_v3()
    _seed_allowed_grant(gate_engine, first_scope)
    second_scope = _scope_v3(
        _attempt(
            attempt_id="attempt-2",
            request_id="request-2",
            grant_id="grant-2",
            principal_scope="offline-worker-2",
            expected_reference="offline-rights-record-2",
        )
    )
    with gate_engine.begin() as connection:
        evidence_sha256 = hashlib.sha256(b"synthetic evidence body 2").hexdigest()
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant_id, :source, :model, 'query', :principal, 'ALLOWED', 1, 0, :expires, "
                "'[]', :reference, :digest, :note, :actor, 1, 1, 1)"
            ),
            {
                "grant_id": second_scope.attempt.grant_id,
                "source": second_scope.attempt.source,
                "model": second_scope.attempt.canonical_model,
                "principal": second_scope.attempt.principal_scope,
                "expires": MAX_TIMESTAMP_US,
                "reference": second_scope.attempt.expected_rights_evidence_reference,
                "digest": evidence_sha256,
                "note": "Synthetic declaration for offline tests.",
                "actor": "offline-test",
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"),
            {"grant_id": second_scope.attempt.grant_id, "host": second_scope.attempt.host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant_id, 1, 1, 1, 'offline-test', 'synthetic-test')"
            ),
            {"grant_id": second_scope.attempt.grant_id},
        )
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    assert gate.reserve_attempt_v3(first_scope).status is ReservationStatus.RESERVED_NEW
    second_result = gate.reserve_attempt_v3(second_scope)

    assert second_result.status is ReservationStatus.DENIED
    assert second_result.denial_code == "source_rate_limited"
