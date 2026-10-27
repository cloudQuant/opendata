"""Behavioral tests for the durable Sina FZB fanout journal and revision 0013."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool

import opendata.pipeline.sina_fzb_fanout_journal as fanout_journal_module
from opendata.pipeline.sina_fzb_fanout_journal import (
    BLOCKED,
    ENQUEUED,
    MAX_FANOUT_CHUNK_SIZE,
    PENDING,
    SINA_FZB_FANOUT_GROUP_TABLE,
    SINA_FZB_JOB_ID,
    FanoutGroupRecord,
    FanoutJournalCorruptionError,
    FanoutManifestConflictError,
    SinaFzbFanoutJournal,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0013_sina_fzb_fanout_groups.py"
)
MIGRATION_NAME = "sina_fzb_fanout_migration_0013"
TABLE_NAME = "od_sina_fzb_fanout_groups"
OCCURRENCE = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 26, 8, 31, tzinfo=timezone.utc)
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")


class _ClockResult:
    """Minimal SQL result used to exercise dialect clock decoding without servers."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _ClockConnection:
    """Capture the selected SQL and return a controlled fake database value."""

    def __init__(self, dialect_name: str, value: object) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.value = value
        self.statement: str | None = None

    def exec_driver_sql(self, statement: str) -> _ClockResult:
        self.statement = statement
        return _ClockResult(self.value)


def _load_migration() -> ModuleType:
    """Load only revision 0013, without evaluating Alembic's application env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path, filename: str = "fanout.sqlite") -> Engine:
    """Create an owned SQLite database with a bounded inter-connection wait."""
    database = tmp_path / filename
    return sa.create_engine(
        f"sqlite:///{database}",
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )


def _apply_revision(engine: Engine, operation_name: str) -> None:
    """Run one migration operation against the explicitly owned connection."""
    migration = _load_migration()
    operation = getattr(migration, operation_name)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            operation()


def _prepare_journal(
    tmp_path: Path, filename: str = "fanout.sqlite"
) -> tuple[Engine, SinaFzbFanoutJournal]:
    """Create the migrated test database and explicitly bind its journal."""
    engine = _new_engine(tmp_path, filename)
    _apply_revision(engine, "upgrade")
    return engine, SinaFzbFanoutJournal(engine, clock_policy="caller")


def _valid_storage_row() -> dict[str, object]:
    """Build a row that satisfies the migration's persisted-state constraints."""
    return {
        "fanout_key": hashlib.sha256(b"slot").hexdigest(),
        "job_id": SINA_FZB_JOB_ID,
        "job_version": "v1",
        "occurrence_at_us": 1,
        "manifest_sha256": hashlib.sha256(b"manifest").hexdigest(),
        "manifest_json": "{}",
        "member_count": 1,
        "next_index": 0,
        "status": PENDING,
        "lease_owner": None,
        "fencing_token": 0,
        "lease_expires_at_us": None,
        "attempt_count": 0,
        "next_attempt_at_us": None,
        "last_error_code": None,
        "created_at_us": 1,
        "updated_at_us": 1,
        "enqueued_at_us": None,
    }


def _manifest(
    symbols: tuple[str, ...] = SYMBOLS,
    *,
    job_id: str = SINA_FZB_JOB_ID,
    job_version: str = "v1",
    occurrence: datetime = OCCURRENCE,
    manifest_id: str = "sha256:manifest-001",
    selection_snapshot_ref: str = "universe:2026-10-26",
) -> str:
    """Build a fixed contract-v1 manifest fixture with its required canonical form."""
    payload = {
        "contract_version": 1,
        "job_id": job_id,
        "job_version": job_version,
        "manifest_id": manifest_id,
        "occurrence": occurrence.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "selection_snapshot_ref": selection_snapshot_ref,
        "symbols": list(symbols),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _create_group(
    journal: SinaFzbFanoutJournal,
    *,
    symbols: tuple[str, ...] = SYMBOLS,
    occurrence: datetime = OCCURRENCE,
    job_version: str = "v1",
    manifest_id: str = "sha256:manifest-001",
) -> FanoutGroupRecord:
    """Create one representative group in the single supported job slot."""
    return journal.create_or_get_group(
        job_id=SINA_FZB_JOB_ID,
        job_version=job_version,
        occurrence=occurrence,
        manifest_json=_manifest(
            symbols,
            job_version=job_version,
            occurrence=occurrence,
            manifest_id=manifest_id,
        ),
    )


def test_create_is_immutable_idempotent_and_persists_across_engine_restart(
    tmp_path: Path,
) -> None:
    """Same slot and bytes replay; manifest or version drift conflicts."""
    first_engine, first_journal = _prepare_journal(tmp_path)
    first = _create_group(first_journal)
    replay = _create_group(first_journal)
    assert replay == first
    first_engine.dispose()

    reopened_engine = _new_engine(tmp_path)
    reopened = SinaFzbFanoutJournal(reopened_engine, clock_policy="caller")
    try:
        assert reopened.get(first.fanout_key) == first
        with pytest.raises(FanoutManifestConflictError):
            _create_group(reopened, manifest_id="sha256:changed-manifest")
        with pytest.raises(FanoutManifestConflictError):
            _create_group(reopened, job_version="v2")
        with pytest.raises(ValueError, match="job_id must be"):
            reopened.create_or_get_group(
                job_id="another-job",
                job_version="v1",
                occurrence=OCCURRENCE,
                manifest_json=_manifest(job_id="another-job"),
            )
    finally:
        reopened_engine.dispose()


def test_constructor_requires_an_explicit_supported_sync_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject invalid construction before the journal opens any connection."""
    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        SinaFzbFanoutJournal(None)  # type: ignore[arg-type]

    engine = sa.create_engine("sqlite://")
    try:
        with pytest.raises(ValueError, match="clock_policy"):
            SinaFzbFanoutJournal(engine, clock_policy="host")  # type: ignore[arg-type]
        monkeypatch.setattr(engine.dialect, "name", "oracle")
        with pytest.raises(ValueError, match="unsupported for 'oracle'"):
            SinaFzbFanoutJournal(engine, clock_policy="database")
    finally:
        engine.dispose()


def test_claim_selects_one_group_and_fences_expired_owner(tmp_path: Path) -> None:
    """Multiple eligible slots lease individually; stale operations lose their fence."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        first = _create_group(journal, occurrence=OCCURRENCE)
        second = _create_group(journal, occurrence=OCCURRENCE + timedelta(minutes=1))

        lease_one = journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(seconds=5))
        lease_two = journal.claim_next_group(worker="worker-b", now=T0, ttl=timedelta(seconds=5))
        assert lease_one is not None and lease_two is not None
        assert {lease_one.fanout_key, lease_two.fanout_key} == {
            first.fanout_key,
            second.fanout_key,
        }
        assert lease_one.fencing_token == lease_two.fencing_token == 1
        assert lease_one.attempt_count == lease_two.attempt_count == 0
        assert journal.claim_next_group(worker="worker-c", now=T0, ttl=timedelta(seconds=5)) is None

        expired_at = T0 + timedelta(seconds=5)
        reclaimed = journal.claim_next_group(
            worker="worker-c", now=expired_at, ttl=timedelta(seconds=5)
        )
        assert reclaimed is not None
        assert reclaimed.fanout_key == lease_one.fanout_key
        assert reclaimed.fencing_token == 2
        assert reclaimed.attempt_count == 0
        assert (
            journal.heartbeat(
                fanout_key=lease_one.fanout_key,
                worker="worker-a",
                fencing_token=lease_one.fencing_token,
                now=expired_at,
                ttl=timedelta(seconds=5),
            )
            is None
        )
        assert (
            journal.advance_cursor(
                fanout_key=lease_one.fanout_key,
                worker="worker-a",
                fencing_token=lease_one.fencing_token,
                expected_index=0,
                new_index=1,
                now=expired_at,
            )
            is None
        )
        assert (
            journal.block(
                fanout_key=lease_one.fanout_key,
                worker="worker-a",
                fencing_token=lease_one.fencing_token,
                now=expired_at,
                error_code="STALE_WORKER",
            )
            is None
        )
        assert (
            journal.release_for_retry(
                fanout_key=lease_one.fanout_key,
                worker="worker-a",
                fencing_token=lease_one.fencing_token,
                now=expired_at,
                retry_delay=timedelta(seconds=1),
                error_code="STALE_RETRY",
            )
            is None
        )
    finally:
        engine.dispose()


def test_separate_repository_instances_compete_for_one_group_with_one_winner(
    tmp_path: Path,
) -> None:
    """Two independent connection pools cannot acquire the same live slot."""
    setup_engine, setup_journal = _prepare_journal(tmp_path)
    group = _create_group(setup_journal)
    setup_engine.dispose()

    left_engine = _new_engine(tmp_path)
    right_engine = _new_engine(tmp_path)
    left = SinaFzbFanoutJournal(left_engine, clock_policy="caller")
    right = SinaFzbFanoutJournal(right_engine, clock_policy="caller")
    barrier = Barrier(2)

    def compete(journal: SinaFzbFanoutJournal, worker: str) -> FanoutGroupRecord | None:
        barrier.wait(timeout=5)
        return journal.claim_next_group(worker=worker, now=T0, ttl=timedelta(minutes=1))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            left_future = executor.submit(compete, left, "worker-left")
            right_future = executor.submit(compete, right, "worker-right")
            winners = [left_future.result(timeout=20), right_future.result(timeout=20)]
        assert sum(winner is not None for winner in winners) == 1
        winner = next(winner for winner in winners if winner is not None)
        assert winner.fanout_key == group.fanout_key
    finally:
        left_engine.dispose()
        right_engine.dispose()


def test_claim_compare_and_set_failure_returns_no_lease(tmp_path: Path) -> None:
    """A database trigger that suppresses the CAS update cannot yield a false claim."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER ignore_fanout_claim "
                f"BEFORE UPDATE OF lease_owner ON {TABLE_NAME} "
                "WHEN NEW.lease_owner IS NOT NULL "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        assert journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(seconds=5)) is None
        unchanged = journal.get(group.fanout_key)
        assert unchanged is not None
        assert unchanged.lease_owner is None
        assert unchanged.fencing_token == 0
    finally:
        engine.dispose()


def test_separate_instances_concurrently_create_one_immutable_slot(tmp_path: Path) -> None:
    """Concurrent identical creates converge on one persisted group and digest."""
    setup_engine, _ = _prepare_journal(tmp_path)
    setup_engine.dispose()
    left_engine = _new_engine(tmp_path)
    right_engine = _new_engine(tmp_path)
    left = SinaFzbFanoutJournal(left_engine, clock_policy="caller")
    right = SinaFzbFanoutJournal(right_engine, clock_policy="caller")
    barrier = Barrier(2)

    def create(journal: SinaFzbFanoutJournal) -> FanoutGroupRecord:
        barrier.wait(timeout=5)
        return _create_group(journal)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(create, left)
            second = executor.submit(create, right)
            created = [first.result(timeout=20), second.result(timeout=20)]
        assert created[0] == created[1]
        with left_engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(SINA_FZB_FANOUT_GROUP_TABLE)
                ).scalar_one()
                == 1
            )
    finally:
        left_engine.dispose()
        right_engine.dispose()


def test_primary_key_collision_outside_slot_is_reported_as_corruption(tmp_path: Path) -> None:
    """A conflicting digest with no matching natural slot is never accepted as a replay."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        other_occurrence = OCCURRENCE + timedelta(minutes=1)
        other = _create_group(journal, occurrence=other_occurrence)
        intended_key = fanout_journal_module._fanout_key(
            SINA_FZB_JOB_ID,
            OCCURRENCE.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )
        with engine.begin() as connection:
            connection.execute(
                sa.update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == other.fanout_key)
                .values(fanout_key=intended_key)
            )
        with pytest.raises(FanoutJournalCorruptionError, match="outside its natural slot"):
            _create_group(journal)
    finally:
        engine.dispose()


def test_heartbeat_extends_only_the_matching_live_lease_and_never_shortens_it(
    tmp_path: Path,
) -> None:
    """CAS heartbeat rejects stale owners and preserves a later current expiry."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        lease = journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(seconds=10))
        assert lease is not None

        assert (
            journal.heartbeat(
                fanout_key=group.fanout_key,
                worker="worker-b",
                fencing_token=lease.fencing_token,
                now=T0 + timedelta(seconds=1),
                ttl=timedelta(seconds=20),
            )
            is None
        )
        assert (
            journal.heartbeat(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token + 1,
                now=T0 + timedelta(seconds=1),
                ttl=timedelta(seconds=20),
            )
            is None
        )

        extended = journal.heartbeat(
            fanout_key=group.fanout_key,
            worker="worker-a",
            fencing_token=lease.fencing_token,
            now=T0 + timedelta(seconds=5),
            ttl=timedelta(seconds=20),
        )
        assert extended is not None
        expected_expiry = T0 + timedelta(seconds=25)
        assert extended.lease_expires_at == expected_expiry
        shortened = journal.heartbeat(
            fanout_key=group.fanout_key,
            worker="worker-a",
            fencing_token=lease.fencing_token,
            now=T0 + timedelta(seconds=6),
            ttl=timedelta(seconds=2),
        )
        assert shortened is not None
        assert shortened.lease_expires_at == expected_expiry
        assert (
            journal.heartbeat(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                now=expected_expiry,
                ttl=timedelta(seconds=10),
            )
            is None
        )
    finally:
        engine.dispose()


def test_cursor_cas_rejects_out_of_range_or_stale_expected_position(tmp_path: Path) -> None:
    """A live owner cannot advance past the manifest or from a stale cursor."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        lease = journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(minutes=1))
        assert lease is not None
        assert (
            journal.advance_cursor(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                expected_index=0,
                new_index=len(SYMBOLS) + 1,
                now=T0,
            )
            is None
        )
        assert (
            journal.advance_cursor(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                expected_index=1,
                new_index=2,
                now=T0,
            )
            is None
        )
        assert journal.get(group.fanout_key).next_index == 0  # type: ignore[union-attr]
    finally:
        engine.dispose()


def test_partial_dispatch_can_replay_chunk_then_finish_without_claiming_child_success(
    tmp_path: Path,
) -> None:
    """A released cursor resumes at the same symbols; ENQUEUED is only enqueue completion."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        lease = journal.claim_next_group(worker="coordinator-a", now=T0, ttl=timedelta(minutes=1))
        assert lease is not None
        released = journal.release_for_retry(
            fanout_key=group.fanout_key,
            worker="coordinator-a",
            fencing_token=lease.fencing_token,
            now=T0,
            retry_delay=timedelta(seconds=10),
            error_code="DISPATCH_TRANSIENT",
        )
        assert released is not None
        assert released.next_index == 0
        assert released.status == PENDING
        assert released.last_error_code == "DISPATCH_TRANSIENT"
        assert released.attempt_count == 1
        assert (
            journal.claim_next_group(
                worker="coordinator-b",
                now=T0 + timedelta(seconds=9, microseconds=999_999),
                ttl=timedelta(minutes=1),
            )
            is None
        )

        engine.dispose()
        resumed_engine = _new_engine(tmp_path)
        resumed = SinaFzbFanoutJournal(resumed_engine, clock_policy="caller")
        try:
            claimed = resumed.claim_next_group(
                worker="coordinator-b",
                now=T0 + timedelta(seconds=10),
                ttl=timedelta(minutes=1),
            )
            assert claimed is not None
            assert claimed.fencing_token == 2
            assert claimed.attempt_count == 1
            assert claimed.symbols[claimed.next_index : 2] == SYMBOLS[:2]
            halfway = resumed.advance_cursor(
                fanout_key=group.fanout_key,
                worker="coordinator-b",
                fencing_token=claimed.fencing_token,
                expected_index=0,
                new_index=2,
                now=T0 + timedelta(seconds=10),
            )
            assert halfway is not None
            assert halfway.next_index == 2
            assert halfway.status == PENDING
            assert halfway.lease_owner is None
            assert halfway.lease_expires_at is None
            assert halfway.last_error_code is None
            next_chunk_lease = resumed.claim_next_group(
                worker="coordinator-b",
                now=T0 + timedelta(seconds=11),
                ttl=timedelta(minutes=1),
            )
            assert next_chunk_lease is not None
            assert next_chunk_lease.fencing_token == 3
            assert next_chunk_lease.attempt_count == 1
            completed = resumed.advance_cursor(
                fanout_key=group.fanout_key,
                worker="coordinator-b",
                fencing_token=next_chunk_lease.fencing_token,
                expected_index=2,
                new_index=3,
                now=T0 + timedelta(seconds=12),
            )
            assert completed is not None
            assert completed.status == ENQUEUED
            assert completed.next_index == len(SYMBOLS)
            assert completed.enqueued_at is not None
            assert completed.lease_owner is None
            assert (
                resumed.claim_next_group(
                    worker="coordinator-c",
                    now=T0 + timedelta(seconds=13),
                    ttl=timedelta(minutes=1),
                )
                is None
            )
        finally:
            resumed_engine.dispose()
    finally:
        engine.dispose()


def test_invalid_manifest_cursor_retry_and_error_inputs_fail_closed(tmp_path: Path) -> None:
    """Manifest bounds, canonical encoding, lease bounds, and safe error codes are enforced."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        with pytest.raises(ValueError, match="sorted and unique"):
            _create_group(journal, symbols=("600000.SH", "000001.SZ"))
        duplicate_key_manifest = _manifest().replace(
            '"job_id":"sina-fzb-capture"',
            '"job_id":"sina-fzb-capture","job_id":"sina-fzb-capture"',
        )
        with pytest.raises(ValueError, match="valid JSON"):
            journal.create_or_get_group(
                job_id=SINA_FZB_JOB_ID,
                job_version="v1",
                occurrence=OCCURRENCE,
                manifest_json=duplicate_key_manifest,
            )
        with pytest.raises(ValueError, match="canonical JSON"):
            journal.create_or_get_group(
                job_id=SINA_FZB_JOB_ID,
                job_version="v1",
                occurrence=OCCURRENCE,
                manifest_json=json.dumps(json.loads(_manifest()), indent=2),
            )

        group = _create_group(journal)
        lease = journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(seconds=5))
        assert lease is not None
        with pytest.raises(ValueError, match="cursor advance"):
            journal.advance_cursor(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                expected_index=0,
                new_index=MAX_FANOUT_CHUNK_SIZE + 1,
                now=T0,
            )
        with pytest.raises(ValueError, match="uppercase machine code"):
            journal.release_for_retry(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                now=T0,
                retry_delay=timedelta(seconds=1),
                error_code="provider returned a raw exception",
            )
        blocked = journal.block(
            fanout_key=group.fanout_key,
            worker="worker-a",
            fencing_token=lease.fencing_token,
            now=T0,
            error_code="MANIFEST_CONFLICT",
        )
        assert blocked is not None
        assert blocked.status == BLOCKED
        assert blocked.lease_owner is None
        assert (
            journal.claim_next_group(
                worker="worker-b", now=T0 + timedelta(seconds=1), ttl=timedelta(seconds=5)
            )
            is None
        )
        with pytest.raises(ValueError, match="ttl must be"):
            journal.heartbeat(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                now=T0,
                ttl=timedelta(milliseconds=100),
            )
        with pytest.raises(ValueError, match="symbols must contain"):
            too_many_symbols = tuple(f"{symbol:06d}.SH" for symbol in range(1, 20_002))
            _create_group(
                journal,
                occurrence=OCCURRENCE + timedelta(minutes=1),
                symbols=too_many_symbols,
            )
    finally:
        engine.dispose()


def test_manifest_validation_rejects_ambiguous_malformed_and_oversized_inputs(
    tmp_path: Path,
) -> None:
    """Reject invalid encoding, JSON shape, identity, references, and symbols early."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        canonical_payload = json.loads(_manifest())
        cases: list[tuple[object, str]] = [
            (None, "JSON string"),
            (_manifest().replace("manifest-001", "manifest-\ud800"), "valid UTF-8"),
            ("x" * (fanout_journal_module.MAX_MANIFEST_BYTES + 1), "exceeds"),
            ("NaN", "valid JSON"),
            ("[]", "exact contract-v1 fields"),
        ]
        extra_field = {**canonical_payload, "unreviewed": True}
        wrong_version = {**canonical_payload, "contract_version": True}
        wrong_job = {**canonical_payload, "job_id": "different-job"}
        wrong_occurrence = {**canonical_payload, "occurrence": "2026-10-26T08:31:00.000000Z"}
        empty_reference = {**canonical_payload, "manifest_id": ""}
        invalid_reference = {**canonical_payload, "selection_snapshot_ref": " bad "}
        non_list_symbols = {**canonical_payload, "symbols": "000001.SZ"}
        empty_symbols = {**canonical_payload, "symbols": []}
        invalid_symbol = {**canonical_payload, "symbols": ["SH600000"]}
        cases.extend(
            (
                (
                    json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                    message,
                )
                for value, message in (
                    (extra_field, "exact contract-v1 fields"),
                    (wrong_version, "integer 1"),
                    (wrong_job, "job identity"),
                    (wrong_occurrence, "occurrence must match"),
                    (empty_reference, "manifest_id must be"),
                    (invalid_reference, "selection_snapshot_ref must be"),
                    (non_list_symbols, "symbols must contain"),
                    (empty_symbols, "symbols must contain"),
                    (invalid_symbol, "six-digit .SH/.SZ"),
                )
            )
        )
        for manifest_json, error_message in cases:
            with pytest.raises(ValueError, match=error_message):
                journal.create_or_get_group(
                    job_id=SINA_FZB_JOB_ID,
                    job_version="v1",
                    occurrence=OCCURRENCE,
                    manifest_json=manifest_json,  # type: ignore[arg-type]
                )
        with pytest.raises(ValueError):
            journal.create_or_get_group(
                job_id=SINA_FZB_JOB_ID,
                job_version="v1",
                occurrence=OCCURRENCE,
                manifest_json="[" * 2_000 + "0" + "]" * 2_000,
            )
    finally:
        engine.dispose()


def test_parser_recursion_errors_are_normalized_for_input_and_persisted_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate parser recursion to input ValueError and persisted corruption."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)

        def raise_recursion(*_args: object, **_kwargs: object) -> object:
            raise RecursionError("simulated parser depth")

        with monkeypatch.context() as patch:
            patch.setattr(fanout_journal_module.json, "loads", raise_recursion)
            with pytest.raises(ValueError, match="nesting exceeds the supported parser depth"):
                _create_group(journal, occurrence=OCCURRENCE + timedelta(minutes=1))

        with monkeypatch.context() as patch:
            patch.setattr(fanout_journal_module.json, "loads", raise_recursion)
            with pytest.raises(
                FanoutJournalCorruptionError,
                match="persisted fanout manifest is invalid",
            ):
                journal.get(group.fanout_key)
    finally:
        engine.dispose()


def test_public_journal_arguments_reject_invalid_times_workers_and_tokens(
    tmp_path: Path,
) -> None:
    """Public entry points reject invalid values before issuing state transitions."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        with pytest.raises(ValueError, match="bounded identifier"):
            journal.create_or_get_group(
                job_id=SINA_FZB_JOB_ID,
                job_version="bad version",
                occurrence=OCCURRENCE,
                manifest_json=_manifest(job_version="bad version"),
            )
        for invalid_occurrence in (
            datetime(2026, 10, 26, 8, 30),
            datetime(2026, 10, 26, 16, 30, tzinfo=timezone(timedelta(hours=8))),
            None,
        ):
            with pytest.raises(ValueError, match="timestamps must"):
                journal.create_or_get_group(
                    job_id=SINA_FZB_JOB_ID,
                    job_version="v1",
                    occurrence=invalid_occurrence,  # type: ignore[arg-type]
                    manifest_json=_manifest(),
                )

        with pytest.raises(ValueError, match="lowercase SHA-256"):
            journal.get("not-a-key")
        for invalid_worker in (" padded", "padded ", "x" * 256):
            with pytest.raises(ValueError, match="worker"):
                journal.claim_next_group(worker=invalid_worker, now=T0, ttl=timedelta(seconds=5))
        with pytest.raises(ValueError, match="between"):
            journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(days=2))
        with pytest.raises(ValueError, match="timestamps must"):
            journal.claim_next_group(
                worker="worker-a", now=datetime(2026, 10, 26, 8, 31), ttl=timedelta(seconds=5)
            )

        lease = journal.claim_next_group(worker="worker-a", now=T0, ttl=timedelta(minutes=1))
        assert lease is not None
        with pytest.raises(ValueError, match="fencing_token"):
            journal.heartbeat(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=True,  # type: ignore[arg-type]
                now=T0,
                ttl=timedelta(seconds=5),
            )
        with pytest.raises(ValueError, match="expected_index"):
            journal.advance_cursor(
                fanout_key=group.fanout_key,
                worker="worker-a",
                fencing_token=lease.fencing_token,
                expected_index=True,  # type: ignore[arg-type]
                new_index=1,
                now=T0,
            )
        for delay in (timedelta(seconds=-1), timedelta(days=31)):
            with pytest.raises(ValueError, match="retry_delay"):
                journal.release_for_retry(
                    fanout_key=group.fanout_key,
                    worker="worker-a",
                    fencing_token=lease.fencing_token,
                    now=T0,
                    retry_delay=delay,
                    error_code="RETRY_LATER",
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("corrupt_values", "error_message"),
    [
        ({"manifest_sha256": "0" * 64}, "identity or manifest hash"),
        ({"member_count": 2}, "member count does not match"),
        ({"next_index": 4}, "cursor is outside"),
        ({"lease_owner": "worker"}, "lease fields are inconsistent"),
        (
            {"status": ENQUEUED, "next_index": 0, "enqueued_at_us": 10},
            "enqueued group is incomplete",
        ),
        ({"status": PENDING, "enqueued_at_us": 10}, "active group has an enqueue timestamp"),
        ({"status": PENDING, "next_index": len(SYMBOLS)}, "pending group has a completed cursor"),
        (
            {
                "status": BLOCKED,
                "lease_owner": "worker",
                "lease_expires_at_us": 10,
                "last_error_code": "BROKEN",
            },
            "blocked group still holds a lease",
        ),
        ({"last_error_code": "contains spaces"}, "error code is invalid"),
        ({"status": BLOCKED}, "blocked group has no error code"),
        (
            {
                "status": ENQUEUED,
                "next_index": len(SYMBOLS),
                "enqueued_at_us": 10,
                "last_error_code": "STALE_ERROR",
            },
            "enqueued group has an error code",
        ),
        ({"fencing_token": -1}, "counters must be non-negative"),
        ({"status": "UNKNOWN"}, "status is invalid"),
        ({"member_count": "three"}, "member_count is invalid"),
        ({"job_id": 123}, "persisted fanout job identity is invalid"),
        ({"job_version": "bad version"}, "persisted fanout job version is invalid"),
        ({"manifest_json": "not-json"}, "persisted fanout manifest is invalid"),
        ({"created_at_us": "yesterday"}, "timestamp is invalid"),
        ({"occurrence_at_us": 2**63 - 1}, "occurrence_at_us is outside datetime range"),
        (
            {"lease_owner": "worker", "lease_expires_at_us": 2**63 - 1},
            "lease_expires_at_us is outside datetime range",
        ),
        ({"enqueued_at_us": 2**63 - 1}, "enqueued_at_us is outside datetime range"),
        ({"next_attempt_at_us": 2**63 - 1}, "next_attempt_at_us is outside datetime range"),
        ({"created_at_us": 2**63 - 1}, "created_at_us is outside datetime range"),
        ({"updated_at_us": 2**63 - 1}, "updated_at_us is outside datetime range"),
        (
            {"manifest_json": "[" * 2_000 + "0" + "]" * 2_000},
            "persisted fanout manifest is invalid",
        ),
    ],
)
def test_get_rejects_corrupted_persisted_group_state(
    tmp_path: Path,
    corrupt_values: dict[str, object],
    error_message: str,
) -> None:
    """Read-time validation catches corruption even when SQLite checks were bypassed."""
    engine, journal = _prepare_journal(tmp_path)
    try:
        group = _create_group(journal)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
            connection.commit()
            connection.execute(
                sa.update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key)
                .values(**corrupt_values)
            )
            connection.commit()
        with pytest.raises(FanoutJournalCorruptionError, match=error_message):
            journal.get(group.fanout_key)
    finally:
        engine.dispose()


def test_retry_delay_uses_database_time_when_caller_clock_is_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host clock years away from DB UTC cannot shift the durable retry deadline."""
    database_now = datetime(2031, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    database_now_us = fanout_journal_module._datetime_to_epoch_us(database_now)
    monkeypatch.setattr(
        fanout_journal_module,
        "_database_clock_us",
        lambda _connection: database_now_us,
    )
    engine = _new_engine(tmp_path, "database-clock.sqlite")
    _apply_revision(engine, "upgrade")
    journal = SinaFzbFanoutJournal(engine, clock_policy="database")
    try:
        group = _create_group(journal)
        claimed = journal.claim_next_group(
            worker="db-clock-worker", now=T0, ttl=timedelta(minutes=1)
        )
        assert claimed is not None
        released = journal.release_for_retry(
            fanout_key=group.fanout_key,
            worker="db-clock-worker",
            fencing_token=claimed.fencing_token,
            now=T0,
            retry_delay=timedelta(seconds=17),
            error_code="RETRY_LATER",
        )
        assert released is not None
        assert released.next_attempt_at == database_now + timedelta(seconds=17)
    finally:
        engine.dispose()


def test_database_clock_decodes_supported_dialect_results_without_live_servers() -> None:
    """Verify SQL selection, UTC normalization, and rejection of malformed results."""
    fixed_utc = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
    mysql_value = datetime(2030, 1, 2, 3, 4, 5, 123456)
    postgres_value = datetime(2030, 1, 2, 11, 4, 5, 123456, tzinfo=timezone(timedelta(hours=8)))
    sqlite_value = "2030-01-02 03:04:05.123"
    cases = (
        ("mysql", mysql_value, "SELECT UTC_TIMESTAMP(6)", fixed_utc),
        ("postgresql", postgres_value, "SELECT clock_timestamp()", fixed_utc),
        (
            "sqlite",
            sqlite_value,
            "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')",
            fixed_utc.replace(microsecond=123000),
        ),
    )
    for dialect_name, value, expected_sql, expected_time in cases:
        connection = _ClockConnection(dialect_name, value)
        observed = fanout_journal_module._database_clock_us(connection)  # type: ignore[arg-type]
        assert observed == fanout_journal_module._datetime_to_epoch_us(expected_time)
        assert connection.statement == expected_sql

    naive_postgres = _ClockConnection("postgresql", datetime(2030, 1, 2, 3, 4, 5, 123456))
    assert fanout_journal_module._database_clock_us(naive_postgres) == (
        fanout_journal_module._datetime_to_epoch_us(fixed_utc)
    )

    with pytest.raises(RuntimeError, match="unsupported for 'oracle'"):
        fanout_journal_module._database_clock_us(_ClockConnection("oracle", None))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="non-text timestamp"):
        fanout_journal_module._database_clock_us(_ClockConnection("sqlite", 123))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        fanout_journal_module._database_clock_us(_ClockConnection("sqlite", "not-a-time"))  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        fanout_journal_module._database_clock_us(_ClockConnection("mysql", 123))  # type: ignore[arg-type]


def test_database_clock_reads_an_owned_sqlite_connection() -> None:
    """Exercise the SQLite database-clock query against an owned in-memory DB."""
    engine = sa.create_engine("sqlite://")
    try:
        before_us = fanout_journal_module._datetime_to_epoch_us(datetime.now(timezone.utc))
        with engine.connect() as connection:
            observed_us = fanout_journal_module._database_clock_us(connection)
        after_us = fanout_journal_module._datetime_to_epoch_us(datetime.now(timezone.utc))
        assert before_us - 1_000_000 <= observed_us <= after_us + 1_000_000
    finally:
        engine.dispose()


def test_migration_upgrade_downgrade_upgrade_preserves_neighbor_and_enforces_checks(
    tmp_path: Path,
) -> None:
    """Revision 0013 changes only its table and its frozen constraints survive replay."""
    engine = _new_engine(tmp_path, "migration.sqlite")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE od_fanout_migration_neighbor "
                    "(id INTEGER PRIMARY KEY, marker VARCHAR(32) NOT NULL)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO od_fanout_migration_neighbor (id, marker) VALUES (1, 'keep-me')"
                )
            )
        _apply_revision(engine, "upgrade")
        inspector = sa.inspect(engine)
        assert TABLE_NAME in inspector.get_table_names()
        columns = inspector.get_columns(TABLE_NAME)
        assert [column["name"] for column in columns] == list(
            SINA_FZB_FANOUT_GROUP_TABLE.columns.keys()
        )
        indexes = inspector.get_indexes(TABLE_NAME)
        assert {index["name"] for index in indexes} == {
            "uq_od_sina_fzb_fanout_slot",
            "ix_od_sina_fzb_fanout_claim",
        }
        check_names = {
            constraint["name"] for constraint in inspector.get_check_constraints(TABLE_NAME)
        }
        assert check_names == {
            "ck_od_sina_fzb_fanout_job_id",
            "ck_od_sina_fzb_fanout_status",
            "ck_od_sina_fzb_fanout_member_count",
            "ck_od_sina_fzb_fanout_cursor",
            "ck_od_sina_fzb_fanout_fence",
            "ck_od_sina_fzb_fanout_attempts",
            "ck_od_sina_fzb_fanout_lease_pair",
            "ck_od_sina_fzb_fanout_enqueued_complete",
            "ck_od_sina_fzb_fanout_pending_incomplete",
            "ck_od_sina_fzb_fanout_blocked_unleased",
            "ck_od_sina_fzb_fanout_blocked_error",
            "ck_od_sina_fzb_fanout_enqueued_no_error",
            "ck_od_sina_fzb_fanout_key_sha_len",
            "ck_od_sina_fzb_manifest_sha_len",
        }
        migration = _load_migration()
        assert migration.revision == "0013_sina_fzb_fanout_groups"
        assert migration.down_revision == "0012_instrument_capture_scope"
        valid_row = _valid_storage_row()
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.insert(SINA_FZB_FANOUT_GROUP_TABLE).values(
                    **{**valid_row, "job_id": "not-sina-fzb"}
                )
            )

        with engine.begin() as connection:
            connection.execute(sa.insert(SINA_FZB_FANOUT_GROUP_TABLE).values(**valid_row))
        with pytest.raises(RuntimeError, match="archived before schema downgrade"):
            _apply_revision(engine, "downgrade")
        assert TABLE_NAME in sa.inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(
                sa.delete(SINA_FZB_FANOUT_GROUP_TABLE).where(
                    SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == valid_row["fanout_key"]
                )
            )
        _apply_revision(engine, "downgrade")
        assert TABLE_NAME not in sa.inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT marker FROM od_fanout_migration_neighbor WHERE id = 1")
                ).scalar_one()
                == "keep-me"
            )
        _apply_revision(engine, "upgrade")
        assert TABLE_NAME in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_migration_rejects_invalid_state_and_digest_boundaries(tmp_path: Path) -> None:
    """DDL checks prevent invalid status, cursor, lease, counter, and digest rows."""
    engine = _new_engine(tmp_path, "constraints.sqlite")
    try:
        _apply_revision(engine, "upgrade")
        invalid_rows = (
            {"job_id": "not-sina-fzb"},
            {"status": "UNKNOWN"},
            {"member_count": 0},
            {"next_index": 2},
            {"fencing_token": -1},
            {"attempt_count": -1},
            {"lease_owner": "worker"},
            {"status": ENQUEUED},
            {"next_index": 1},
            {"status": BLOCKED},
            {
                "status": BLOCKED,
                "lease_owner": "worker",
                "lease_expires_at_us": 3,
                "last_error_code": "BROKEN",
            },
            {"status": ENQUEUED, "next_index": 1, "enqueued_at_us": 2, "last_error_code": "BAD"},
            {"fanout_key": "short"},
            {"manifest_sha256": "short"},
        )
        for index, overrides in enumerate(invalid_rows):
            row = {
                **_valid_storage_row(),
                "fanout_key": hashlib.sha256(f"invalid-row-{index}".encode()).hexdigest(),
                **overrides,
            }
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(sa.insert(SINA_FZB_FANOUT_GROUP_TABLE).values(**row))
    finally:
        engine.dispose()
