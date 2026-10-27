"""Behavior tests for the UTC cron slot journal and revision 0014."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update
from sqlalchemy.pool import NullPool

import opendata.pipeline.sina_fzb_slot_reconciliation_journal as reconciliation_module
from opendata.pipeline.sina_fzb_fanout_journal import (
    SINA_FZB_FANOUT_GROUP_TABLE,
    SINA_FZB_JOB_ID,
    FanoutGroupRecord,
    SinaFzbFanoutJournal,
)
from opendata.pipeline.sina_fzb_slot_reconciliation_journal import (
    CRON_ONLY,
    GROUP_REFERENCE_ONLY,
    LATE_GROUP_ONLY,
    MAX_RECOVERY_SLOTS_PER_BATCH,
    MAX_SLOTS_PER_BATCH,
    NOT_ASSESSED,
    RECONCILIATION_GRACE_SECONDS,
    SINA_FZB_RECONCILED_SLOT_TABLE,
    SINA_FZB_RECONCILIATION_JOB_ID,
    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE,
    ReconciliationBatch,
    ReconciliationCASConflictError,
    ReconciliationCursor,
    ReconciliationJournalCorruptionError,
    ReconciliationScheduleConflictError,
    SinaFzbSlotReconciliationJournal,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0013 = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0013_sina_fzb_fanout_groups.py"
)
MIGRATION_0014 = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0014_sina_fzb_slot_reconciliation.py"
)
OCCURRENCE = datetime(2026, 10, 26, 8, 30, tzinfo=timezone.utc)
INITIAL_FROM = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
CHECKED_AT = datetime(2026, 10, 26, 8, 32, tzinfo=timezone.utc)
JOB_VERSION = "capture-v1"
CRONTAB = "30 8 * * *"
INITIAL_FROM_BASIS = "operator:bootstrap-reviewed-001"


def _new_engine(tmp_path: Path, filename: str = "reconcile.sqlite") -> Engine:
    """Create an owned file-backed SQLite engine with a bounded lock wait."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"timeout": 15},
        poolclass=NullPool,
    )


def _load_migration(path: Path, module_name: str) -> ModuleType:
    """Load a frozen revision module without entering Alembic's app env."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_migration(engine: Engine, path: Path, module_name: str, action: str) -> None:
    """Run exactly one revision against the caller-owned test connection."""
    migration = _load_migration(path, module_name)
    operation = getattr(migration, action)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            operation()


def _apply_chain(engine: Engine) -> None:
    """Create the 0013 fanout table before applying revision 0014."""
    _apply_migration(engine, MIGRATION_0013, "test_fanout_migration_0013", "upgrade")
    _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "upgrade")


def _schedule_snapshot(
    crontab: str = CRONTAB, *, job_version: str = JOB_VERSION
) -> tuple[str, str]:
    """Build the frozen exact cron-only snapshot and its canonical digest."""
    payload = {
        "calendar_policy": "cron_only",
        "contract_version": 1,
        "crontab": crontab,
        "grace_seconds": RECONCILIATION_GRACE_SECONDS,
        "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
        "job_version": job_version,
        "max_recovery_slots_per_batch": MAX_RECOVERY_SLOTS_PER_BATCH,
        "max_slots_per_batch": MAX_SLOTS_PER_BATCH,
        "schedule_kind": "cron",
        "selection_policy": "unknown_if_no_slot_evidence",
        "timezone": "UTC",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _new_journal(
    engine: Engine,
    *,
    crontab: str = CRONTAB,
    job_version: str = JOB_VERSION,
    initial_from: datetime = INITIAL_FROM,
    initial_from_basis: str = INITIAL_FROM_BASIS,
    now: datetime = INITIAL_FROM,
    clock_policy: Literal["caller", "database"] = "caller",
) -> SinaFzbSlotReconciliationJournal:
    """Initialize a single immutable generation with explicit lower boundary."""
    snapshot, fingerprint = _schedule_snapshot(crontab, job_version=job_version)
    journal = SinaFzbSlotReconciliationJournal(engine, clock_policy=clock_policy)
    journal.initialize(
        job_id=SINA_FZB_RECONCILIATION_JOB_ID,
        job_version=job_version,
        schedule_snapshot_json=snapshot,
        schedule_fingerprint=fingerprint,
        initial_from_basis=initial_from_basis,
        initial_from=initial_from,
        now=now,
    )
    return journal


def _manifest(occurrence: datetime, *, job_version: str = JOB_VERSION) -> str:
    """Build one exact fanout manifest referencing only the observed group."""
    payload = {
        "contract_version": 1,
        "job_id": SINA_FZB_JOB_ID,
        "job_version": job_version,
        "manifest_id": "manifest:observed-at-slot",
        "occurrence": occurrence.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "selection_snapshot_ref": "selection:snapshot-2026-10-26",
        "symbols": ["600519.SH"],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _expected_fanout_key(occurrence: datetime) -> str:
    """Return the natural-key digest from the fanout journal's frozen formula."""
    occurrence_text = occurrence.isoformat(timespec="microseconds").replace("+00:00", "Z")
    canonical_slot = json.dumps(
        {
            "contract_version": 1,
            "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
            "occurrence": occurrence_text,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_slot.encode("utf-8")).hexdigest()


def _create_group(
    engine: Engine,
    occurrence: datetime,
    *,
    job_version: str = JOB_VERSION,
) -> FanoutGroupRecord:
    """Create a test fanout row through its real journal contract."""
    fanout = SinaFzbFanoutJournal(engine, clock_policy="caller")
    return fanout.create_or_get_group(
        job_id=SINA_FZB_JOB_ID,
        job_version=job_version,
        occurrence=occurrence,
        manifest_json=_manifest(occurrence, job_version=job_version),
    )


def _reconcile(
    journal: SinaFzbSlotReconciliationJournal,
    *,
    cursor: ReconciliationCursor,
    checked_at: datetime,
    expected_occurrences: tuple[datetime, ...],
    new_cursor: datetime,
) -> ReconciliationBatch:
    """Call one batch against the exact revision and watermark read by caller."""
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


def test_migration_0014_upgrade_downgrade_upgrade_is_data_safe(tmp_path: Path) -> None:
    """The migration creates only its tables and can roll back while empty."""
    engine = _new_engine(tmp_path)
    try:
        _apply_migration(engine, MIGRATION_0013, "test_fanout_migration_0013", "upgrade")
        _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "upgrade")
        inspector = sa.inspect(engine)
        assert {
            "od_sina_fzb_slot_reconciliation_state",
            "od_sina_fzb_reconciled_slots",
            "od_sina_fzb_fanout_groups",
        } <= set(inspector.get_table_names())
        assert "ix_od_sina_fzb_reconciled_recovery" in {
            index["name"] for index in inspector.get_indexes("od_sina_fzb_reconciled_slots")
        }

        _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "downgrade")
        inspector = sa.inspect(engine)
        assert "od_sina_fzb_slot_reconciliation_state" not in inspector.get_table_names()
        assert "od_sina_fzb_reconciled_slots" not in inspector.get_table_names()
        assert "od_sina_fzb_fanout_groups" in inspector.get_table_names()

        _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "upgrade")
        assert "od_sina_fzb_reconciled_slots" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_migration_columns_constraints_and_indexes_match_journal_metadata(
    tmp_path: Path,
) -> None:
    """The frozen migration creates the same tables the journal declares."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        inspector = sa.inspect(engine)
        tables = (
            SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE,
            SINA_FZB_RECONCILED_SLOT_TABLE,
        )
        for expected in tables:
            actual_columns = {item["name"]: item for item in inspector.get_columns(expected.name)}
            assert set(actual_columns) == {column.name for column in expected.columns}
            for expected_column in expected.columns:
                actual = actual_columns[expected_column.name]
                assert actual["nullable"] is expected_column.nullable
                if isinstance(expected_column.type, sa.String):
                    assert actual["type"].length == expected_column.type.length
                else:
                    assert actual["type"]._type_affinity == expected_column.type._type_affinity
            primary_key = inspector.get_pk_constraint(expected.name)
            assert primary_key["constrained_columns"] == [
                column.name for column in expected.primary_key.columns
            ]
            actual_checks = {
                item["name"] for item in inspector.get_check_constraints(expected.name)
            }
            expected_checks = {
                constraint.name
                for constraint in expected.constraints
                if isinstance(constraint, sa.CheckConstraint)
            }
            assert actual_checks == expected_checks
            actual_indexes = {item["name"] for item in inspector.get_indexes(expected.name)}
            expected_indexes = {index.name for index in expected.indexes}
            assert actual_indexes == expected_indexes
    finally:
        engine.dispose()


def test_migration_downgrade_refuses_to_drop_cursor_or_slot_evidence(tmp_path: Path) -> None:
    """Downgrade fails before DDL if either reconciliation table has data."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        with pytest.raises(RuntimeError, match="must be archived"):
            _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "downgrade")
        names = set(sa.inspect(engine).get_table_names())
        assert "od_sina_fzb_slot_reconciliation_state" in names
        assert "od_sina_fzb_reconciled_slots" in names
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) is not None
    finally:
        engine.dispose()


def test_migration_downgrade_refuses_slot_rows_even_without_cursor(tmp_path: Path) -> None:
    """An orphaned slot observation is still evidence that must be archived."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        with engine.begin() as connection:
            connection.execute(
                SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.delete().where(
                    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c.job_id
                    == SINA_FZB_RECONCILIATION_JOB_ID
                )
            )
        with pytest.raises(RuntimeError, match="must be archived"):
            _apply_migration(engine, MIGRATION_0014, "test_slot_migration_0014", "downgrade")
        names = set(sa.inspect(engine).get_table_names())
        assert "od_sina_fzb_slot_reconciliation_state" in names
        assert "od_sina_fzb_reconciled_slots" in names
    finally:
        engine.dispose()


def test_migration_graph_has_one_0014_head() -> None:
    """The data migration chain remains linear through the new journal head."""
    revisions: dict[str, str | None] = {}
    for path in (REPOSITORY_ROOT / "alembic_data" / "versions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    values[target.id] = ast.literal_eval(value)
        revision = values.get("revision")
        down_revision = values.get("down_revision")
        if isinstance(revision, str):
            revisions[revision] = down_revision if isinstance(down_revision, str) else None
    parents = {parent for parent in revisions.values() if parent is not None}
    assert set(revisions) - parents == {"0014_sina_fzb_slot_reconciliation"}
    assert revisions["0014_sina_fzb_slot_reconciliation"] == "0013_sina_fzb_fanout_groups"


def test_constructor_rejects_unowned_engine_and_unsupported_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The journal requires a synchronous engine and a supported clock policy."""
    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        SinaFzbSlotReconciliationJournal(object())  # type: ignore[arg-type]

    engine = sa.create_engine("sqlite://")
    try:
        with pytest.raises(ValueError, match="clock_policy"):
            SinaFzbSlotReconciliationJournal(engine, clock_policy="host")  # type: ignore[arg-type]

        monkeypatch.setattr(engine.dialect, "name", "oracle")
        with pytest.raises(ValueError, match="unsupported"):
            SinaFzbSlotReconciliationJournal(engine, clock_policy="database")
        assert SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
    finally:
        engine.dispose()


def test_two_journals_racing_initialization_create_one_frozen_cursor(tmp_path: Path) -> None:
    """Concurrent bootstrap with identical inputs converges on one durable row."""
    engine_a = _new_engine(tmp_path)
    engine_b = _new_engine(tmp_path)
    barrier = Barrier(2)
    try:
        _apply_chain(engine_a)
        journal_a = SinaFzbSlotReconciliationJournal(engine_a, clock_policy="caller")
        journal_b = SinaFzbSlotReconciliationJournal(engine_b, clock_policy="caller")
        snapshot, fingerprint = _schedule_snapshot()

        def initialize(journal: SinaFzbSlotReconciliationJournal) -> ReconciliationCursor:
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
            first, second = tuple(executor.map(initialize, (journal_a, journal_b)))
        assert first == second
        assert first.cursor_revision == 0
        with engine_a.connect() as connection:
            count = connection.execute(
                select(sa.func.count()).select_from(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE)
            ).scalar_one()
        assert count == 1
    finally:
        engine_a.dispose()
        engine_b.dispose()


def test_initialize_requires_an_explicit_canonical_cron_only_contract(tmp_path: Path) -> None:
    """Initialization records an explicit boundary and refuses calendar fallback."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        snapshot, fingerprint = _schedule_snapshot()
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        cursor = journal.initialize(
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            schedule_snapshot_json=snapshot,
            schedule_fingerprint=fingerprint,
            initial_from_basis=INITIAL_FROM_BASIS,
            initial_from=INITIAL_FROM,
            now=INITIAL_FROM,
        )
        assert cursor.initial_from == INITIAL_FROM
        assert cursor.reconciled_through == INITIAL_FROM
        assert cursor.earlier_history_status == NOT_ASSESSED
        assert cursor.calendar_policy == CRON_ONLY
        assert cursor.cursor_revision == 0

        assert (
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM + timedelta(minutes=1),
            )
            == cursor
        )
        assert (
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM - timedelta(days=1),
            )
            == cursor
        )

        with pytest.raises(ReconciliationScheduleConflictError):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis="operator:changed-boundary",
                initial_from=INITIAL_FROM - timedelta(days=1),
                now=INITIAL_FROM,
            )

        payload = json.loads(snapshot)
        payload["calendar_policy"] = "weekday_fallback"
        invalid = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        invalid_hash = hashlib.sha256(invalid.encode("utf-8")).hexdigest()
        with pytest.raises(ValueError, match="fixed cron-only contract"):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=invalid,
                schedule_fingerprint=invalid_hash,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )

        numeric_weekday_snapshot, numeric_weekday_hash = _schedule_snapshot("30 8 * * 1")
        with pytest.raises(ValueError, match="weekday must use"):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=numeric_weekday_snapshot,
                schedule_fingerprint=numeric_weekday_hash,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )
    finally:
        engine.dispose()


def test_initialize_rejects_future_boundary_against_selected_clock(tmp_path: Path) -> None:
    """The explicit lower boundary cannot skip slots after the clock instant."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        snapshot, fingerprint = _schedule_snapshot()
        caller_journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        with pytest.raises(ValueError, match="initial_from must not be in the future"):
            caller_journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM + timedelta(minutes=1),
                now=INITIAL_FROM,
            )
        assert caller_journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) is None

        database_journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="database")
        database_now = database_journal.database_now()
        with pytest.raises(ValueError, match="initial_from must not be in the future"):
            database_journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=database_now + timedelta(days=1),
                now=database_now,
            )
        assert database_journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) is None
    finally:
        engine.dispose()


def test_snapshot_rejects_noncanonical_duplicate_and_wrong_digest(tmp_path: Path) -> None:
    """Snapshot bytes are unambiguous and the stored hash binds their encoding."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        snapshot, fingerprint = _schedule_snapshot()

        def initialize(candidate_json: str, candidate_fingerprint: str) -> ReconciliationCursor:
            return journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=candidate_json,
                schedule_fingerprint=candidate_fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )

        with pytest.raises(ValueError, match="canonical JSON"):
            initialize(snapshot + " ", fingerprint)
        with pytest.raises(ValueError, match="unambiguous JSON"):
            initialize(
                snapshot.replace(
                    '"contract_version":1', '"contract_version":1,"contract_version":1'
                ),
                fingerprint,
            )
        with pytest.raises(ValueError, match="does not match canonical snapshot"):
            initialize(snapshot, "0" * 64)
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) is None
    finally:
        engine.dispose()


def test_reconcile_records_present_and_missing_without_claiming_collection(
    tmp_path: Path,
) -> None:
    """A matching group yields reference-only evidence; absence stays UNKNOWN."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        _create_group(engine, OCCURRENCE)
        journal = _new_journal(engine)
        missing_occurrence = OCCURRENCE + timedelta(days=1)
        checked_at = CHECKED_AT + timedelta(days=1)
        due = checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        batch = _reconcile(
            journal,
            cursor=cursor,
            checked_at=checked_at,
            expected_occurrences=(OCCURRENCE, missing_occurrence),
            new_cursor=missing_occurrence,
        )
        assert [slot.status for slot in batch.slots] == ["PRESENT", "MISSING"]
        present, missing = batch.slots
        assert present.first_selection_evidence_status == GROUP_REFERENCE_ONLY
        assert present.first_selection_snapshot_ref == "selection:snapshot-2026-10-26"
        assert present.first_fanout_status == "PENDING"
        assert present.first_manifest_sha256 is not None
        assert missing.first_missing_at == checked_at
        assert missing.first_selection_evidence_status == "UNKNOWN"
        assert missing.first_selection_snapshot_ref is None
        assert missing.first_fanout_status is None
        assert batch.cursor.reconciled_through == missing_occurrence
        assert batch.cursor.reconciled_through <= due
    finally:
        engine.dispose()


def test_late_group_recovery_preserves_first_missing_and_unknown_selection(
    tmp_path: Path,
) -> None:
    """A late group becomes separate recovery evidence and never dispatches work."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        missing = first.slots[0]
        assert missing.status == "MISSING"

        _create_group(engine, OCCURRENCE)
        checked_later = CHECKED_AT + timedelta(hours=2)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        recovered = _reconcile(
            journal,
            cursor=cursor,
            checked_at=checked_later,
            expected_occurrences=(),
            new_cursor=checked_later - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
        )
        assert recovered.slots == ()
        assert len(recovered.recovered_slots) == 1
        record = recovered.recovered_slots[0]
        assert record.status == "RECOVERED"
        assert record.first_missing_at == CHECKED_AT
        assert record.recovered_at == checked_later
        assert record.first_selection_evidence_status == "UNKNOWN"
        assert record.first_selection_snapshot_ref is None
        assert record.recovery_fanout_status == "PENDING"
        assert record.recovery_manifest_sha256 is not None
        assert record.recovery_group_selection_snapshot_ref == "selection:snapshot-2026-10-26"
        assert record.recovery_evidence_status == LATE_GROUP_ONLY
        assert recovered.cursor.reconciled_through == (
            checked_later - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        )
    finally:
        engine.dispose()


def test_job_version_conflict_rolls_back_all_slot_writes(tmp_path: Path) -> None:
    """A group from another job version cannot create evidence or advance state."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        _create_group(engine, OCCURRENCE, job_version="other-v2")
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with pytest.raises(ReconciliationScheduleConflictError):
            _reconcile(
                journal,
                cursor=cursor,
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


def test_stale_cursor_is_rejected_after_a_successful_advance(tmp_path: Path) -> None:
    """An old revision cannot replay a slot or rewrite its first observation."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        assert first.cursor.cursor_revision == cursor.cursor_revision + 1
        with pytest.raises(ReconciliationCASConflictError, match="stale"):
            _reconcile(
                journal,
                cursor=cursor,
                checked_at=CHECKED_AT + timedelta(minutes=1),
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )
        with engine.connect() as connection:
            count = connection.execute(
                select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
            ).scalar_one()
        assert count == 1
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == first.cursor
    finally:
        engine.dispose()


def test_cron_batch_truncation_never_skips_due_slots(tmp_path: Path) -> None:
    """A batch capped at 128 advances only to its last included cron slot."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        minute_cron = "* * * * *"
        initial = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
        checked = datetime(2026, 10, 26, 10, 12, tzinfo=timezone.utc)
        due = checked - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        journal = _new_journal(engine, crontab=minute_cron, initial_from=initial, now=initial)
        expected = tuple(initial + timedelta(minutes=index) for index in range(1, 129))
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        batch = _reconcile(
            journal,
            cursor=cursor,
            checked_at=checked,
            expected_occurrences=expected,
            new_cursor=expected[-1],
        )
        assert len(batch.slots) == MAX_SLOTS_PER_BATCH
        assert batch.slots[0].occurrence == expected[0]
        assert batch.slots[-1].occurrence == expected[-1]
        assert batch.cursor.reconciled_through == expected[-1]
        assert batch.cursor.reconciled_through < due
    finally:
        engine.dispose()


def test_late_recovery_limit_filters_by_group_before_limit(tmp_path: Path) -> None:
    """Unmatched old gaps cannot consume the bounded recovery result window."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        initial = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
        journal = _new_journal(engine, crontab="* * * * *", initial_from=initial, now=initial)
        expected = tuple(initial + timedelta(minutes=index) for index in range(1, 129))
        first_cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert first_cursor is not None
        first = _reconcile(
            journal,
            cursor=first_cursor,
            checked_at=datetime(2026, 10, 26, 10, 11, tzinfo=timezone.utc),
            expected_occurrences=expected,
            new_cursor=expected[-1],
        )
        second_cursor = first.cursor
        second_occurrence = expected[-1] + timedelta(minutes=1)
        second_checked = datetime(2026, 10, 26, 10, 11, tzinfo=timezone.utc)
        second = _reconcile(
            journal,
            cursor=second_cursor,
            checked_at=second_checked,
            expected_occurrences=(second_occurrence,),
            new_cursor=second_occurrence,
        )
        assert second.slots[0].status == "MISSING"

        for occurrence in expected[-5:]:
            _create_group(engine, occurrence)
        recovery_checked = second_checked
        latest = _reconcile(
            journal,
            cursor=second.cursor,
            checked_at=recovery_checked,
            expected_occurrences=(),
            new_cursor=second.cursor.reconciled_through,
        )
        assert len(latest.recovered_slots) == 5
        assert [slot.occurrence for slot in latest.recovered_slots] == list(expected[-5:])
        assert len(latest.recovered_slots) <= MAX_RECOVERY_SLOTS_PER_BATCH
    finally:
        engine.dispose()


def test_clock_rollback_allows_recovery_without_watermark_regression(tmp_path: Path) -> None:
    """A stale clock cannot rewind the cursor or forge a recovery timestamp."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        before = first.cursor
        assert before is not None
        _create_group(engine, OCCURRENCE)
        earlier_check = CHECKED_AT - timedelta(minutes=10)
        replay = _reconcile(
            journal,
            cursor=before,
            checked_at=earlier_check,
            expected_occurrences=(),
            new_cursor=before.reconciled_through,
        )
        assert replay.cursor.reconciled_through == OCCURRENCE
        assert replay.cursor.cursor_revision == before.cursor_revision + 1
        assert replay.cursor.last_checked_at == before.last_checked_at
        assert replay.recovered_slots == ()

        later_check = CHECKED_AT + timedelta(minutes=1)
        settled_cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert settled_cursor is not None
        settled = _reconcile(
            journal,
            cursor=settled_cursor,
            checked_at=later_check,
            expected_occurrences=(),
            new_cursor=later_check - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
        )
        assert len(settled.recovered_slots) == 1
        assert settled.recovered_slots[0].recovered_at == later_check
    finally:
        engine.dispose()


def test_database_clock_rejects_a_future_host_cutoff(tmp_path: Path) -> None:
    """A multi-host grace cutoff cannot be ahead of the database clock."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine, clock_policy="database")
        db_now = journal.database_now()
        checked_at = db_now + timedelta(days=1)
        due = checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with pytest.raises(ValueError, match="ahead of the database clock"):
            journal.reconcile_batch(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                expected_schedule_fingerprint=cursor.schedule_fingerprint,
                expected_cursor_revision=cursor.cursor_revision,
                expected_from=cursor.reconciled_through,
                checked_at=checked_at,
                due_through=due,
                new_cursor=cursor.reconciled_through,
                expected_occurrences=(),
            )
    finally:
        engine.dispose()


def test_database_clock_is_not_available_in_caller_clock_mode(tmp_path: Path) -> None:
    """A single-host caller-clock journal cannot masquerade as DB time."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine, clock_policy="caller")
        with pytest.raises(ValueError, match="database_now requires"):
            journal.database_now()
    finally:
        engine.dispose()


def test_database_clock_does_not_recover_when_checked_at_predates_missing_time(
    tmp_path: Path,
) -> None:
    """A newer DB wall clock cannot turn a caller's stale check into recovery."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="database")
        database_now = journal.database_now()
        previous_hour = database_now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        initial_from = previous_hour - timedelta(minutes=1)
        checked_at = previous_hour + timedelta(seconds=121)
        schedule_snapshot, fingerprint = _schedule_snapshot("0 * * * *", job_version=JOB_VERSION)
        cursor = journal.initialize(
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            schedule_snapshot_json=schedule_snapshot,
            schedule_fingerprint=fingerprint,
            initial_from_basis=INITIAL_FROM_BASIS,
            initial_from=initial_from,
            now=database_now,
        )
        first_due = checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=checked_at,
            expected_occurrences=(previous_hour,),
            new_cursor=first_due,
        )
        assert first.slots[0].status == "MISSING"
        _create_group(engine, previous_hour)

        rolled_back_at = checked_at - timedelta(seconds=1)
        rollback = _reconcile(
            journal,
            cursor=first.cursor,
            checked_at=rolled_back_at,
            expected_occurrences=(),
            new_cursor=first.cursor.reconciled_through,
        )
        assert rollback.recovered_slots == ()
        assert rollback.cursor.reconciled_through == first.cursor.reconciled_through

        later_checked_at = checked_at + timedelta(seconds=1)
        latest_cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert latest_cursor is not None
        settled = _reconcile(
            journal,
            cursor=latest_cursor,
            checked_at=later_checked_at,
            expected_occurrences=(),
            new_cursor=later_checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
        )
        assert len(settled.recovered_slots) == 1
        recovered_slot = settled.recovered_slots[0]
        assert recovered_slot.first_missing_at == checked_at
        assert recovered_slot.recovered_at is not None
        assert recovered_slot.recovered_at >= checked_at
    finally:
        engine.dispose()


def test_blocked_cursor_fails_closed_before_reconciliation(tmp_path: Path) -> None:
    """An operator-blocked singleton cannot continue advancing its cursor."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE)
                .where(
                    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c.job_id
                    == SINA_FZB_RECONCILIATION_JOB_ID
                )
                .values(status="BLOCKED")
            )
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None and cursor.status == "BLOCKED"
        with pytest.raises(ReconciliationCASConflictError, match="blocked"):
            _reconcile(
                journal,
                cursor=cursor,
                checked_at=CHECKED_AT,
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
    finally:
        engine.dispose()


def test_two_journals_racing_the_same_cursor_have_one_winner(tmp_path: Path) -> None:
    """SQLite writer reservation and revision CAS serialize concurrent scans."""
    engine = _new_engine(tmp_path)
    second_engine = _new_engine(tmp_path)
    barrier = Barrier(2)
    try:
        _apply_chain(engine)
        journal_a = _new_journal(engine)
        journal_b = SinaFzbSlotReconciliationJournal(second_engine, clock_policy="caller")
        cursor = journal_a.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None

        def attempt(journal: SinaFzbSlotReconciliationJournal) -> str:
            barrier.wait(timeout=10)
            try:
                _reconcile(
                    journal,
                    cursor=cursor,
                    checked_at=CHECKED_AT,
                    expected_occurrences=(OCCURRENCE,),
                    new_cursor=OCCURRENCE,
                )
            except ReconciliationCASConflictError:
                return "stale"
            return "advanced"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(attempt, (journal_a, journal_b)))
        assert sorted(outcomes) == ["advanced", "stale"]
        final = journal_a.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert final is not None and final.cursor_revision == 1
        with engine.connect() as connection:
            count = connection.execute(
                select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
            ).scalar_one()
        assert count == 1
    finally:
        engine.dispose()
        second_engine.dispose()


def test_invalid_utc_order_cursor_and_basis_inputs_fail_before_writes(tmp_path: Path) -> None:
    """Malformed timestamps, order, cursor, and boundary declarations are rejected."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        snapshot, fingerprint = _schedule_snapshot()
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        with pytest.raises(ValueError, match="initial_from_basis"):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis="contains space",
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=datetime(2026, 10, 26, 8, 0),
                now=INITIAL_FROM,
            )
        cursor = _new_journal(engine).read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with pytest.raises(ValueError, match="sorted and unique"):
            journal.reconcile_batch(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                expected_schedule_fingerprint=cursor.schedule_fingerprint,
                expected_cursor_revision=cursor.cursor_revision,
                expected_from=cursor.reconciled_through,
                checked_at=CHECKED_AT + timedelta(days=1),
                due_through=CHECKED_AT + timedelta(days=1) - timedelta(seconds=120),
                new_cursor=OCCURRENCE + timedelta(days=1),
                expected_occurrences=(OCCURRENCE + timedelta(days=1), OCCURRENCE),
            )
    finally:
        engine.dispose()


def test_empty_due_window_advances_only_to_safe_cutoff(tmp_path: Path) -> None:
    """A valid window with no scheduled fire still records its checked boundary."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        checked_at = datetime(2026, 10, 26, 8, 20, tzinfo=timezone.utc)
        safe_cutoff = checked_at - timedelta(seconds=RECONCILIATION_GRACE_SECONDS)
        result = _reconcile(
            journal,
            cursor=cursor,
            checked_at=checked_at,
            expected_occurrences=(),
            new_cursor=safe_cutoff,
        )
        assert result.slots == ()
        assert result.cursor.reconciled_through == safe_cutoff
        assert result.cursor.cursor_revision == cursor.cursor_revision + 1
    finally:
        engine.dispose()


def test_corrupt_fanout_reference_fails_closed(tmp_path: Path) -> None:
    """A group with a mismatched digest cannot be used as PRESENT evidence."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        group = _create_group(engine, OCCURRENCE)
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key)
                .values(manifest_sha256="0" * 64)
            )
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with pytest.raises(ReconciliationJournalCorruptionError, match="digest"):
            _reconcile(
                journal,
                cursor=cursor,
                checked_at=CHECKED_AT,
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
        with engine.connect() as connection:
            count = connection.execute(
                select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
            ).scalar_one()
        assert count == 0
    finally:
        engine.dispose()


def test_corrupt_fanout_natural_key_rejects_new_present_slot_atomically(
    tmp_path: Path,
) -> None:
    """A length-valid but noncanonical group key is not PRESENT evidence."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        group = _create_group(engine, OCCURRENCE)
        bad_key = ("1" if group.fanout_key[0] != "1" else "2") + group.fanout_key[1:]
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key)
                .values(fanout_key=bad_key)
            )
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with pytest.raises(ReconciliationJournalCorruptionError, match="natural key"):
            _reconcile(
                journal,
                cursor=cursor,
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


def test_corrupt_fanout_natural_key_rolls_back_late_recovery(tmp_path: Path) -> None:
    """A corrupted late group key cannot rewrite a MISSING observation."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        group = _create_group(engine, OCCURRENCE)
        bad_key = ("1" if group.fanout_key[0] != "1" else "2") + group.fanout_key[1:]
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_FANOUT_GROUP_TABLE)
                .where(SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == group.fanout_key)
                .values(fanout_key=bad_key)
            )
        checked_later = CHECKED_AT + timedelta(minutes=2)
        with pytest.raises(ReconciliationJournalCorruptionError, match="natural key"):
            _reconcile(
                journal,
                cursor=first.cursor,
                checked_at=checked_later,
                expected_occurrences=(),
                new_cursor=CHECKED_AT,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == first.cursor
        with engine.connect() as connection:
            row = connection.execute(
                select(
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.status,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.recovered_at_us,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.recovery_manifest_sha256,
                ).where(
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.job_id == SINA_FZB_RECONCILIATION_JOB_ID,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.occurrence_at_us
                    == int(OCCURRENCE.timestamp() * 1_000_000),
                )
            ).one()
        assert row.status == "MISSING"
        assert row.recovered_at_us is None
        assert row.recovery_manifest_sha256 is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("job_version", "bad version"),
        ("schedule_snapshot_json", "{}"),
        ("schedule_fingerprint", "0" * 64),
        ("calendar_policy", "weekday_fallback"),
        ("calendar_id", "calendar:unexpected"),
        ("calendar_sha256", "a" * 64),
        ("grace_seconds", RECONCILIATION_GRACE_SECONDS + 1),
        ("earlier_history_status", "ASSESSED"),
        ("initial_from_basis", "not a safe basis"),
        ("initial_from_us", int(INITIAL_FROM.timestamp() * 1_000_000) + 1),
        ("reconciled_through_us", int(INITIAL_FROM.timestamp() * 1_000_000) - 1),
        ("cursor_revision", -1),
        ("status", "RUNNING"),
        ("created_at_us", 9_223_372_036_854_775_807),
        ("updated_at_us", 9_223_372_036_854_775_807),
        ("last_checked_at_us", 9_223_372_036_854_775_807),
    ],
)
def test_read_cursor_translates_corrupt_persisted_contract(
    tmp_path: Path, column: str, value: object
) -> None:
    """Every frozen cursor field is revalidated when loaded from storage."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
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
    ("column", "value"),
    [
        ("job_version", "other-version"),
        ("schedule_fingerprint", "f" * 64),
        ("first_missing_at_us", int(CHECKED_AT.timestamp() * 1_000_000) + 1),
        ("first_selection_snapshot_ref", "unexpected-selection"),
        ("first_fanout_status", "PENDING"),
    ],
)
def test_recovery_rejects_corrupt_missing_evidence_atomically(
    tmp_path: Path, column: str, value: object
) -> None:
    """A matched late group cannot hide inconsistencies in first-missing facts."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        _create_group(engine, OCCURRENCE)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                update(SINA_FZB_RECONCILED_SLOT_TABLE)
                .where(
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.job_id == SINA_FZB_RECONCILIATION_JOB_ID,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.occurrence_at_us
                    == int(OCCURRENCE.timestamp() * 1_000_000),
                )
                .values({column: value})
            )
        with pytest.raises(ReconciliationJournalCorruptionError):
            _reconcile(
                journal,
                cursor=first.cursor,
                checked_at=CHECKED_AT + timedelta(minutes=2),
                expected_occurrences=(),
                new_cursor=CHECKED_AT,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == first.cursor
        with engine.connect() as connection:
            row = connection.execute(
                select(SINA_FZB_RECONCILED_SLOT_TABLE.c.status).where(
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.job_id == SINA_FZB_RECONCILIATION_JOB_ID,
                    SINA_FZB_RECONCILED_SLOT_TABLE.c.occurrence_at_us
                    == int(OCCURRENCE.timestamp() * 1_000_000),
                )
            ).scalar_one()
        assert row == "MISSING"
    finally:
        engine.dispose()


def test_corrupt_cursor_never_allows_duplicate_slot_insert(tmp_path: Path) -> None:
    """A cursor moved behind existing evidence fails closed and rolls back."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        first = _reconcile(
            journal,
            cursor=cursor,
            checked_at=CHECKED_AT,
            expected_occurrences=(OCCURRENCE,),
            new_cursor=OCCURRENCE,
        )
        with engine.begin() as connection:
            connection.execute(
                update(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE)
                .where(
                    SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c.job_id
                    == SINA_FZB_RECONCILIATION_JOB_ID
                )
                .values(
                    reconciled_through_us=int(INITIAL_FROM.timestamp() * 1_000_000),
                    cursor_revision=first.cursor.cursor_revision + 1,
                )
            )
        corrupted_cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert corrupted_cursor is not None
        with pytest.raises(ReconciliationJournalCorruptionError, match="already exists"):
            _reconcile(
                journal,
                cursor=corrupted_cursor,
                checked_at=OCCURRENCE + timedelta(minutes=2),
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == corrupted_cursor
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_cursor_compare_and_set_noop_rolls_back_new_slot(tmp_path: Path) -> None:
    """A zero-row CAS is treated as a conflict and its preceding writes roll back."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER ignore_reconcile_cursor_update "
                "BEFORE UPDATE ON od_sina_fzb_slot_reconciliation_state "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        with pytest.raises(ReconciliationCASConflictError, match="compare-and-set"):
            _reconcile(
                journal,
                cursor=cursor,
                checked_at=CHECKED_AT,
                expected_occurrences=(OCCURRENCE,),
                new_cursor=OCCURRENCE,
            )
        with engine.connect() as connection:
            assert (
                connection.execute(
                    select(sa.func.count()).select_from(SINA_FZB_RECONCILED_SLOT_TABLE)
                ).scalar_one()
                == 0
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
    finally:
        engine.dispose()


def test_reconcile_requires_initialized_state_and_frozen_contract(tmp_path: Path) -> None:
    """An absent state or a changed generation cannot create slot evidence."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        snapshot, fingerprint = _schedule_snapshot()
        with pytest.raises(ReconciliationCASConflictError, match="not initialized"):
            journal.reconcile_batch(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                expected_schedule_fingerprint=fingerprint,
                expected_cursor_revision=0,
                expected_from=INITIAL_FROM,
                checked_at=CHECKED_AT,
                due_through=CHECKED_AT - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
                new_cursor=INITIAL_FROM,
                expected_occurrences=(),
            )
        journal = _new_journal(engine)
        cursor = journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID)
        assert cursor is not None and cursor.schedule_snapshot_json == snapshot
        with pytest.raises(ReconciliationScheduleConflictError, match="generation"):
            journal.reconcile_batch(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version="capture-v2",
                expected_schedule_fingerprint=fingerprint,
                expected_cursor_revision=cursor.cursor_revision,
                expected_from=cursor.reconciled_through,
                checked_at=CHECKED_AT,
                due_through=CHECKED_AT - timedelta(seconds=RECONCILIATION_GRACE_SECONDS),
                new_cursor=INITIAL_FROM,
                expected_occurrences=(),
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) == cursor
    finally:
        engine.dispose()


@pytest.mark.parametrize("status", ["PENDING", "ENQUEUED", "BLOCKED"])
def test_fanout_observation_accepts_all_durable_group_states(status: str) -> None:
    """A group state proves registration only; all three persisted states parse."""
    manifest = _manifest(OCCURRENCE)
    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    observed = reconciliation_module._fanout_observation_from_row(
        {
            "fanout_key": _expected_fanout_key(OCCURRENCE),
            "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
            "job_version": JOB_VERSION,
            "occurrence_at_us": int(OCCURRENCE.timestamp() * 1_000_000),
            "status": status,
            "manifest_json": manifest,
            "manifest_sha256": digest,
        },
        expected_job_version=JOB_VERSION,
    )
    assert observed.status == status
    assert observed.occurrence_at_us == int(OCCURRENCE.timestamp() * 1_000_000)
    assert observed.manifest_sha256 == digest
    assert observed.selection_snapshot_ref == "selection:snapshot-2026-10-26"


@pytest.mark.parametrize(
    ("change", "error", "match"),
    [
        ("wrong-job", ReconciliationJournalCorruptionError, "job identity"),
        ("wrong-version", ReconciliationScheduleConflictError, "job version"),
        ("invalid-status", ReconciliationJournalCorruptionError, "group status"),
        ("invalid-fanout-key", ReconciliationJournalCorruptionError, "natural key"),
        ("wrong-fanout-key", ReconciliationJournalCorruptionError, "natural key"),
        ("missing-manifest", ReconciliationJournalCorruptionError, "manifest evidence"),
        ("missing-hash", ReconciliationJournalCorruptionError, "manifest evidence"),
        ("bad-hash-format", ReconciliationJournalCorruptionError, "digest is invalid"),
        ("bad-digest", ReconciliationJournalCorruptionError, "digest does not match"),
        ("wrong-occurrence", ReconciliationJournalCorruptionError, "identity is inconsistent"),
        ("wrong-job-in-snapshot", ReconciliationJournalCorruptionError, "identity is inconsistent"),
        (
            "wrong-contract-version",
            ReconciliationJournalCorruptionError,
            "identity is inconsistent",
        ),
        ("invalid-reference", ReconciliationJournalCorruptionError, "reference is invalid"),
        ("invalid-manifest-id", ReconciliationJournalCorruptionError, "reference is invalid"),
        ("invalid-symbol", ReconciliationJournalCorruptionError, "symbol set is invalid"),
        ("unsorted-symbols", ReconciliationJournalCorruptionError, "symbol set is invalid"),
        ("duplicate-symbols", ReconciliationJournalCorruptionError, "symbol set is invalid"),
        ("empty-symbols", ReconciliationJournalCorruptionError, "symbol set is invalid"),
        ("noncanonical", ReconciliationJournalCorruptionError, "canonical JSON"),
    ],
)
def test_fanout_observation_fails_closed_on_manifest_corruption(
    change: str,
    error: type[Exception],
    match: str,
) -> None:
    """A matching group key cannot mask malformed or altered manifest evidence."""
    payload = json.loads(_manifest(OCCURRENCE))
    row: dict[str, object] = {
        "fanout_key": _expected_fanout_key(OCCURRENCE),
        "job_id": SINA_FZB_RECONCILIATION_JOB_ID,
        "job_version": JOB_VERSION,
        "occurrence_at_us": int(OCCURRENCE.timestamp() * 1_000_000),
        "status": "PENDING",
    }
    if change == "wrong-job":
        row["job_id"] = "other-job"
    if change == "wrong-version":
        row["job_version"] = "other-version"
    if change == "invalid-status":
        row["status"] = "RUNNING"
    if change == "invalid-fanout-key":
        row["fanout_key"] = "x" * 64
    if change == "wrong-fanout-key":
        expected_key = _expected_fanout_key(OCCURRENCE)
        replacement_first_char = "1" if expected_key[0] != "1" else "2"
        row["fanout_key"] = replacement_first_char + expected_key[1:]
    if change == "wrong-occurrence":
        payload["occurrence"] = "2026-10-26T08:31:00.000000Z"
    if change == "wrong-job-in-snapshot":
        payload["job_id"] = "other-job"
    if change == "wrong-contract-version":
        payload["contract_version"] = True
    if change == "invalid-reference":
        payload["selection_snapshot_ref"] = ""
    if change == "invalid-manifest-id":
        payload["manifest_id"] = ""
    if change == "invalid-symbol":
        payload["symbols"] = ["600519"]
    if change == "unsorted-symbols":
        payload["symbols"] = ["600519.SH", "000001.SZ"]
    if change == "duplicate-symbols":
        payload["symbols"] = ["600519.SH", "600519.SH"]
    if change == "empty-symbols":
        payload["symbols"] = []

    manifest = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if change == "noncanonical":
        manifest = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    row["manifest_json"] = manifest
    row["manifest_sha256"] = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    if change == "missing-manifest":
        row["manifest_json"] = None
    if change == "missing-hash":
        row["manifest_sha256"] = None
    if change == "bad-hash-format":
        row["manifest_sha256"] = "not-a-digest"
    if change == "bad-digest":
        row["manifest_sha256"] = "0" * 64

    with pytest.raises(error, match=match):
        reconciliation_module._fanout_observation_from_row(
            row,
            expected_job_version=JOB_VERSION,
        )


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        ("{bad json", "JSON is invalid"),
        ('{"x":1,"x":2}', "JSON is invalid"),
        ('{"value":NaN}', "JSON is invalid"),
        ("[]", "JSON object"),
        ("\ud800", "valid UTF-8"),
    ],
)
def test_manifest_parser_rejects_ambiguous_oversized_and_invalid_utf8(
    manifest: str, match: str
) -> None:
    """Persisted manifests reject malformed encodings before contract checks."""
    with pytest.raises(ReconciliationJournalCorruptionError, match=match):
        reconciliation_module._parse_manifest(manifest)

    with pytest.raises(ReconciliationJournalCorruptionError, match="byte limit"):
        reconciliation_module._parse_manifest(" " * 2_097_153)


def test_manifest_parser_translates_recursion_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parser recursion failures become typed corruption rather than leaking internals."""

    def raise_recursion(*args: object, **kwargs: object) -> object:
        raise RecursionError("synthetic parser recursion")

    monkeypatch.setattr(reconciliation_module.json, "loads", raise_recursion)
    with pytest.raises(ReconciliationJournalCorruptionError, match="JSON is invalid"):
        reconciliation_module._parse_manifest("{}")


def test_schedule_parser_translates_recursion_errors_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser failure in caller input is a validation error and leaves no cursor."""
    engine = _new_engine(tmp_path)
    try:
        _apply_chain(engine)
        journal = SinaFzbSlotReconciliationJournal(engine, clock_policy="caller")
        snapshot, fingerprint = _schedule_snapshot()

        def raise_recursion(*args: object, **kwargs: object) -> object:
            raise RecursionError("synthetic parser recursion")

        monkeypatch.setattr(reconciliation_module.json, "loads", raise_recursion)
        with pytest.raises(ValueError, match="nesting is too deep"):
            journal.initialize(
                job_id=SINA_FZB_RECONCILIATION_JOB_ID,
                job_version=JOB_VERSION,
                schedule_snapshot_json=snapshot,
                schedule_fingerprint=fingerprint,
                initial_from_basis=INITIAL_FROM_BASIS,
                initial_from=INITIAL_FROM,
                now=INITIAL_FROM,
            )
        assert journal.read_cursor(SINA_FZB_RECONCILIATION_JOB_ID) is None
    finally:
        engine.dispose()


def test_sqlite_clock_parser_validates_driver_values_and_formats() -> None:
    """Database-clock adapters validate SQLite text and normalize UTC explicitly."""

    class FakeResult:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar_one(self) -> object:
            return self.value

    class FakeConnection:
        def __init__(self, dialect: str, value: object) -> None:
            self.dialect = SimpleNamespace(name=dialect)
            self.value = value
            self.statements: list[str] = []

        def exec_driver_sql(self, statement: str) -> FakeResult:
            self.statements.append(statement)
            return FakeResult(self.value)

    connection = FakeConnection("sqlite", "2026-10-27 10:30:15.123")
    assert reconciliation_module._database_clock_us(connection) == int(
        datetime(2026, 10, 27, 10, 30, 15, 123000, tzinfo=timezone.utc).timestamp() * 1_000_000
    )
    assert len(connection.statements) == 1
    with pytest.raises(RuntimeError, match="non-text"):
        reconciliation_module._database_clock_us(FakeConnection("sqlite", 123))
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        reconciliation_module._database_clock_us(FakeConnection("sqlite", "not-a-time"))
    with pytest.raises(RuntimeError, match="unsupported"):
        reconciliation_module._database_clock_us(FakeConnection("oracle", object()))
    with pytest.raises(RuntimeError, match="invalid timestamp"):
        reconciliation_module._database_clock_us(FakeConnection("mysql", "not-a-datetime"))
    assert reconciliation_module._database_clock_us(
        FakeConnection("postgresql", datetime(2026, 10, 27, 10, 30, tzinfo=timezone.utc))
    ) == int(datetime(2026, 10, 27, 10, 30, tzinfo=timezone.utc).timestamp() * 1_000_000)


def test_small_persisted_field_converters_fail_closed() -> None:
    """Nullable fields and finite status vocabularies keep records well typed."""
    assert reconciliation_module._optional_fanout_status(None) is None
    assert reconciliation_module._optional_string(None) is None
    assert reconciliation_module._optional_string("opaque") == "opaque"
    assert reconciliation_module._slot_status("PRESENT") == "PRESENT"
    assert reconciliation_module._slot_status("MISSING") == "MISSING"
    assert reconciliation_module._slot_status("RECOVERED") == "RECOVERED"
    assert reconciliation_module._cursor_status("ACTIVE") == "ACTIVE"
    assert reconciliation_module._cursor_status("BLOCKED") == "BLOCKED"
    assert reconciliation_module._selection_evidence_status("UNKNOWN") == "UNKNOWN"
    assert (
        reconciliation_module._selection_evidence_status("GROUP_REFERENCE_ONLY")
        == GROUP_REFERENCE_ONLY
    )
    with pytest.raises(ReconciliationJournalCorruptionError, match="slot status"):
        reconciliation_module._slot_status("QUEUED")
    with pytest.raises(ValueError, match="cursor status"):
        reconciliation_module._cursor_status("QUEUED")
    with pytest.raises(ReconciliationJournalCorruptionError, match="selection evidence"):
        reconciliation_module._selection_evidence_status("HISTORICAL")
    with pytest.raises(ReconciliationJournalCorruptionError, match="group status"):
        reconciliation_module._optional_fanout_status("RUNNING")
    with pytest.raises(ReconciliationJournalCorruptionError, match="text field"):
        reconciliation_module._optional_string(4)


def test_timestamp_json_and_numeric_contract_helpers_cover_boundary_errors() -> None:
    """Exact UTC and JSON encodings reject coercion, control values and overflow."""
    assert reconciliation_module._datetime_to_epoch_us(INITIAL_FROM, "initial") == int(
        INITIAL_FROM.timestamp() * 1_000_000
    )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        reconciliation_module._datetime_to_epoch_us(datetime(2026, 10, 26, 8), "initial")
    with pytest.raises(ValueError, match="must be UTC"):
        reconciliation_module._datetime_to_epoch_us(
            datetime(2026, 10, 26, 16, tzinfo=timezone(timedelta(hours=8))), "initial"
        )
    with pytest.raises(ReconciliationJournalCorruptionError, match="datetime range"):
        reconciliation_module._epoch_us_to_datetime(9_223_372_036_854_775_807, "instant")
    with pytest.raises(ReconciliationJournalCorruptionError, match="persisted instant is invalid"):
        reconciliation_module._epoch_us_to_datetime(True, "instant")
    assert reconciliation_module._persisted_datetime(None, "instant") is None
    with pytest.raises(ReconciliationJournalCorruptionError, match="unexpectedly null"):
        reconciliation_module._required_persisted_datetime(None, "instant")
    assert reconciliation_module._format_occurrence(OCCURRENCE).endswith("Z")
    assert reconciliation_module._canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    with pytest.raises(ReconciliationJournalCorruptionError, match="canonicalized"):
        reconciliation_module._canonical_json({"bad": float("nan")})
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ReconciliationJournalCorruptionError, match="canonicalized"):
        reconciliation_module._canonical_json(cyclic)
    with pytest.raises(ValueError, match="bounded non-negative"):
        reconciliation_module._validate_revision(-1)
    with pytest.raises(ReconciliationJournalCorruptionError, match="persisted value is invalid"):
        reconciliation_module._integer(True, "value")


def test_fixed_identity_and_reference_validators_reject_unbounded_values() -> None:
    """Fixed job identity, opaque basis, identifier and digest formats are strict."""
    reconciliation_module._validate_job_id(SINA_FZB_RECONCILIATION_JOB_ID)
    reconciliation_module._validate_identifier(JOB_VERSION, "job_version", 128)
    assert reconciliation_module._validate_initial_from_basis(INITIAL_FROM_BASIS)
    reconciliation_module._validate_sha256("a" * 64, "digest")
    with pytest.raises(ValueError, match="reviewed Sina FZB capture job"):
        reconciliation_module._validate_job_id("other-job")
    for invalid in ("", "bad version", "x" * 129, None):
        with pytest.raises(ValueError, match="bounded identifier"):
            reconciliation_module._validate_identifier(invalid, "job_version", 128)
    for invalid in ("", "has space", "x" * 256, None):
        with pytest.raises(ValueError, match="initial_from_basis"):
            reconciliation_module._validate_initial_from_basis(invalid)
    for invalid in ("A" * 64, "x" * 64, None):
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            reconciliation_module._validate_sha256(invalid, "digest")
    assert reconciliation_module._is_opaque_reference("opaque:value")
    assert not reconciliation_module._is_opaque_reference("")
    assert not reconciliation_module._is_opaque_reference("contains\ncontrol")


@pytest.mark.parametrize(
    ("crontab", "message"),
    [
        ("", "non-empty and bounded"),
        ("* * * * * " + "x" * 251, "non-empty and bounded"),
        ("* * * * *\n", "control character"),
        ("* * * *", "five-field"),
        ("0 0 * * 1", "weekday must use"),
        ("99 99 * * *", "not a supported five-field"),
    ],
)
def test_schedule_snapshot_rejects_bad_cron_contracts(crontab: str, message: str) -> None:
    """The frozen cron snapshot accepts only the reviewed bounded five-field form."""
    snapshot, fingerprint = _schedule_snapshot(crontab)
    with pytest.raises(ValueError, match=message):
        reconciliation_module._validate_schedule_snapshot(
            snapshot,
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )


def test_schedule_snapshot_rejects_invalid_encoding_size_and_json_constants() -> None:
    """Snapshot byte limits and standard JSON encoding are checked before storage."""
    snapshot, fingerprint = _schedule_snapshot()
    with pytest.raises(ValueError, match="must be a JSON string"):
        reconciliation_module._validate_schedule_snapshot(
            None,  # type: ignore[arg-type]
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        reconciliation_module._validate_schedule_snapshot(
            "\ud800",
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )
    with pytest.raises(ValueError, match="byte limit"):
        reconciliation_module._validate_schedule_snapshot(
            " " * (reconciliation_module.MAX_SCHEDULE_SNAPSHOT_BYTES + 1),
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )
    with pytest.raises(ValueError, match="unambiguous JSON"):
        reconciliation_module._validate_schedule_snapshot(
            '{"value":NaN}',
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )
    with pytest.raises(ValueError, match="unambiguous JSON"):
        reconciliation_module._validate_schedule_snapshot(
            '{"value":1,"value":2}',
            job_id=SINA_FZB_RECONCILIATION_JOB_ID,
            job_version=JOB_VERSION,
            fingerprint=fingerprint,
        )
    assert snapshot


def test_occurrence_and_grace_validators_enforce_bounded_order() -> None:
    """Expected slots must be a bounded tuple and the grace cutoff exact."""
    assert reconciliation_module._validate_occurrences_input((OCCURRENCE,))
    for invalid in (
        [OCCURRENCE],
        tuple(OCCURRENCE + timedelta(minutes=index) for index in range(129)),
        (OCCURRENCE, OCCURRENCE),
        (OCCURRENCE + timedelta(minutes=1), OCCURRENCE),
    ):
        with pytest.raises(ValueError):
            reconciliation_module._validate_occurrences_input(invalid)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="checked_at minus"):
        reconciliation_module._validate_grace_window(
            checked_at_us=10_000_000,
            due_through_us=0,
            grace_seconds=RECONCILIATION_GRACE_SECONDS,
        )


def test_snapshot_crontab_and_occurrence_prefix_fail_closed() -> None:
    """A valid prefix progresses exactly once; malformed state or skipped slots fail."""
    snapshot, _ = _schedule_snapshot()
    assert reconciliation_module._snapshot_crontab(snapshot) == CRONTAB
    with pytest.raises(ReconciliationJournalCorruptionError, match="cannot be parsed"):
        reconciliation_module._snapshot_crontab("{bad json")
    with pytest.raises(ReconciliationJournalCorruptionError, match="fields are invalid"):
        reconciliation_module._snapshot_crontab("{}")
    with pytest.raises(ReconciliationJournalCorruptionError, match="crontab is invalid"):
        bad_crontab = json.loads(snapshot)
        bad_crontab["crontab"] = 12
        reconciliation_module._snapshot_crontab(
            json.dumps(bad_crontab, sort_keys=True, separators=(",", ":"))
        )

    occurrence_us = int(OCCURRENCE.timestamp() * 1_000_000)
    initial_us = int(INITIAL_FROM.timestamp() * 1_000_000)
    reconciliation_module._validate_expected_occurrence_prefix(
        crontab=CRONTAB,
        expected_from_us=initial_us,
        due_through_us=occurrence_us,
        expected_occurrences_us=(occurrence_us,),
        new_cursor_us=occurrence_us,
    )
    with pytest.raises(ValueError, match="no-progress batch"):
        reconciliation_module._validate_expected_occurrence_prefix(
            crontab=CRONTAB,
            expected_from_us=initial_us,
            due_through_us=initial_us,
            expected_occurrences_us=(),
            new_cursor_us=initial_us + 1,
        )
    with pytest.raises(ValueError, match="cron prefix"):
        reconciliation_module._validate_expected_occurrence_prefix(
            crontab=CRONTAB,
            expected_from_us=initial_us,
            due_through_us=occurrence_us,
            expected_occurrences_us=(),
            new_cursor_us=occurrence_us,
        )
    with pytest.raises(ValueError, match="bounded cron progress"):
        reconciliation_module._validate_expected_occurrence_prefix(
            crontab=CRONTAB,
            expected_from_us=initial_us,
            due_through_us=occurrence_us,
            expected_occurrences_us=(occurrence_us,),
            new_cursor_us=INITIAL_FROM,
        )
