"""Boundary behavior tests for the UTC Sina FZB slot reconciler and journal."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update
from sqlalchemy.pool import NullPool

from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
from opendata.pipeline.sina_fzb_fanout_journal import (
    SINA_FZB_FANOUT_GROUP_TABLE,
    SINA_FZB_JOB_ID,
    FanoutGroupRecord,
    SinaFzbFanoutJournal,
)
from opendata.pipeline.sina_fzb_slot_reconciliation import SinaFzbSlotReconciler
from opendata.pipeline.sina_fzb_slot_reconciliation_journal import (
    MAX_SLOTS_PER_BATCH,
    RECONCILIATION_GRACE_SECONDS,
    SINA_FZB_RECONCILED_SLOT_TABLE,
    SINA_FZB_RECONCILIATION_JOB_ID,
    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE,
    ReconciliationBatch,
    ReconciliationJournalCorruptionError,
    ReconciliationScheduleConflictError,
    SinaFzbSlotReconciliationJournal,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from sqlalchemy.engine import Engine

UTC = timezone.utc
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0013 = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0013_sina_fzb_fanout_groups.py"
)
MIGRATION_0014 = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0014_sina_fzb_slot_reconciliation.py"
)
JOB_VERSION = "capture-v1"
INITIAL_FROM = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
INITIAL_FROM_BASIS = "operator:boundary-review-001"
OCCURRENCE = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
CHECKED_AT = datetime(2026, 10, 26, 8, 32, tzinfo=UTC)
CRONTAB = "30 8 * * *"


def _new_engine(tmp_path: Path, name: str = "reconciliation.sqlite") -> Engine:
    """Create an isolated file-backed SQLite engine with a bounded lock wait."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )


def _load_migration(path: Path, module_name: str) -> ModuleType:
    """Load one revision directly without entering the application Alembic env."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_revision(engine: Engine, path: Path, module_name: str) -> None:
    """Apply one data revision to this test's explicitly owned SQLite file."""
    migration = _load_migration(path, module_name)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


def _apply_schema(engine: Engine) -> None:
    """Create only the fanout and reconciliation tables required by these tests."""
    _apply_revision(engine, MIGRATION_0013, "slot_boundary_migration_0013")
    _apply_revision(engine, MIGRATION_0014, "slot_boundary_migration_0014")


def _snapshot(crontab: str = CRONTAB, *, job_version: str = JOB_VERSION) -> tuple[str, str]:
    """Build a canonical v1 contract snapshot and its fixed digest."""
    payload = {
        "calendar_policy": "cron_only",
        "contract_version": 1,
        "crontab": crontab,
        "grace_seconds": RECONCILIATION_GRACE_SECONDS,
        "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
        "job_version": job_version,
        "max_recovery_slots_per_batch": 128,
        "max_slots_per_batch": MAX_SLOTS_PER_BATCH,
        "schedule_kind": "cron",
        "selection_policy": "unknown_if_no_slot_evidence",
        "timezone": "UTC",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _initialize(
    engine: Engine,
    *,
    crontab: str = CRONTAB,
    job_version: str = JOB_VERSION,
    initial_from: datetime = INITIAL_FROM,
    now: datetime = INITIAL_FROM,
) -> SinaFzbSlotReconciliationJournal:
    """Initialize the immutable cursor using only caller-supplied test time."""
    snapshot, fingerprint = _snapshot(crontab, job_version=job_version)
    journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
    journal.initialize(
        job_id=SINA_FZB_RECONCILIATION_JOB_ID,
        job_version=job_version,
        schedule_snapshot_json=snapshot,
        schedule_fingerprint=fingerprint,
        initial_from_basis=INITIAL_FROM_BASIS,
        initial_from=initial_from,
        now=now,
    )
    return journal


def _reconcile(
    journal: SinaFzbSlotReconciliationJournal,
    *,
    checked_at: datetime,
    expected_occurrences: tuple[datetime, ...],
    new_cursor: datetime,
) -> ReconciliationBatch:
    """Submit a batch bound to the cursor currently persisted in SQLite."""
    cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
    assert cursor is not None
    return journal.reconcile_batch(
        job_id=SINA_FZB_RECONCILIATION_JOB_ID,
        job_version=JOB_VERSION,
        expected_schedule_fingerprint=cursor.schedule_fingerprint,
        expected_cursor_revision=cursor.cursor_revision,
        expected_from=cursor.reconciled_through,
        checked_at=checked_at,
        due_through=checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
        new_cursor=new_cursor,
        expected_occurrences=expected_occurrences,
    )


def _manifest(occurrence: datetime, *, job_version: str = JOB_VERSION) -> str:
    """Build the canonical source-free manifest accepted by the real fanout journal."""
    payload = {
        "contract_version": 1,
        "job_id": SINA_FZB_JOB_ID,
        "job_version": job_version,
        "manifest_id": "manifest:slot-boundary-test",
        "occurrence": occurrence.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "selection_snapshot_ref": "selection:slot-boundary-test",
        "symbols": ["600519.SH"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _create_group(
    engine: Engine,
    occurrence: datetime,
    *,
    job_version: str = JOB_VERSION,
) -> FanoutGroupRecord:
    """Insert valid reference evidence through the production fanout journal."""
    fanout = SinaFzbFanoutJournal(engine, clock_policy="caller")
    return fanout.create_or_get_group(
        job_id=SINA_FZB_JOB_ID,
        job_version=job_version,
        occurrence=occurrence,
        manifest_json=_manifest(occurrence, job_version=job_version),
    )


def _rewrite_manifest(
    engine: Engine,
    group: FanoutGroupRecord,
    transform: Callable[[dict[str, object]], None],
) -> None:
    """Change one persisted manifest field and keep its digest internally consistent."""
    with engine.begin() as connection:
        row = connection.execute(
            select(SINA_FZB_FANOUT_GROUP_TABLE.c.manifest_json).where(
                SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key
            )
        ).one()
        payload = json.loads(row.manifest_json)
        transform(payload)
        manifest_json = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        connection.execute(
            update(SINA_FZB_FANOUT_GROUP_TABLE)
            .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key)
            .values(
                manifest_json=manifest_json,
                manifest_sha256=hashlib.sha256(manifest_json.encode("utf-8")).hexdigest(),
            )
        )


def _task(expression: str, *, version: str = JOB_VERSION) -> TaskDefinition:
    """Return an inert UTC cron task matching the reconciler's fixed job."""
    return TaskDefinition(
        job_id=SINA_FZB_RECONCILIATION_JOB_ID,
        version=version,
        dataset_id="sina-fzb-observations",
        provider="sina",
        model="financial-statement",
        timezone="UTC",
        calendar=None,
        schedule=ScheduleMetadata(kind="cron", expression=expression),
        enabled=False,
    )


def test_initialization_rejects_job_version_and_cron_drift_without_resetting_cursor(
    tmp_path: Path,
) -> None:
    """Neither a new version nor a changed cron expression silently resets state."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        journal = _initialize(engine)
        original = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert original is not None

        for version, crontab in (("capture-v2", CRONTAB), (JOB_VERSION, "0 8 * * *")):
            snapshot, fingerprint = _snapshot(crontab, job_version=version)
            with pytest.raises(ReconciliationScheduleConflictError):
                journal.initialize(
                    job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                    job_version=version,
                    schedule_snapshot_json=snapshot,
                    schedule_fingerprint=fingerprint,
                    initial_from_basis=INITIAL_FROM_BASIS,
                    initial_from=INITIAL_FROM,
                    now=INITIAL_FROM + timedelta(days=1),
                )
            assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == original
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schedule_snapshot_json", "{}"),
        ("created_at_us", 9_223_372_036_854_775_807),
    ],
)
def test_read_cursor_fails_closed_on_corrupted_persisted_contract_or_timestamp(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """A damaged durable contract cannot be exposed as a usable cursor."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        journal = _initialize(engine)
        column = SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c[field]
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE)
                .where(
                    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c.job_id
                    == SINA_FZB_RECONCILIATION_JOB_ID
                )
                .values({column: value})
            )

        with pytest.raises(ReconciliationJournalCorruptionError):
            journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "invalid_case",
    ["bool_revision", "list_occurrences", "too_many", "naive_clock", "wrong_grace"],
)
def test_invalid_batch_boundaries_are_rejected_without_durable_changes(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    """Malformed cursor, clock, grace, and batch-size inputs leave no partial rows."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        journal = _initialize(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        checked_at = CHECKED_AT + timedelta(days=1)
        due_through = checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        arguments: dict[str, object] = {
            "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
            "job_version": JOB_VERSION,
            "expected_schedule_fingerprint": cursor.schedule_fingerprint,
            "expected_cursor_revision": cursor.cursor_revision,
            "expected_from": cursor.reconciled_through,
            "checked_at": checked_at,
            "due_through": due_through,
            "new_cursor": cursor.reconciled_through,
            "expected_occurrences": (),
        }
        if invalid_case == "bool_revision":
            arguments["expected_cursor_revision"] = True
        elif invalid_case == "list_occurrences":
            arguments["expected_occurrences"] = []
        elif invalid_case == "too_many":
            arguments["expected_occurrences"] = tuple(
                INITIAL_FROM + timedelta(minutes=index)
                for index in range(1, MAX_SLOTS_PER_BATCH + 2)
            )
        elif invalid_case == "naive_clock":
            arguments["checked_at"] = datetime(2026, 10, 27, 8, 32)
        else:
            arguments["due_through"] = due_through - timedelta(microseconds=1)

        with pytest.raises(ValueError):
            journal.reconcile_batch(**arguments)  # type: ignore[arg-type]

        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_sqlite_initialization_race_returns_one_frozen_cursor(tmp_path: Path) -> None:
    """Two processes initializing one slot contract cannot create competing rows."""
    engine_a = _new_engine(tmp_path, "initialization-race.sqlite")
    engine_b = _new_engine(tmp_path, "initialization-race.sqlite")
    barrier = Barrier(2)
    try:
        _apply_schema(engine_a)
        snapshot, fingerprint = _snapshot()
        journals = (
            SinaFzbSlotReconciliationJournal(engine_a, clock_policy="caller"),
            SinaFzbSlotReconciliationJournal(engine_b, clock_policy="caller"),
        )

        def initialize(journal: SinaFzbSlotReconciliationJournal) -> object:
            barrier.wait(timeout=10)
            return journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            cursors = tuple(executor.map(initialize, journals))

        assert cursors[0] == cursors[1]
        with engine_a.connect() as connection:
            assert (
                connection.execute(
                    select(sa.func.count()).select_from(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE)
                ).scalar_one()
                == 1
            )
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_sqlite_cron_service_excludes_lower_edge_and_includes_upper_edge(
    tmp_path: Path,
) -> None:
    """The real service records exactly (initial_from, grace_cutoff] on SQLite."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        first = INITIAL_FROM + timedelta(minutes=1)
        upper = INITIAL_FROM + timedelta(minutes=2)
        _create_group(engine, upper)
        checked_at = upper + timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        reconciler = SinaFzbSlotReconciler(
            journal,
            _task("* * * * *"),
            initial_from=INITIAL_FROM,
            initial_from_basis=INITIAL_FROM_BASIS,
            clock=lambda: checked_at,
        )

        batch = reconciler.reconcile()

        assert [slot.occurrence for slot in batch.slots] == [first, upper]
        assert [slot.status for slot in batch.slots] == ["MISSING", "PRESENT"]
        assert batch.cursor.reconciled_through == upper
        assert batch.cursor.initial_from == INITIAL_FROM
    finally:
        engine.dispose()


def test_late_group_recovery_is_inclusive_at_first_missing_instant(tmp_path: Path) -> None:
    """Recovery waits when checked before the missing time and accepts equality."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        journal = _initialize(engine)
        first = _reconcile(
            journal,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        _create_group(engine, OCCURRENCE)

        before_boundary = _reconcile(
            journal,
            checked_at=CHECKED_AT - timedelta(microseconds=1),
            expected_occurrences=(),
            new_cursor=OCCURRENCE,
        )
        assert before_boundary.recovered_slots == ()

        at_boundary = _reconcile(
            journal,
            checked_at=CHECKED_AT,
            expected_occurrences=(),
            new_cursor=OCCURRENCE,
        )

        assert len(at_boundary.recovered_slots) == 1
        recovered = at_boundary.recovered_slots[0]
        assert recovered.status == "RECOVERED"
        assert recovered.first_missing_at == CHECKED_AT
        assert recovered.recovered_at == CHECKED_AT
        assert at_boundary.cursor.reconciled_through == first.cursor.reconciled_through
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "symbols",
    [[], ["invalid"], ["600519.SH", "600519.SH"], ["600519.SH", "000001.SZ"]],
)
def test_manifest_semantic_corruption_fails_even_when_digest_matches(
    tmp_path: Path,
    symbols: list[str],
) -> None:
    """A valid digest cannot make an invalid symbol set valid historical evidence."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        group = _create_group(engine, OCCURRENCE)
        _rewrite_manifest(engine, group, lambda payload: payload.__setitem__("symbols", symbols))
        journal = _initialize(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None

        with pytest.raises(ReconciliationJournalCorruptionError, match="symbol set"):
            _reconcile(
                journal,
                checked_at=CHECKED_AT,
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )

        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_recovery_update_rolls_back_when_a_later_slot_manifest_is_corrupt(
    tmp_path: Path,
) -> None:
    """A failure after recovery mutation rolls back both recovery and cursor writes."""
    engine = _new_engine(tmp_path)
    try:
        _apply_schema(engine)
        minute_snapshot, minute_fingerprint = _snapshot("* * * * *")
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        journal.initialize(
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            schedule_snapshot_json=minute_snapshot,
            schedule_fingerprint=minute_fingerprint,
            initial_from_basis=INITIAL_FROM_BASIS,
            initial_from=INITIAL_FROM,
            now=INITIAL_FROM,
        )
        missing = INITIAL_FROM + timedelta(minutes=1)
        next_slot = INITIAL_FROM + timedelta(minutes=2)
        first_check = INITIAL_FROM + timedelta(minutes=3)
        first_batch = _reconcile(
            journal,
            checked_at=first_check,
            expected_occurrences=(missing,),
            new_cursor=missing,
        )
        _create_group(engine, missing)
        broken_group = _create_group(engine, next_slot)
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == broken_group.fanout_key)
                .values(manifest_sha256="0" * 64)
            )

        checked_later = INITIAL_FROM + timedelta(minutes=4)
        before_failure = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert before_failure is not None
        with pytest.raises(ReconciliationJournalCorruptionError, match="digest"):
            journal.reconcile_batch(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                expected_schedule_fingerprint=minute_fingerprint,
                expected_cursor_revision=before_failure.cursor_revision,
                expected_from=before_failure.reconciled_through,
                checked_at=checked_later,
                due_through=checked_later - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
                new_cursor=next_slot,
                expected_occurrences=(next_slot,),
            )

        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == before_failure
        with engine.connect() as connection:
            rows = connection.execute(
                select(
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.occurrence_at_us,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.status,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.recovered_at_us,
                )
            ).all()
        assert len(rows) == 1
        assert rows[0].status == "MISSING"
        assert rows[0].recovered_at_us is None
        assert first_batch.cursor.cursor_revision == before_failure.cursor_revision
    finally:
        engine.dispose()
