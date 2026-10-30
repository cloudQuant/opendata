"""Offline behavior tests for the local CSI500 THS token-bucket provisioner."""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from alembic_source_gate import schema_contract
from alembic_source_gate.path_contract import DatabaseFileIdentity, inspect_existing_database_path
from opendata.data import source_gate_quota_provisioning as quota_provisioning
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptInputError,
    SourceAttemptScopeV2,
    SourceAttemptScopeV3,
    TransportAttempt,
)
from opendata.data.source_gate_quota_provisioning import (
    Csi500SampleQuotaCleanupError,
    Csi500SampleQuotaCommittedCleanupError,
    Csi500SampleQuotaCommitUncertainError,
    Csi500SampleQuotaConflictError,
    Csi500SampleQuotaGrantError,
    Csi500SampleQuotaInputError,
    Csi500SampleQuotaReceipt,
    Csi500SampleQuotaStatus,
    Csi500SampleQuotaUnavailableError,
    _require_exact_sample_host_set,
    provision_csi500_sample_quota,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisionRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from sqlalchemy.engine import Engine


_PRINCIPAL = "synthetic-csi500-sample"
_GRANT_ID = "synthetic-csi500-grant"
_ACTOR = "synthetic-operator"
_EVIDENCE_REFERENCE = "operator-declaration:synthetic-csi500-sample"
_EVIDENCE_SHA256 = hashlib.sha256(b"synthetic authorization provenance").hexdigest()
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class _PreparedGate:
    """One temporary, migrated gate containing a single public-API grant."""

    path: Path
    identity: DatabaseFileIdentity
    engine: Engine
    gate: SourceAttemptGate
    expires_at: datetime


@pytest.fixture
def prepared_gate(tmp_path: Path) -> Generator[_PreparedGate, None, None]:
    """Create one isolated SQLite gate, migrate it, and issue one exact grant."""
    path = tmp_path / "csi500-sample.sqlite"
    with closing(sqlite3.connect(path)):
        pass
    setup_engine = sa.create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with setup_engine.begin() as connection:
            schema_contract._apply_v1_reference(connection)
    finally:
        setup_engine.dispose()

    from scripts import migrate_source_gate

    assert migrate_source_gate.main(["--database-path", str(path)]) == 0
    engine = sa.create_engine(f"sqlite+pysqlite:///{path}", poolclass=sa.pool.NullPool)
    gate = SourceAttemptGate(engine)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=10)
    grant_request = SourceGrantProvisionRequest(
        grant_id=_GRANT_ID,
        revision=1,
        source="ths",
        canonical_model="index_daily",
        operation="query",
        principal_scope=_PRINCIPAL,
        host="fuyao.aicubes.cn",
        not_before=now - timedelta(seconds=1),
        expires_at=expires_at,
        rights_evidence_reference=_EVIDENCE_REFERENCE,
        rights_evidence_sha256=_EVIDENCE_SHA256,
        operator_confirms_authorized_use=True,
        operator_attestation_note="Synthetic declaration for an offline test only.",
        attested_by=_ACTOR,
    )
    SourceGrantProvisioner(gate).provision(grant_request)
    canonical_path, identity = inspect_existing_database_path(path)
    assert canonical_path == path
    yield _PreparedGate(path, identity, engine, gate, expires_at)
    engine.dispose()


def _upgrade_to_revision(path: Path, revision: str) -> None:
    """Apply one exact source-gate revision to a disposable test database."""
    canonical_path, identity = inspect_existing_database_path(path)
    config = Config(str(_REPOSITORY_ROOT / "alembic_source_gate.ini"))
    config.attributes["source_gate_path"] = canonical_path
    config.attributes["source_gate_file_identity"] = identity
    command.upgrade(config, revision)


def _provision(prepared: _PreparedGate, **overrides: object) -> Csi500SampleQuotaReceipt:
    """Issue the fixed local bucket configuration through its public function."""
    values: dict[str, object] = {
        "database_path": prepared.path,
        "expected_identity": prepared.identity,
        "grant_id": _GRANT_ID,
        "principal_scope": _PRINCIPAL,
        "actor": _ACTOR,
        "evidence_reference": _EVIDENCE_REFERENCE,
        "expires_at": prepared.expires_at,
    }
    values.update(overrides)
    return provision_csi500_sample_quota(**values)  # type: ignore[arg-type]


def _attempt(*, index: int, grant_id: str = _GRANT_ID) -> TransportAttempt:
    """Build one synthetic V1 THS attempt for the public gate method."""
    return TransportAttempt(
        attempt_id=f"attempt-{index}",
        request_id=f"request-{index}",
        attempt_no=1,
        grant_id=grant_id,
        expected_revision=1,
        source="ths",
        canonical_model="index_daily",
        operation="query",
        principal_scope=_PRINCIPAL,
        host="fuyao.aicubes.cn",
        expected_rights_evidence_reference=_EVIDENCE_REFERENCE,
    )


def _v2_scope(
    receipt: Csi500SampleQuotaReceipt,
    *,
    index: int,
    policy_sha256: str | None = None,
) -> SourceAttemptScopeV2:
    """Bind an explicit V2 attempt to the provisioned receipt hashes."""
    return SourceAttemptScopeV2(
        attempt=TransportAttempt(
            attempt_id=f"v2-attempt-{index}",
            request_id=f"v2-request-{index}",
            attempt_no=1,
            grant_id=_GRANT_ID,
            expected_revision=1,
            source="ths",
            canonical_model="index_daily",
            operation="query",
            principal_scope=_PRINCIPAL,
            host="fuyao.aicubes.cn",
            run_key="synthetic-run",
        ),
        capture_intent_id="synthetic-capture-intent",
        request_summary_sha256=hashlib.sha256(b"synthetic request summary").hexdigest(),
        source_policy_id="synthetic-source-policy",
        source_policy_revision="synthetic-revision-1",
        source_policy_sha256=hashlib.sha256(b"synthetic source policy").hexdigest(),
        budget_binding_sha256=receipt.binding_sha256,
        budget_quota_key=receipt.quota_key,
        budget_policy_revision=receipt.policy_revision,
        budget_policy_sha256=policy_sha256 or receipt.policy_sha256,
    )


def _v3_scope(
    receipt: Csi500SampleQuotaReceipt,
    *,
    index: int,
    policy_sha256: str | None = None,
) -> SourceAttemptScopeV3:
    """Bind an explicit V3 request identity to the provisioned local hashes."""
    return SourceAttemptScopeV3(
        attempt=TransportAttempt(
            attempt_id=f"v3-attempt-{index}",
            request_id=f"v3-request-{index}",
            attempt_no=1,
            grant_id=_GRANT_ID,
            expected_revision=1,
            source="ths",
            canonical_model="index_daily",
            operation="query",
            principal_scope=_PRINCIPAL,
            host="fuyao.aicubes.cn",
            run_key="synthetic-run",
            expected_rights_evidence_reference=_EVIDENCE_REFERENCE,
        ),
        request_scheme="https",
        request_method="GET",
        request_host="fuyao.aicubes.cn",
        request_path="/v1/index_daily",
        capture_intent_id="synthetic-capture-intent",
        request_summary_sha256=hashlib.sha256(b"synthetic V3 request summary").hexdigest(),
        source_policy_id="synthetic-source-policy",
        source_policy_revision="synthetic-revision-1",
        source_policy_sha256=hashlib.sha256(b"synthetic source policy").hexdigest(),
        budget_binding_sha256=receipt.binding_sha256,
        budget_quota_key=receipt.quota_key,
        budget_policy_revision=receipt.policy_revision,
        budget_policy_sha256=policy_sha256 or receipt.policy_sha256,
    )


def test_initial_bucket_token_is_debited_by_the_v1_gate(
    prepared_gate: _PreparedGate,
) -> None:
    """V1 debits the initial token and immediately rate-limits another attempt."""
    receipt = _provision(prepared_gate)

    assert receipt.status == Csi500SampleQuotaStatus.PROVISIONED.value
    assert receipt.source == "ths"
    assert receipt.policy_revision == 1
    assert receipt.rate_microtokens_per_second == 1
    assert receipt.capacity_microtokens == 1_000_000
    assert len(receipt.binding_sha256) == 64
    assert len(receipt.policy_sha256) == 64
    first = prepared_gate.gate.reserve_attempt(_attempt(index=1))
    second = prepared_gate.gate.reserve_attempt(_attempt(index=2))

    assert first.status is ReservationStatus.RESERVED_NEW
    assert second.status is ReservationStatus.DENIED
    assert second.denial_code == "source_rate_limited"


def test_receipt_hashes_are_accepted_by_explicit_v2_scope_pinning(
    prepared_gate: _PreparedGate,
) -> None:
    """V2 only admits a scope whose binding and policy pins match persisted rows."""
    receipt = _provision(prepared_gate)
    wrong_policy = _v2_scope(
        receipt,
        index=1,
        policy_sha256=hashlib.sha256(b"wrong policy").hexdigest(),
    )
    denied = prepared_gate.gate.reserve_attempt_v2(wrong_policy)
    matching = prepared_gate.gate.reserve_attempt_v2(_v2_scope(receipt, index=2))

    assert denied.status is ReservationStatus.DENIED
    assert denied.denial_code == "budget_scope_mismatch"
    assert matching.status is ReservationStatus.RESERVED_NEW


def test_receipt_hashes_are_accepted_by_explicit_v3_scope_pinning(
    prepared_gate: _PreparedGate,
) -> None:
    """V3 scope pins still match the bucket seeded in a complete V4 gate."""
    receipt = _provision(prepared_gate)
    wrong_policy = _v3_scope(
        receipt,
        index=1,
        policy_sha256=hashlib.sha256(b"wrong V3 policy").hexdigest(),
    )
    denied = prepared_gate.gate.reserve_attempt_v3(wrong_policy)
    matching = prepared_gate.gate.reserve_attempt_v3(_v3_scope(receipt, index=2))

    assert denied.status is ReservationStatus.DENIED
    assert denied.denial_code == "budget_scope_mismatch"
    assert matching.status is ReservationStatus.RESERVED_NEW


def test_v3_schema_is_rejected_even_when_its_snapshot_is_complete(tmp_path: Path) -> None:
    """Quota setup requires the exact V4 marker and refuses a complete V3 database."""
    path = tmp_path / "v3-source-gate.sqlite"
    with closing(sqlite3.connect(path)):
        pass
    engine = sa.create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as connection:
            schema_contract._apply_v1_reference(connection)
    finally:
        engine.dispose()
    _upgrade_to_revision(path, schema_contract.V3_REVISION)
    _canonical_path, identity = inspect_existing_database_path(path)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="schema is unavailable"):
        provision_csi500_sample_quota(
            database_path=path,
            expected_identity=identity,
            grant_id=_GRANT_ID,
            principal_scope=_PRINCIPAL,
            actor=_ACTOR,
            evidence_reference=_EVIDENCE_REFERENCE,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )


def test_sample_grant_rejects_any_host_set_beyond_exact_fuyao() -> None:
    """The local sample declaration requires Fuyao as its only sealed host."""
    _require_exact_sample_host_set([{"host": "fuyao.aicubes.cn"}])

    with pytest.raises(Csi500SampleQuotaGrantError, match="exact Fuyao host only"):
        _require_exact_sample_host_set(
            [{"host": "fuyao.aicubes.cn"}, {"host": "secondary.example"}]
        )


def test_repeat_provision_fails_closed_without_reconfiguring_existing_rows(
    prepared_gate: _PreparedGate,
) -> None:
    """A second call sees the original configuration and cannot update it."""
    first = _provision(prepared_gate)

    with pytest.raises(Csi500SampleQuotaConflictError):
        _provision(prepared_gate)

    assert prepared_gate.gate.reserve_attempt(_attempt(index=3)).status is (
        ReservationStatus.RESERVED_NEW
    )
    assert first.policy_sha256


@pytest.mark.parametrize("reserve", (False, True), ids=("lock-only", "lock-and-permit"))
def test_existing_v4_one_shot_state_blocks_quota_reprovisioning(
    prepared_gate: _PreparedGate,
    reserve: bool,
) -> None:
    """An existing V4 lock or consumed permit makes setup non-repeatable."""
    receipt = _provision(prepared_gate)
    scope = _v3_scope(receipt, index=31)
    lock_receipt = prepared_gate.gate.lock_one_shot_scope(scope)
    assert lock_receipt.scope_sha256
    if reserve:
        reservation = prepared_gate.gate.reserve_one_shot_attempt(scope)
        assert reservation.status is ReservationStatus.RESERVED_NEW

    with pytest.raises(Csi500SampleQuotaConflictError):
        _provision(prepared_gate)


def test_v4_configure_lock_reserve_and_exact_replay_without_http(
    prepared_gate: _PreparedGate,
) -> None:
    """A locked V4 fixture uses the one-shot reservation API and classifies replay."""
    receipt = _provision(prepared_gate)
    scope = _v3_scope(receipt, index=32)

    lock_receipt = prepared_gate.gate.lock_one_shot_scope(scope)
    first = prepared_gate.gate.reserve_one_shot_attempt(scope)
    replay = prepared_gate.gate.reserve_one_shot_attempt(scope)

    assert lock_receipt.scope_sha256
    assert first.status is ReservationStatus.RESERVED_NEW
    assert replay.status is ReservationStatus.DUPLICATE


def test_wrong_principal_is_rejected_then_exact_grant_can_be_provisioned(
    prepared_gate: _PreparedGate,
) -> None:
    """Grant mismatch rolls back the writer transaction before later valid use."""
    with pytest.raises(Csi500SampleQuotaGrantError):
        _provision(prepared_gate, principal_scope="another-principal")

    receipt = _provision(prepared_gate)

    assert receipt.grant_id == _GRANT_ID
    assert prepared_gate.gate.reserve_attempt(_attempt(index=4)).status is (
        ReservationStatus.RESERVED_NEW
    )


def test_mismatched_target_identity_is_rejected_before_storage_access(
    prepared_gate: _PreparedGate,
) -> None:
    """A caller cannot redirect a gate provision to a replacement inode."""
    wrong_identity = DatabaseFileIdentity(
        device=prepared_gate.identity.device,
        inode=prepared_gate.identity.inode + 1,
    )

    with pytest.raises(Exception, match="target identity"):
        _provision(prepared_gate, expected_identity=wrong_identity)

    assert _provision(prepared_gate).status == Csi500SampleQuotaStatus.PROVISIONED.value


def test_declared_grant_expiry_must_be_shorter_than_refill_window(
    prepared_gate: _PreparedGate,
) -> None:
    """The declared grant has a bounded lifetime; this is not a persistent budget."""
    too_late = datetime.now(timezone.utc) + timedelta(seconds=1_000_000)

    with pytest.raises(Csi500SampleQuotaInputError, match="refill-window bound"):
        _provision(prepared_gate, expires_at=too_late)

    assert _provision(prepared_gate).status == Csi500SampleQuotaStatus.PROVISIONED.value


def test_lost_commit_ack_is_ambiguous_and_never_retried_as_an_update(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed but unacknowledged write is reported and cannot be replayed."""
    original_commit: Callable[[SourceAttemptGate, object], None] = SourceAttemptGate._commit

    def commit_then_drop_ack(gate: SourceAttemptGate, connection: object) -> None:
        original_commit(gate, connection)
        raise RuntimeError("synthetic lost acknowledgement")

    monkeypatch.setattr(SourceAttemptGate, "_commit", commit_then_drop_ack)
    with pytest.raises(Csi500SampleQuotaCommitUncertainError):
        _provision(prepared_gate)
    monkeypatch.setattr(SourceAttemptGate, "_commit", original_commit)

    with pytest.raises(Csi500SampleQuotaConflictError):
        _provision(prepared_gate)


def test_engine_cleanup_failure_preserves_uncertain_commit_classification(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup error cannot replace the primary uncertain-commit outcome."""
    original_commit: Callable[[SourceAttemptGate, object], None] = SourceAttemptGate._commit
    original_open_engine = quota_provisioning._open_explicit_engine

    def commit_then_drop_ack(gate: SourceAttemptGate, connection: object) -> None:
        original_commit(gate, connection)
        raise RuntimeError("synthetic lost acknowledgement")

    def open_engine_with_dispose_failure(path: Path) -> Engine:
        engine = original_open_engine(path)

        def fail_dispose() -> None:
            raise RuntimeError("synthetic engine cleanup failure")

        monkeypatch.setattr(engine, "dispose", fail_dispose)
        return engine

    monkeypatch.setattr(SourceAttemptGate, "_commit", commit_then_drop_ack)
    monkeypatch.setattr(
        quota_provisioning,
        "_open_explicit_engine",
        open_engine_with_dispose_failure,
    )

    with pytest.raises(Csi500SampleQuotaCommitUncertainError, match="engine cleanup failed"):
        _provision(prepared_gate)


def test_concurrent_provisioners_commit_at_most_one_initial_configuration(
    prepared_gate: _PreparedGate,
) -> None:
    """SQLite's writer lock serializes calls; only one can install the bucket."""

    def provision_or_conflict() -> str:
        try:
            _provision(prepared_gate)
        except Csi500SampleQuotaConflictError:
            return "conflict"
        return "provisioned"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: provision_or_conflict(), range(2)))

    assert sorted(results) == ["conflict", "provisioned"]
    reservation: AttemptReservation = prepared_gate.gate.reserve_attempt(_attempt(index=5))
    assert reservation.status is ReservationStatus.RESERVED_NEW


class _ExplodingTimezone(tzinfo):
    """A malformed timezone object used to exercise datetime conversion failure."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        del dt
        raise RuntimeError("synthetic timezone failure")

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None

    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "synthetic"


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        ({"grant_id": "invalid grant id"}, Csi500SampleQuotaInputError, "grant_id is invalid"),
        (
            {"expected_identity": object()},
            Csi500SampleQuotaInputError,
            "expected_identity must be a DatabaseFileIdentity",
        ),
        (
            {"expected_identity": DatabaseFileIdentity(device=True, inode=1)},
            Csi500SampleQuotaInputError,
            "expected_identity is invalid",
        ),
        (
            {"evidence_reference": None},
            Csi500SampleQuotaInputError,
            "evidence_reference must be text",
        ),
        (
            {"evidence_reference": "operator:bad\nreference"},
            Csi500SampleQuotaInputError,
            "evidence_reference is invalid",
        ),
        (
            {"evidence_reference": "operator/.env/template"},
            Csi500SampleQuotaInputError,
            "credential-like material",
        ),
        (
            {"evidence_reference": "https://operator:token@docs.example/reference"},
            Csi500SampleQuotaInputError,
            "credential-like material",
        ),
        (
            {"evidence_reference": "operator%3ask-secret-abcdefghijklmnop"},
            Csi500SampleQuotaInputError,
            "credential-like material",
        ),
        (
            {"expires_at": "2026-10-31T00:00:00Z"},
            Csi500SampleQuotaInputError,
            "timezone-aware datetime",
        ),
        (
            {"expires_at": datetime(2026, 10, 31)},
            Csi500SampleQuotaInputError,
            "timezone-aware datetime",
        ),
        (
            {"expires_at": datetime(2026, 10, 31, tzinfo=_ExplodingTimezone())},
            Csi500SampleQuotaInputError,
            "outside the supported range",
        ),
        (
            {"expires_at": datetime(1969, 12, 31, tzinfo=timezone.utc)},
            Csi500SampleQuotaInputError,
            "outside the supported range",
        ),
    ],
    ids=(
        "identifier",
        "identity-type",
        "identity-values",
        "evidence-type",
        "evidence-control",
        "dotenv-reference",
        "uri-userinfo",
        "encoded-credential",
        "expiry-type",
        "naive-expiry",
        "broken-timezone",
        "pre-epoch-expiry",
    ),
)
def test_invalid_bounded_inputs_fail_before_provisioning(
    prepared_gate: _PreparedGate,
    overrides: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    """Malformed values and credential-shaped references never reach the gate."""
    with pytest.raises(error, match=message):
        _provision(prepared_gate, **overrides)


def test_nonexistent_database_path_is_rejected_before_engine_open(
    prepared_gate: _PreparedGate,
    tmp_path: Path,
) -> None:
    """The provisioner never creates a missing SQLite target as a side effect."""
    missing_path = tmp_path / "missing-gate.sqlite"

    with pytest.raises(Csi500SampleQuotaInputError, match="existing canonical regular file"):
        _provision(prepared_gate, database_path=missing_path)

    assert not missing_path.exists()


def test_receipt_repr_redacts_scope_and_hash_identifiers(prepared_gate: _PreparedGate) -> None:
    """The safe receipt representation does not echo local scope or file pins."""
    receipt = _provision(prepared_gate)
    rendered = repr(receipt)

    assert "<redacted>" in rendered
    assert _GRANT_ID not in rendered
    assert receipt.binding_sha256 not in rendered
    assert receipt.target_identity_sha256 not in rendered


def test_invalid_active_expiry_is_rejected_against_gate_clock(
    prepared_gate: _PreparedGate,
) -> None:
    """A syntactically valid but expired declaration fails before quota writes."""
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(Csi500SampleQuotaGrantError, match="expired or not yet usable"):
        _provision(prepared_gate, expires_at=expired_at)

    assert _provision(prepared_gate).status is Csi500SampleQuotaStatus.PROVISIONED


def test_second_preexisting_grant_blocks_sample_bucket_configuration(
    prepared_gate: _PreparedGate,
) -> None:
    """The quota setup refuses a mixed gate with more than its exact one grant."""
    now = datetime.now(timezone.utc)
    request = SourceGrantProvisionRequest(
        grant_id="second-synthetic-grant",
        revision=1,
        source="ths",
        canonical_model="index_daily",
        operation="query",
        principal_scope=_PRINCIPAL,
        host="fuyao.aicubes.cn",
        not_before=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        rights_evidence_reference=_EVIDENCE_REFERENCE,
        rights_evidence_sha256=_EVIDENCE_SHA256,
        operator_confirms_authorized_use=True,
        operator_attestation_note="Synthetic second grant for an offline test only.",
        attested_by=_ACTOR,
    )
    SourceGrantProvisioner(prepared_gate.gate).provision(request)

    with pytest.raises(Csi500SampleQuotaConflictError, match="exactly its one"):
        _provision(prepared_gate)


def test_existing_denied_attempt_tombstone_blocks_quota_configuration(
    prepared_gate: _PreparedGate,
) -> None:
    """A prior denied attempt still makes the supposedly fresh sample gate mixed."""
    denial = prepared_gate.gate.reserve_attempt(_attempt(index=81))
    assert denial.status is ReservationStatus.DENIED
    assert denial.denial_code == "source_quota_unconfigured"

    with pytest.raises(Csi500SampleQuotaConflictError, match="old attempts"):
        _provision(prepared_gate)


def test_unknown_grant_id_is_not_substituted_with_the_only_existing_grant(
    prepared_gate: _PreparedGate,
) -> None:
    """The configured grant identifier must exactly select the sealed record."""
    with pytest.raises(Csi500SampleQuotaGrantError, match="exact source grant is unavailable"):
        _provision(prepared_gate, grant_id="another-synthetic-grant")


@pytest.mark.parametrize(
    "failure",
    [
        SourceAttemptInputError("synthetic grant input failure"),
        sa.exc.SQLAlchemyError("synthetic DB"),
    ],
    ids=("grant-input", "grant-query"),
)
def test_grant_inspection_input_and_storage_errors_fail_as_grant_errors(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """An invalid grant inspection does not leak driver details or proceed."""

    def fail_grant_check(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(SourceAttemptGate, "_grant_denial", fail_grant_check)

    with pytest.raises(Csi500SampleQuotaGrantError, match="exact source grant is unavailable"):
        _provision(prepared_gate)


def test_unexpected_grant_inspection_failure_fails_closed_as_unavailable(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected local grant inspection errors are surfaced as unavailable."""

    def fail_grant_check(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic internal failure")

    monkeypatch.setattr(SourceAttemptGate, "_grant_denial", fail_grant_check)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="grant state is unavailable"):
        _provision(prepared_gate)


def test_partial_quota_insert_failure_rolls_back_before_retry(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second-row failure rolls back the already inserted policy before retry."""
    original_insert = quota_provisioning._insert_quota_rows
    calls = 0

    def insert_one_then_fail(connection: object, **kwargs: object) -> None:
        nonlocal calls
        original_execute = connection.execute

        def fail_second_row(*args: object, **execute_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sa.exc.SQLAlchemyError("synthetic second-row write failure")
            return original_execute(*args, **execute_kwargs)

        monkeypatch.setattr(connection, "execute", fail_second_row)
        try:
            original_insert(connection, **kwargs)
        finally:
            monkeypatch.setattr(connection, "execute", original_execute)

    monkeypatch.setattr(quota_provisioning, "_insert_quota_rows", insert_one_then_fail)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="safely committed"):
        _provision(prepared_gate)

    assert calls == 2
    monkeypatch.undo()
    receipt = _provision(prepared_gate)
    assert receipt.status is Csi500SampleQuotaStatus.PROVISIONED


def test_unexpected_quota_write_failure_is_sanitized(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected pre-commit write exceptions do not leak implementation details."""

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr(quota_provisioning, "_insert_quota_rows", fail_insert)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="safely committed"):
        _provision(prepared_gate)


def test_unsealed_host_set_is_rejected_even_when_the_grant_record_matches(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sealed host audit must be present and must agree with the row set."""
    monkeypatch.setattr(SourceAttemptGate, "_grant_seal_event_matches", lambda *_args: False)

    with pytest.raises(Csi500SampleQuotaGrantError, match="seal event is unavailable"):
        _provision(prepared_gate)


def test_complete_v4_marker_must_also_have_a_complete_snapshot(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale or incomplete snapshot is rejected before grant/quota inspection."""
    monkeypatch.setattr(
        SourceAttemptGate,
        "_source_gate_snapshot_schema_valid",
        lambda *_args: False,
    )

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="schema is unavailable"):
        _provision(prepared_gate)


def test_schema_probe_exception_is_sanitized_as_unavailable(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected schema-probe errors are converted to one safe public error."""

    def fail_schema_probe(*_args: object) -> bool:
        raise RuntimeError("synthetic schema failure")

    monkeypatch.setattr(SourceAttemptGate, "_source_gate_snapshot_schema_valid", fail_schema_probe)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="schema is unavailable"):
        _provision(prepared_gate)


def test_non_sqlite_driver_is_rejected_by_schema_check() -> None:
    """Schema inspection refuses a connection backed by another DB driver."""
    fake_connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=object()))

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="schema is unavailable"):
        quota_provisioning._require_valid_schema(object(), fake_connection)


def test_grant_seal_check_rejects_non_sqlite_driver() -> None:
    """The host seal verifier fails closed when its raw SQLite event store is absent."""
    grant = {
        "revision": 1,
        "attested_by": _ACTOR,
        "expires_at_us": 1_800_000_000_000_000,
    }
    rows = iter(([grant], [{"host": "fuyao.aicubes.cn"}]))

    class _Result:
        def __init__(self, values: list[dict[str, object]]) -> None:
            self._values = values

        def mappings(self) -> _Result:
            return self

        def all(self) -> list[dict[str, object]]:
            return self._values

    def execute(*_args: object, **_kwargs: object) -> _Result:
        return _Result(next(rows))

    fake_connection = SimpleNamespace(
        execute=execute,
        connection=SimpleNamespace(driver_connection=object()),
    )
    fake_gate = SimpleNamespace(_grant_denial=lambda *_args: None)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="seal event is unavailable"):
        quota_provisioning._require_exact_grant(
            fake_gate,
            fake_connection,
            effective_now=1_700_000_000_000_000,
            grant_id=_GRANT_ID,
            principal_scope=_PRINCIPAL,
            actor=_ACTOR,
            evidence_reference=_EVIDENCE_REFERENCE,
            grant_expiry_us=1_800_000_000_000_000,
        )


def test_fresh_gate_state_query_failure_is_sanitized() -> None:
    """A failed state read is unavailable, never interpreted as an empty gate."""

    def fail_execute(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic read failure")

    fake_connection = SimpleNamespace(execute=fail_execute)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="state is unavailable"):
        quota_provisioning._require_fresh_gate(fake_connection)


def test_quota_readback_rejects_missing_or_altered_rows() -> None:
    """The read-back verifier rejects partial inserts and mismatched policy rows."""

    class _Result:
        def __init__(self, values: list[dict[str, object]]) -> None:
            self._values = values

        def mappings(self) -> _Result:
            return self

        def all(self) -> list[dict[str, object]]:
            return self._values

    class _ReadBackConnection:
        def __init__(self, values: list[list[dict[str, object]]]) -> None:
            self._values = iter(values)

        def execute(self, *_args: object, **_kwargs: object) -> _Result:
            return _Result(next(self._values))

    expected = {
        "quota_key": "csi500-fuyao-sample-204",
        "policy_revision": 1,
        "rate_microtokens_per_second": 1,
        "capacity_microtokens": 1_000_000,
        "policy_reference": _EVIDENCE_REFERENCE,
        "policy_sha256": "a" * 64,
        "configured_by": _ACTOR,
        "configured_at_us": 1_700_000_000_000_000,
    }
    binding = {
        "source": "ths",
        "quota_key": "csi500-fuyao-sample-204",
        "binding_reference": _EVIDENCE_REFERENCE,
        "binding_sha256": "b" * 64,
        "configured_by": _ACTOR,
        "configured_at_us": 1_700_000_000_000_000,
    }
    state = {
        "quota_key": "csi500-fuyao-sample-204",
        "tokens_micro": 1_000_000,
        "remainder": 0,
        "last_refill_us": 1_700_000_000_000_000,
        "policy_revision": 1,
    }

    def verify(connection: object) -> None:
        quota_provisioning._verify_quota_rows(
            connection,
            actor=_ACTOR,
            configured_at_us=1_700_000_000_000_000,
            policy_reference=_EVIDENCE_REFERENCE,
            policy_sha256="a" * 64,
            binding_reference=_EVIDENCE_REFERENCE,
            binding_sha256="b" * 64,
        )

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="read-back failed"):
        verify(_ReadBackConnection([[], [binding], [state]]))

    altered_policy = {**expected, "rate_microtokens_per_second": 2}
    with pytest.raises(Csi500SampleQuotaUnavailableError, match="read-back failed"):
        verify(_ReadBackConnection([[altered_policy], [binding], [state]]))


def test_engine_creation_failure_is_sanitized(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to open the explicit SQLite URL becomes a safe unavailable error."""

    def fail_create_engine(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(quota_provisioning, "create_engine", fail_create_engine)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="engine is unavailable"):
        _provision(prepared_gate)


def test_writer_acquisition_failure_is_sanitized(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate unable to acquire its SQLite writer cannot reach grant setup."""

    def fail_open_writer(_gate: SourceAttemptGate) -> None:
        raise RuntimeError("synthetic writer failure")

    monkeypatch.setattr(SourceAttemptGate, "_open_write_connection", fail_open_writer)

    with pytest.raises(Csi500SampleQuotaUnavailableError, match="transaction is unavailable"):
        _provision(prepared_gate)


def test_post_commit_identity_change_is_uncertain_and_never_retried(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swapped file after acknowledged commit cannot be reported as success."""
    original_assert = quota_provisioning.assert_database_file_identity
    checks = 0

    def fail_after_commit(path: Path, identity: DatabaseFileIdentity) -> None:
        nonlocal checks
        checks += 1
        if checks == 4:
            raise RuntimeError("synthetic post-commit identity change")
        original_assert(path, identity)

    monkeypatch.setattr(quota_provisioning, "assert_database_file_identity", fail_after_commit)

    with pytest.raises(Csi500SampleQuotaCommitUncertainError, match="identity changed"):
        _provision(prepared_gate)

    with pytest.raises(Csi500SampleQuotaConflictError):
        _provision(prepared_gate)


def test_precommit_rollback_failure_is_reported_as_cleanup_error(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rollback is not replaced by the original grant mismatch."""
    original_open = SourceAttemptGate._open_write_connection
    opened: list[object] = []

    def open_with_rollback_failure(gate: SourceAttemptGate) -> object:
        connection = original_open(gate)
        original_rollback = connection.rollback

        def fail_rollback() -> None:
            original_rollback()
            raise RuntimeError("synthetic rollback failure")

        monkeypatch.setattr(connection, "rollback", fail_rollback)
        opened.append(connection)
        return connection

    monkeypatch.setattr(SourceAttemptGate, "_open_write_connection", open_with_rollback_failure)

    with pytest.raises(Csi500SampleQuotaCleanupError, match="rollback could not be confirmed"):
        _provision(prepared_gate, principal_scope="wrong-principal")

    assert opened


@pytest.mark.parametrize(
    ("commit_state", "error"),
    [
        ("precommit", Csi500SampleQuotaCleanupError),
        ("committed", Csi500SampleQuotaCommittedCleanupError),
        ("uncertain", Csi500SampleQuotaCommitUncertainError),
    ],
    ids=("precommit-cleanup", "committed-cleanup", "uncertain-cleanup"),
)
def test_connection_close_failure_preserves_transaction_outcome(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
    commit_state: str,
    error: type[Exception],
) -> None:
    """Connection cleanup failure is classified by commit state and no retry occurs."""
    original_open = SourceAttemptGate._open_write_connection
    original_commit: Callable[[SourceAttemptGate, object], None] = SourceAttemptGate._commit

    def open_with_close_failure(gate: SourceAttemptGate) -> object:
        connection = original_open(gate)
        original_close = connection.close

        def close_then_fail() -> None:
            original_close()
            raise RuntimeError("synthetic connection cleanup failure")

        monkeypatch.setattr(connection, "close", close_then_fail)
        return connection

    def commit_then_drop_ack(gate: SourceAttemptGate, connection: object) -> None:
        original_commit(gate, connection)
        raise RuntimeError("synthetic lost acknowledgement")

    monkeypatch.setattr(SourceAttemptGate, "_open_write_connection", open_with_close_failure)
    if commit_state == "uncertain":
        monkeypatch.setattr(SourceAttemptGate, "_commit", commit_then_drop_ack)

    with pytest.raises(error, match="connection cleanup failed"):
        if commit_state == "precommit":
            _provision(prepared_gate, principal_scope="wrong-principal")
        else:
            _provision(prepared_gate)

    if commit_state != "precommit":
        monkeypatch.undo()
        with pytest.raises(Csi500SampleQuotaConflictError):
            _provision(prepared_gate)


def test_close_failure_does_not_skip_engine_disposal_or_mask_uncertain_commit(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both cleanup actions run while a lost commit acknowledgement stays uncertain."""
    original_open_connection = SourceAttemptGate._open_write_connection
    original_commit: Callable[[SourceAttemptGate, object], None] = SourceAttemptGate._commit
    original_open_engine = quota_provisioning._open_explicit_engine
    opened_connections: list[tuple[object, Callable[[], None]]] = []
    dispose_attempts: list[bool] = []

    def open_with_failing_close(gate: SourceAttemptGate) -> object:
        connection = original_open_connection(gate)
        original_close = connection.close

        def fail_before_close() -> None:
            raise RuntimeError("synthetic close failure before release")

        monkeypatch.setattr(connection, "close", fail_before_close)
        opened_connections.append((connection, original_close))
        return connection

    def commit_then_drop_ack(gate: SourceAttemptGate, connection: object) -> None:
        original_commit(gate, connection)
        raise RuntimeError("synthetic lost acknowledgement")

    def open_engine_with_dispose_probe(path: Path) -> Engine:
        engine = original_open_engine(path)
        original_dispose = engine.dispose

        def dispose_and_record() -> None:
            dispose_attempts.append(True)
            original_dispose()
            raise RuntimeError("synthetic dispose failure after release")

        monkeypatch.setattr(engine, "dispose", dispose_and_record)
        return engine

    monkeypatch.setattr(SourceAttemptGate, "_open_write_connection", open_with_failing_close)
    monkeypatch.setattr(SourceAttemptGate, "_commit", commit_then_drop_ack)
    monkeypatch.setattr(quota_provisioning, "_open_explicit_engine", open_engine_with_dispose_probe)

    try:
        with pytest.raises(
            Csi500SampleQuotaCommitUncertainError,
            match="connection and engine cleanup failed",
        ):
            _provision(prepared_gate)
    finally:
        monkeypatch.undo()
        for _connection, original_close in opened_connections:
            original_close()

    assert dispose_attempts == [True]


@pytest.mark.parametrize(
    ("commit_state", "error"),
    [
        ("precommit", Csi500SampleQuotaCleanupError),
        ("committed", Csi500SampleQuotaCommittedCleanupError),
    ],
    ids=("precommit-engine-cleanup", "committed-engine-cleanup"),
)
def test_engine_dispose_failure_is_reported_for_each_acknowledgement_state(
    prepared_gate: _PreparedGate,
    monkeypatch: pytest.MonkeyPatch,
    commit_state: str,
    error: type[Exception],
) -> None:
    """Engine disposal cannot erase whether provisioning committed."""
    original_open_engine = quota_provisioning._open_explicit_engine

    def open_engine_with_dispose_failure(path: Path) -> Engine:
        engine = original_open_engine(path)
        original_dispose = engine.dispose

        def dispose_then_fail() -> None:
            original_dispose()
            raise RuntimeError("synthetic engine cleanup failure")

        monkeypatch.setattr(engine, "dispose", dispose_then_fail)
        return engine

    monkeypatch.setattr(
        quota_provisioning,
        "_open_explicit_engine",
        open_engine_with_dispose_failure,
    )

    with pytest.raises(error, match="engine cleanup failed"):
        if commit_state == "precommit":
            _provision(prepared_gate, principal_scope="wrong-principal")
        else:
            _provision(prepared_gate)

    if commit_state == "committed":
        monkeypatch.undo()
        with pytest.raises(Csi500SampleQuotaConflictError):
            _provision(prepared_gate)
