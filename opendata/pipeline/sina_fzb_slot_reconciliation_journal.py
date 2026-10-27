"""Durable, cron-only reconciliation of Sina FZB scheduler slots.

This journal records whether an expected UTC cron slot had a fanout group at
the time it was checked. It never creates a fanout group or dispatches a run.
The initial boundary is an explicit caller declaration; history before that
boundary is persisted as ``NOT_ASSESSED``. The v1 schedule is immutable for
the lifetime of this one-generation-per-job journal.

The stored fanout reference proves only that a group row was observed. It does
not prove the group completed collection, that its selection represented a
historical universe, or that data coverage is complete. A group discovered
after a missing observation is recorded separately as late evidence while the
original missing timestamp and UNKNOWN historical selection remain intact.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, TypeGuard

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    and_,
    insert,
    join,
    select,
    update,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Connection, RowMapping

SINA_FZB_RECONCILIATION_JOB_ID = "sina-fzb-capture"
RECONCILIATION_GRACE_SECONDS = 120
MAX_SLOTS_PER_BATCH = 128
MAX_RECOVERY_SLOTS_PER_BATCH = 128
MAX_SCHEDULE_SNAPSHOT_BYTES = 16_384
MAX_JOB_VERSION_LENGTH = 128
MAX_SELECTION_REFERENCE_LENGTH = 512
MAX_INITIAL_FROM_BASIS_LENGTH = 255

CalendarPolicy = Literal["cron_only"]
CursorStatus = Literal["ACTIVE", "BLOCKED"]
SlotStatus = Literal["PRESENT", "MISSING", "RECOVERED"]
FanoutStatus = Literal["PENDING", "ENQUEUED", "BLOCKED"]
SelectionEvidenceStatus = Literal["UNKNOWN", "GROUP_REFERENCE_ONLY"]
RecoveryEvidenceStatus = Literal["LATE_GROUP_ONLY"]

CRON_ONLY: CalendarPolicy = "cron_only"
NOT_ASSESSED = "NOT_ASSESSED"
UNKNOWN: SelectionEvidenceStatus = "UNKNOWN"
GROUP_REFERENCE_ONLY: SelectionEvidenceStatus = "GROUP_REFERENCE_ONLY"
LATE_GROUP_ONLY: RecoveryEvidenceStatus = "LATE_GROUP_ONLY"

SINA_FZB_SLOT_RECONCILIATION_METADATA = MetaData()


def _mysql_identifier_type(length: int) -> String:
    """Build the repository's byte-exact MySQL comparison type."""
    return String(length, collation="utf8mb4_0900_bin")


SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE = Table(
    "od_sina_fzb_slot_reconciliation_state",
    SINA_FZB_SLOT_RECONCILIATION_METADATA,
    Column(
        "job_id",
        String(128).with_variant(_mysql_identifier_type(128), "mysql"),
        primary_key=True,
    ),
    Column(
        "job_version",
        String(MAX_JOB_VERSION_LENGTH).with_variant(
            _mysql_identifier_type(MAX_JOB_VERSION_LENGTH), "mysql"
        ),
        nullable=False,
    ),
    Column("schedule_fingerprint", String(64), nullable=False),
    Column(
        "schedule_snapshot_json",
        Text().with_variant(MEDIUMTEXT(), "mysql"),
        nullable=False,
    ),
    Column(
        "calendar_policy",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=False,
    ),
    Column(
        "calendar_id",
        String(128).with_variant(_mysql_identifier_type(128), "mysql"),
        nullable=True,
    ),
    Column(
        "calendar_version",
        String(128).with_variant(_mysql_identifier_type(128), "mysql"),
        nullable=True,
    ),
    Column("calendar_sha256", String(64), nullable=True),
    Column("grace_seconds", BigInteger, nullable=False),
    Column(
        "earlier_history_status",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=False,
    ),
    Column(
        "initial_from_basis",
        String(MAX_INITIAL_FROM_BASIS_LENGTH).with_variant(
            _mysql_identifier_type(MAX_INITIAL_FROM_BASIS_LENGTH), "mysql"
        ),
        nullable=False,
    ),
    Column("initial_from_us", BigInteger, nullable=False),
    Column("reconciled_through_us", BigInteger, nullable=False),
    Column("cursor_revision", BigInteger, nullable=False),
    Column(
        "status",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=False,
    ),
    Column("created_at_us", BigInteger, nullable=False),
    Column("updated_at_us", BigInteger, nullable=False),
    Column("last_checked_at_us", BigInteger, nullable=False),
    CheckConstraint(
        "job_id = 'sina-fzb-capture'",
        name="ck_od_sina_fzb_reconcile_job",
    ),
    CheckConstraint(
        "calendar_policy = 'cron_only'",
        name="ck_od_sina_fzb_reconcile_calendar_policy",
    ),
    CheckConstraint(
        "calendar_id IS NULL AND calendar_version IS NULL AND calendar_sha256 IS NULL",
        name="ck_od_sina_fzb_reconcile_no_calendar",
    ),
    CheckConstraint(
        f"grace_seconds = {RECONCILIATION_GRACE_SECONDS}",
        name="ck_od_sina_fzb_reconcile_grace",
    ),
    CheckConstraint(
        "earlier_history_status = 'NOT_ASSESSED'",
        name="ck_od_sina_fzb_reconcile_history",
    ),
    CheckConstraint(
        "length(schedule_fingerprint) = 64",
        name="ck_od_sina_fzb_reconcile_fingerprint_len",
    ),
    CheckConstraint(
        f"length(initial_from_basis) BETWEEN 1 AND {MAX_INITIAL_FROM_BASIS_LENGTH}",
        name="ck_od_sina_fzb_reconcile_basis_nonempty",
    ),
    CheckConstraint(
        "reconciled_through_us >= initial_from_us",
        name="ck_od_sina_fzb_reconcile_watermark",
    ),
    CheckConstraint(
        "cursor_revision >= 0",
        name="ck_od_sina_fzb_reconcile_revision",
    ),
    CheckConstraint(
        "status IN ('ACTIVE', 'BLOCKED')",
        name="ck_od_sina_fzb_reconcile_status",
    ),
)

SINA_FZB_RECONCILED_SLOT_TABLE = Table(
    "od_sina_fzb_reconciled_slots",
    SINA_FZB_SLOT_RECONCILIATION_METADATA,
    Column(
        "job_id",
        String(128).with_variant(_mysql_identifier_type(128), "mysql"),
        primary_key=True,
    ),
    Column("occurrence_at_us", BigInteger, primary_key=True),
    Column(
        "job_version",
        String(MAX_JOB_VERSION_LENGTH).with_variant(
            _mysql_identifier_type(MAX_JOB_VERSION_LENGTH), "mysql"
        ),
        nullable=False,
    ),
    Column("schedule_fingerprint", String(64), nullable=False),
    Column(
        "status",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=False,
    ),
    Column("first_checked_at_us", BigInteger, nullable=False),
    Column("first_missing_at_us", BigInteger, nullable=True),
    Column(
        "first_fanout_status",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=True,
    ),
    Column("first_manifest_sha256", String(64), nullable=True),
    Column(
        "first_selection_evidence_status",
        String(32).with_variant(_mysql_identifier_type(32), "mysql"),
        nullable=False,
    ),
    Column(
        "first_selection_snapshot_ref",
        String(MAX_SELECTION_REFERENCE_LENGTH).with_variant(
            _mysql_identifier_type(MAX_SELECTION_REFERENCE_LENGTH), "mysql"
        ),
        nullable=True,
    ),
    Column("recovered_at_us", BigInteger, nullable=True),
    Column(
        "recovery_fanout_status",
        String(16).with_variant(_mysql_identifier_type(16), "mysql"),
        nullable=True,
    ),
    Column("recovery_manifest_sha256", String(64), nullable=True),
    Column(
        "recovery_group_selection_snapshot_ref",
        String(MAX_SELECTION_REFERENCE_LENGTH).with_variant(
            _mysql_identifier_type(MAX_SELECTION_REFERENCE_LENGTH), "mysql"
        ),
        nullable=True,
    ),
    Column(
        "recovery_evidence_status",
        String(24).with_variant(_mysql_identifier_type(24), "mysql"),
        nullable=True,
    ),
    CheckConstraint(
        "job_id = 'sina-fzb-capture'",
        name="ck_od_sina_fzb_reconciled_job",
    ),
    CheckConstraint(
        "status IN ('PRESENT', 'MISSING', 'RECOVERED')",
        name="ck_od_sina_fzb_reconciled_status",
    ),
    CheckConstraint(
        "length(schedule_fingerprint) = 64",
        name="ck_od_sina_fzb_reconciled_fingerprint_len",
    ),
    CheckConstraint(
        "first_selection_evidence_status IN ('UNKNOWN', 'GROUP_REFERENCE_ONLY')",
        name="ck_od_sina_fzb_reconciled_selection_evidence",
    ),
    CheckConstraint(
        "first_fanout_status IS NULL OR first_fanout_status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
        name="ck_od_sina_fzb_reconciled_first_fanout_status",
    ),
    CheckConstraint(
        "recovery_fanout_status IS NULL OR "
        "recovery_fanout_status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
        name="ck_od_sina_fzb_reconciled_recovery_fanout_status",
    ),
    CheckConstraint(
        "(status = 'PRESENT' AND first_missing_at_us IS NULL "
        "AND first_fanout_status IS NOT NULL "
        "AND first_manifest_sha256 IS NOT NULL "
        "AND first_selection_evidence_status = 'GROUP_REFERENCE_ONLY' "
        "AND first_selection_snapshot_ref IS NOT NULL "
        "AND recovered_at_us IS NULL AND recovery_fanout_status IS NULL "
        "AND recovery_manifest_sha256 IS NULL "
        "AND recovery_group_selection_snapshot_ref IS NULL "
        "AND recovery_evidence_status IS NULL) OR "
        "(status = 'MISSING' AND first_missing_at_us = first_checked_at_us "
        "AND first_fanout_status IS NULL AND first_manifest_sha256 IS NULL "
        "AND first_selection_evidence_status = 'UNKNOWN' "
        "AND first_selection_snapshot_ref IS NULL "
        "AND recovered_at_us IS NULL AND recovery_fanout_status IS NULL "
        "AND recovery_manifest_sha256 IS NULL "
        "AND recovery_group_selection_snapshot_ref IS NULL "
        "AND recovery_evidence_status IS NULL) OR "
        "(status = 'RECOVERED' AND first_missing_at_us = first_checked_at_us "
        "AND first_fanout_status IS NULL AND first_manifest_sha256 IS NULL "
        "AND first_selection_evidence_status = 'UNKNOWN' "
        "AND first_selection_snapshot_ref IS NULL "
        "AND recovered_at_us IS NOT NULL "
        "AND recovered_at_us >= first_missing_at_us "
        "AND recovery_fanout_status IS NOT NULL "
        "AND recovery_manifest_sha256 IS NOT NULL "
        "AND recovery_group_selection_snapshot_ref IS NOT NULL "
        "AND recovery_evidence_status = 'LATE_GROUP_ONLY')",
        name="ck_od_sina_fzb_reconciled_state_shape",
    ),
    CheckConstraint(
        "first_manifest_sha256 IS NULL OR length(first_manifest_sha256) = 64",
        name="ck_od_sina_fzb_reconciled_first_sha_len",
    ),
    CheckConstraint(
        "recovery_manifest_sha256 IS NULL OR length(recovery_manifest_sha256) = 64",
        name="ck_od_sina_fzb_reconciled_recovery_sha_len",
    ),
)
Index(
    "ix_od_sina_fzb_reconciled_recovery",
    SINA_FZB_RECONCILED_SLOT_TABLE.c.job_id,
    SINA_FZB_RECONCILED_SLOT_TABLE.c.status,
    SINA_FZB_RECONCILED_SLOT_TABLE.c.occurrence_at_us,
)

_FANOUT_GROUPS = Table(
    "od_sina_fzb_fanout_groups",
    MetaData(),
    Column("fanout_key", String(64), nullable=False),
    Column("job_id", String(128), nullable=False),
    Column("job_version", String(128), nullable=False),
    Column("occurrence_at_us", BigInteger, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("manifest_json", Text, nullable=False),
    Column("status", String(16), nullable=False),
)

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_MICROSECONDS_PER_SECOND = 1_000_000
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$", re.ASCII)
_BASIS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,254}$", re.ASCII)
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$", re.ASCII)
_WEEKDAY_FIELD_PATTERN = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)(?:-(?:mon|tue|wed|thu|fri|sat|sun))?"
    r"(?:/\d+)?(?:,(?:mon|tue|wed|thu|fri|sat|sun)"
    r"(?:-(?:mon|tue|wed|thu|fri|sat|sun))?(?:/\d+)?)*$",
    re.IGNORECASE,
)
_SCHEDULE_KEYS = frozenset(
    {
        "calendar_policy",
        "contract_version",
        "crontab",
        "grace_seconds",
        "job_id",
        "job_version",
        "max_recovery_slots_per_batch",
        "max_slots_per_batch",
        "schedule_kind",
        "selection_policy",
        "timezone",
    }
)
_FANOUT_MANIFEST_KEYS = frozenset(
    {
        "contract_version",
        "job_id",
        "job_version",
        "occurrence",
        "manifest_id",
        "selection_snapshot_ref",
        "symbols",
    }
)


@dataclass(frozen=True, slots=True)
class ReconciliationCursor:
    """Immutable view of the one frozen cron generation for the Sina job."""

    job_id: str
    job_version: str
    schedule_fingerprint: str
    schedule_snapshot_json: str
    calendar_policy: CalendarPolicy
    grace_seconds: int
    initial_from_basis: str
    initial_from: datetime
    reconciled_through: datetime
    cursor_revision: int
    earlier_history_status: str
    status: CursorStatus
    created_at: datetime
    updated_at: datetime
    last_checked_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciledSlot:
    """First slot observation and any separate late-group recovery evidence."""

    occurrence: datetime
    status: SlotStatus
    first_checked_at: datetime
    first_missing_at: datetime | None
    recovered_at: datetime | None
    first_fanout_status: FanoutStatus | None
    first_manifest_sha256: str | None
    first_selection_evidence_status: SelectionEvidenceStatus
    first_selection_snapshot_ref: str | None
    recovery_fanout_status: FanoutStatus | None
    recovery_manifest_sha256: str | None
    recovery_group_selection_snapshot_ref: str | None
    recovery_evidence_status: RecoveryEvidenceStatus | None


@dataclass(frozen=True, slots=True)
class ReconciliationBatch:
    """Result of one atomic cursor advance and bounded late-group repair."""

    cursor: ReconciliationCursor
    slots: tuple[ReconciledSlot, ...]
    recovered_slots: tuple[ReconciledSlot, ...]


class ReconciliationContractConflictError(ValueError):
    """Raised when an initialized job is replayed with a changed contract."""


class ReconciliationScheduleConflictError(ReconciliationContractConflictError):
    """Raised when job version or cron fingerprint differs from the frozen row."""


class ReconciliationCASConflictError(RuntimeError):
    """Raised when another worker advanced the cursor or the state is blocked."""


class ReconciliationJournalCorruptionError(RuntimeError):
    """Raised when persisted state, slot, or fanout evidence is inconsistent."""


class SinaFzbSlotReconciliationJournal:
    """Reconcile cron slots against the durable Sina FZB fanout group table.

    Args:
        engine: Caller-owned synchronous SQLAlchemy engine. Both 0013 fanout
            and 0014 reconciliation tables must already exist.
        clock_policy: ``database`` rejects a future caller timestamp relative
            to the active database clock and is the multi-host default.
            ``caller`` uses the explicitly supplied UTC timestamp.

    The journal is read/write inert until a method is called; it does not
    create tables, load application settings, or schedule jobs. There is one
    frozen schedule generation per job. Changing its version or fingerprint
    requires a separately designed, non-overlapping cutover; this API fails
    closed instead of rolling the cursor. Recovery records only a late group
    reference and never retries or dispatches a source request.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock_policy: Literal["caller", "database"] = "database",
    ) -> None:
        """Bind an explicit engine without connecting or creating schema."""
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if clock_policy not in {"caller", "database"}:
            raise ValueError("clock_policy must be 'caller' or 'database'")
        if clock_policy == "database" and engine.dialect.name not in {
            "mysql",
            "postgresql",
            "sqlite",
        }:
            raise ValueError(f"database clock policy is unsupported for {engine.dialect.name!r}")
        self._engine = engine
        self._clock_policy = clock_policy

    def initialize(
        self,
        *,
        job_id: str,
        job_version: str,
        schedule_snapshot_json: str,
        schedule_fingerprint: str,
        initial_from_basis: str,
        initial_from: datetime,
        now: datetime,
    ) -> ReconciliationCursor:
        """Freeze a cron-only schedule and explicit exclusive lower boundary.

        An exact replay returns the existing cursor. Any changed snapshot,
        fingerprint, job version, lower boundary, or boundary basis conflicts;
        the state is never reset or rolled over automatically.
        """
        _validate_job_id(job_id)
        _validate_identifier(job_version, "job_version", MAX_JOB_VERSION_LENGTH)
        _validate_schedule_snapshot(
            schedule_snapshot_json,
            job_id=job_id,
            job_version=job_version,
            fingerprint=schedule_fingerprint,
        )
        basis = _validate_initial_from_basis(initial_from_basis)
        initial_from_us = _datetime_to_epoch_us(initial_from, "initial_from")
        now_us_input = _datetime_to_epoch_us(now, "now")
        table = SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE
        try:
            with self._engine.begin() as connection:
                _begin_sqlite_write(connection)
                existing_row = self._select_cursor_row(connection, job_id, for_update=True)
                if existing_row is not None:
                    existing_cursor = _cursor_from_row(existing_row)
                    if not _same_initial_contract(
                        existing_cursor,
                        job_version=job_version,
                        snapshot_json=schedule_snapshot_json,
                        fingerprint=schedule_fingerprint,
                        initial_from=initial_from_us,
                        initial_from_basis=basis,
                    ):
                        raise ReconciliationScheduleConflictError(
                            "reconciliation state already exists with a different frozen contract"
                        )
                    return existing_cursor
                clock_us = self._timestamp_us(connection, now_us_input)
                if initial_from_us > clock_us:
                    raise ValueError(
                        "initial_from must not be in the future relative to the configured clock"
                    )
                connection.execute(
                    insert(table).values(
                        job_id=job_id,
                        job_version=job_version,
                        schedule_fingerprint=schedule_fingerprint,
                        schedule_snapshot_json=schedule_snapshot_json,
                        calendar_policy=CRON_ONLY,
                        calendar_id=None,
                        calendar_version=None,
                        calendar_sha256=None,
                        grace_seconds=RECONCILIATION_GRACE_SECONDS,
                        earlier_history_status=NOT_ASSESSED,
                        initial_from_basis=basis,
                        initial_from_us=initial_from_us,
                        reconciled_through_us=initial_from_us,
                        cursor_revision=0,
                        status="ACTIVE",
                        created_at_us=clock_us,
                        updated_at_us=clock_us,
                        last_checked_at_us=clock_us,
                    )
                )
                row = self._select_cursor_row(connection, job_id, for_update=False)
        except IntegrityError:
            raced_cursor = self.read_cursor(job_id)
            if raced_cursor is None:
                raise ReconciliationJournalCorruptionError(
                    "reconciliation identity collided without a state row"
                ) from None
            if not _same_initial_contract(
                raced_cursor,
                job_version=job_version,
                snapshot_json=schedule_snapshot_json,
                fingerprint=schedule_fingerprint,
                initial_from=initial_from_us,
                initial_from_basis=basis,
            ):
                raise ReconciliationScheduleConflictError(
                    "reconciliation state already exists with a different frozen contract"
                ) from None
            return raced_cursor
        if row is None:
            raise ReconciliationJournalCorruptionError("inserted reconciliation state is missing")
        return _cursor_from_row(row)

    def read_cursor(self, job_id: str) -> ReconciliationCursor | None:
        """Read the durable cursor, or return ``None`` before initialization."""
        _validate_job_id(job_id)
        with self._engine.connect() as connection:
            row = self._select_cursor_row(connection, job_id, for_update=False)
        return _cursor_from_row(row) if row is not None else None

    def database_now(self) -> datetime:
        """Read the database UTC clock used to derive grace cutoffs.

        The caller can use this timestamp to enumerate cron slots before
        calling :meth:`reconcile_batch`. The batch rejects a timestamp ahead
        of the database clock, so any time elapsed between these calls only
        makes the cutoff conservative.
        """
        if self._clock_policy != "database":
            raise ValueError("database_now requires clock_policy='database'")
        with self._engine.connect() as connection:
            now_us = _database_clock_us(connection)
        return _epoch_us_to_datetime(now_us, "database clock")

    def reconcile_batch(
        self,
        *,
        job_id: str,
        job_version: str,
        expected_schedule_fingerprint: str,
        expected_cursor_revision: int,
        expected_from: datetime,
        checked_at: datetime,
        due_through: datetime,
        new_cursor: datetime,
        expected_occurrences: tuple[datetime, ...],
    ) -> ReconciliationBatch:
        """Atomically observe cron slots, late groups, and advance the cursor.

        ``expected_occurrences`` must equal the first bounded occurrences from
        the frozen UTC crontab. Each occurrence is in
        ``(expected_from, due_through]``. When more than 128 are due, the
        cursor advances only to the 128th slot; otherwise it advances through
        the due boundary. If the clock moved behind the watermark, an empty
        batch may still perform the bounded late-group scan without moving the
        watermark backward.
        """
        _validate_job_id(job_id)
        _validate_identifier(job_version, "job_version", MAX_JOB_VERSION_LENGTH)
        _validate_sha256(expected_schedule_fingerprint, "expected_schedule_fingerprint")
        _validate_revision(expected_cursor_revision)
        expected_from_us = _datetime_to_epoch_us(expected_from, "expected_from")
        checked_at_us = _datetime_to_epoch_us(checked_at, "checked_at")
        due_through_us = _datetime_to_epoch_us(due_through, "due_through")
        new_cursor_us = _datetime_to_epoch_us(new_cursor, "new_cursor")
        occurrences_us = _validate_occurrences_input(expected_occurrences)

        state = SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE
        slot_table = SINA_FZB_RECONCILED_SLOT_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            cursor_row = self._select_cursor_row(connection, job_id, for_update=True)
            if cursor_row is None:
                raise ReconciliationCASConflictError("reconciliation state is not initialized")
            cursor = _cursor_from_row(cursor_row)
            self._assert_contract_matches(
                cursor,
                job_version=job_version,
                schedule_fingerprint=expected_schedule_fingerprint,
                expected_cursor_revision=expected_cursor_revision,
                expected_from_us=expected_from_us,
            )
            if cursor.status != "ACTIVE":
                raise ReconciliationCASConflictError("reconciliation state is blocked")
            clock_us = self._timestamp_us(connection, checked_at_us)
            self._reject_future_timestamp(checked_at_us, clock_us)
            _validate_grace_window(
                checked_at_us=checked_at_us,
                due_through_us=due_through_us,
                grace_seconds=cursor.grace_seconds,
            )
            _validate_expected_occurrence_prefix(
                crontab=_snapshot_crontab(cursor.schedule_snapshot_json),
                expected_from_us=expected_from_us,
                due_through_us=due_through_us,
                expected_occurrences_us=occurrences_us,
                new_cursor_us=new_cursor_us,
            )

            recovered_slots = self._recover_late_groups(
                connection,
                job_id=job_id,
                job_version=job_version,
                schedule_fingerprint=expected_schedule_fingerprint,
                checked_at_us=checked_at_us,
                recovered_at_us=clock_us,
            )
            observed_groups = self._read_fanout_groups(
                connection,
                job_id=job_id,
                job_version=job_version,
                occurrences_us=occurrences_us,
            )
            new_slots: list[ReconciledSlot] = []
            for occurrence_us in occurrences_us:
                _epoch_us_to_datetime(occurrence_us, "occurrence_at_us")
                group = observed_groups.get(occurrence_us)
                if group is None:
                    values = _missing_slot_values(
                        job_id=job_id,
                        occurrence_us=occurrence_us,
                        job_version=job_version,
                        fingerprint=expected_schedule_fingerprint,
                        checked_at_us=checked_at_us,
                    )
                else:
                    values = _present_slot_values(
                        job_id=job_id,
                        occurrence_us=occurrence_us,
                        job_version=job_version,
                        fingerprint=expected_schedule_fingerprint,
                        checked_at_us=checked_at_us,
                        group=group,
                    )
                try:
                    connection.execute(insert(slot_table).values(**values))
                except IntegrityError as exc:
                    raise ReconciliationJournalCorruptionError(
                        "a new cron slot already exists beyond the durable cursor"
                    ) from exc
                new_slots.append(_slot_from_values(values))

            next_revision = cursor.cursor_revision + 1
            new_watermark_us = max(expected_from_us, new_cursor_us)
            last_checked_us = max(
                _integer(cursor_row["last_checked_at_us"], "last_checked_at_us"), checked_at_us
            )
            updated = connection.execute(
                update(state)
                .where(
                    and_(
                        state.c.job_id == job_id,
                        state.c.cursor_revision == expected_cursor_revision,
                        state.c.reconciled_through_us == expected_from_us,
                        state.c.schedule_fingerprint == expected_schedule_fingerprint,
                        state.c.status == "ACTIVE",
                    )
                )
                .values(
                    reconciled_through_us=new_watermark_us,
                    cursor_revision=next_revision,
                    updated_at_us=max(
                        clock_us, _integer(cursor_row["updated_at_us"], "updated_at_us")
                    ),
                    last_checked_at_us=last_checked_us,
                )
            )
            if updated.rowcount != 1:
                raise ReconciliationCASConflictError("reconciliation cursor compare-and-set failed")
            next_row = self._select_cursor_row(connection, job_id, for_update=False)
            if next_row is None:
                raise ReconciliationJournalCorruptionError(
                    "updated reconciliation cursor is missing"
                )
            return ReconciliationBatch(
                cursor=_cursor_from_row(next_row),
                slots=tuple(new_slots),
                recovered_slots=tuple(recovered_slots),
            )

    def _select_cursor_row(
        self,
        connection: Connection,
        job_id: str,
        *,
        for_update: bool,
    ) -> RowMapping | None:
        """Read one cursor row, optionally locking it for a write transaction."""
        statement = select(SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE).where(
            SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE.c.job_id == job_id
        )
        if for_update and connection.dialect.name in {"mysql", "postgresql"}:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

    def _assert_contract_matches(
        self,
        cursor: ReconciliationCursor,
        *,
        job_version: str,
        schedule_fingerprint: str,
        expected_cursor_revision: int,
        expected_from_us: int,
    ) -> None:
        """Reject generation drift before any slot insert or recovery update."""
        if cursor.job_version != job_version or cursor.schedule_fingerprint != schedule_fingerprint:
            raise ReconciliationScheduleConflictError(
                "reconciliation generation differs from its immutable frozen contract"
            )
        if (
            cursor.cursor_revision != expected_cursor_revision
            or _datetime_to_epoch_us(cursor.reconciled_through, "reconciled_through")
            != expected_from_us
        ):
            raise ReconciliationCASConflictError("reconciliation cursor is stale")

    def _read_fanout_groups(
        self,
        connection: Connection,
        *,
        job_id: str,
        job_version: str,
        occurrences_us: tuple[int, ...],
    ) -> dict[int, _FanoutObservation]:
        """Load groups for the bounded new-slot batch and verify their evidence."""
        if not occurrences_us:
            return {}
        rows = (
            connection.execute(
                select(_FANOUT_GROUPS).where(
                    and_(
                        _FANOUT_GROUPS.c.job_id == job_id,
                        _FANOUT_GROUPS.c.occurrence_at_us.in_(occurrences_us),
                    )
                )
            )
            .mappings()
            .all()
        )
        observations: dict[int, _FanoutObservation] = {}
        for row in rows:
            observation = _fanout_observation_from_row(row, expected_job_version=job_version)
            occurrence_us = observation.occurrence_at_us
            if occurrence_us in observations:
                raise ReconciliationJournalCorruptionError("fanout slot has duplicate group rows")
            observations[occurrence_us] = observation
        return observations

    def _recover_late_groups(
        self,
        connection: Connection,
        *,
        job_id: str,
        job_version: str,
        schedule_fingerprint: str,
        checked_at_us: int,
        recovered_at_us: int,
    ) -> list[ReconciledSlot]:
        """Mark at most 128 matched late groups without erasing first-missing facts."""
        slots = SINA_FZB_RECONCILED_SLOT_TABLE
        matched = join(
            slots,
            _FANOUT_GROUPS,
            and_(
                slots.c.job_id == _FANOUT_GROUPS.c.job_id,
                slots.c.occurrence_at_us == _FANOUT_GROUPS.c.occurrence_at_us,
            ),
        )
        query = (
            select(
                slots,
                _FANOUT_GROUPS.c.job_id.label("group_job_id"),
                _FANOUT_GROUPS.c.fanout_key.label("group_fanout_key"),
                _FANOUT_GROUPS.c.job_version.label("group_job_version"),
                _FANOUT_GROUPS.c.occurrence_at_us.label("group_occurrence_at_us"),
                _FANOUT_GROUPS.c.manifest_sha256.label("group_manifest_sha256"),
                _FANOUT_GROUPS.c.manifest_json.label("group_manifest_json"),
                _FANOUT_GROUPS.c.status.label("group_status"),
            )
            .select_from(matched)
            .where(
                and_(
                    slots.c.job_id == job_id,
                    slots.c.status == "MISSING",
                    slots.c.first_missing_at_us <= checked_at_us,
                )
            )
            .order_by(slots.c.occurrence_at_us)
            .limit(MAX_RECOVERY_SLOTS_PER_BATCH)
        )
        rows = connection.execute(query).mappings().all()
        recovered: list[ReconciledSlot] = []
        for row in rows:
            occurrence_us = _integer(row["occurrence_at_us"], "occurrence_at_us")
            group = _fanout_observation_from_row(
                {
                    "job_id": row["group_job_id"],
                    "fanout_key": row["group_fanout_key"],
                    "job_version": row["group_job_version"],
                    "occurrence_at_us": row["group_occurrence_at_us"],
                    "manifest_sha256": row["group_manifest_sha256"],
                    "manifest_json": row["group_manifest_json"],
                    "status": row["group_status"],
                },
                expected_job_version=job_version,
            )
            if group.occurrence_at_us != occurrence_us:
                raise ReconciliationJournalCorruptionError(
                    "late group slot identity is inconsistent"
                )
            if row["schedule_fingerprint"] != schedule_fingerprint:
                raise ReconciliationJournalCorruptionError(
                    "late missing slot belongs to a different frozen schedule fingerprint"
                )
            if row["job_version"] != job_version:
                raise ReconciliationJournalCorruptionError(
                    "late missing slot job version differs from reconciliation generation"
                )
            if row["first_missing_at_us"] != row["first_checked_at_us"]:
                raise ReconciliationJournalCorruptionError(
                    "persisted first-missing time does not match the first observation"
                )
            if (
                row["first_fanout_status"] is not None
                or row["first_manifest_sha256"] is not None
                or row["first_selection_evidence_status"] != UNKNOWN
                or row["first_selection_snapshot_ref"] is not None
                or row["recovered_at_us"] is not None
                or row["recovery_fanout_status"] is not None
                or row["recovery_manifest_sha256"] is not None
                or row["recovery_group_selection_snapshot_ref"] is not None
                or row["recovery_evidence_status"] is not None
            ):
                raise ReconciliationJournalCorruptionError(
                    "persisted missing-slot evidence has unexpected fields"
                )
            update_result = connection.execute(
                update(slots)
                .where(
                    and_(
                        slots.c.job_id == job_id,
                        slots.c.occurrence_at_us == occurrence_us,
                        slots.c.status == "MISSING",
                    )
                )
                .values(
                    status="RECOVERED",
                    recovered_at_us=recovered_at_us,
                    recovery_fanout_status=group.status,
                    recovery_manifest_sha256=group.manifest_sha256,
                    recovery_group_selection_snapshot_ref=group.selection_snapshot_ref,
                    recovery_evidence_status=LATE_GROUP_ONLY,
                )
            )
            if update_result.rowcount != 1:
                raise ReconciliationCASConflictError("late-slot recovery compare-and-set failed")
            recovered.append(
                ReconciledSlot(
                    occurrence=_epoch_us_to_datetime(occurrence_us, "occurrence_at_us"),
                    status="RECOVERED",
                    first_checked_at=_required_persisted_datetime(
                        row["first_checked_at_us"], "first_checked_at_us"
                    ),
                    first_missing_at=_required_persisted_datetime(
                        row["first_missing_at_us"], "first_missing_at_us"
                    ),
                    recovered_at=_epoch_us_to_datetime(recovered_at_us, "recovered_at_us"),
                    first_fanout_status=None,
                    first_manifest_sha256=None,
                    first_selection_evidence_status=UNKNOWN,
                    first_selection_snapshot_ref=None,
                    recovery_fanout_status=group.status,
                    recovery_manifest_sha256=group.manifest_sha256,
                    recovery_group_selection_snapshot_ref=group.selection_snapshot_ref,
                    recovery_evidence_status=LATE_GROUP_ONLY,
                )
            )
        return recovered

    def _timestamp_us(self, connection: Connection, caller_timestamp_us: int) -> int:
        """Read the selected clock for audit timestamps and future-clock checks."""
        if self._clock_policy == "caller":
            return caller_timestamp_us
        return _database_clock_us(connection)

    def _reject_future_timestamp(self, caller_timestamp_us: int, clock_us: int) -> None:
        """Reject caller cutoffs ahead of the selected database clock."""
        if self._clock_policy == "database" and caller_timestamp_us > clock_us:
            raise ValueError("checked timestamps must not be ahead of the database clock")


@dataclass(frozen=True, slots=True)
class _FanoutObservation:
    """Validated group data observed at one job-slot key."""

    occurrence_at_us: int
    status: FanoutStatus
    manifest_sha256: str
    selection_snapshot_ref: str


def _validate_job_id(job_id: str) -> None:
    """Accept only the reviewed Sina FZB fixed job id."""
    if job_id != SINA_FZB_RECONCILIATION_JOB_ID:
        raise ValueError("job_id must identify the reviewed Sina FZB capture job")


def _validate_identifier(value: str, field: str, maximum: int) -> None:
    """Validate a bounded immutable identifier without preserving raw errors."""
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded identifier")
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded identifier")


def _validate_initial_from_basis(value: str) -> str:
    """Require a short non-secret caller reference for the explicit boundary."""
    if not isinstance(value, str) or _BASIS_PATTERN.fullmatch(value) is None:
        raise ValueError("initial_from_basis must be a short non-secret opaque reference")
    return value


def _validate_sha256(value: str, field: str) -> None:
    """Require lowercase SHA-256 hex encoding."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be lowercase SHA-256 hex")


def _validate_schedule_snapshot(
    schedule_snapshot_json: str,
    *,
    job_id: str,
    job_version: str,
    fingerprint: str,
) -> dict[str, object]:
    """Validate the exact immutable UTC cron-only snapshot and its digest."""
    _validate_sha256(fingerprint, "schedule_fingerprint")
    if not isinstance(schedule_snapshot_json, str):
        raise ValueError("schedule_snapshot_json must be a JSON string")
    try:
        encoded = schedule_snapshot_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("schedule_snapshot_json must be valid UTF-8") from exc
    if len(encoded) > MAX_SCHEDULE_SNAPSHOT_BYTES:
        raise ValueError("schedule_snapshot_json exceeds its byte limit")
    try:
        payload = json.loads(
            schedule_snapshot_json,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("schedule_snapshot_json nesting is too deep") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("schedule_snapshot_json must contain unambiguous JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _SCHEDULE_KEYS:
        raise ValueError("schedule snapshot must contain the exact cron-only contract fields")
    if (
        type(payload["contract_version"]) is not int
        or payload["contract_version"] != 1
        or payload["calendar_policy"] != CRON_ONLY
        or payload["schedule_kind"] != "cron"
        or payload["timezone"] != "UTC"
        or payload["selection_policy"] != "unknown_if_no_slot_evidence"
        or type(payload["grace_seconds"]) is not int
        or payload["grace_seconds"] != RECONCILIATION_GRACE_SECONDS
        or type(payload["max_slots_per_batch"]) is not int
        or payload["max_slots_per_batch"] != MAX_SLOTS_PER_BATCH
        or type(payload["max_recovery_slots_per_batch"]) is not int
        or payload["max_recovery_slots_per_batch"] != MAX_RECOVERY_SLOTS_PER_BATCH
        or payload["job_id"] != job_id
        or payload["job_version"] != job_version
    ):
        raise ValueError("schedule snapshot does not match the fixed cron-only contract")
    crontab = payload["crontab"]
    if not isinstance(crontab, str) or not crontab or len(crontab) > 255:
        raise ValueError("schedule snapshot crontab must be non-empty and bounded")
    if any(ord(character) < 32 for character in crontab):
        raise ValueError("schedule snapshot crontab contains a control character")
    fields = crontab.split()
    if len(fields) != 5:
        raise ValueError("schedule snapshot must contain a five-field cron expression")
    if fields[4] != "*" and _WEEKDAY_FIELD_PATTERN.fullmatch(fields[4]) is None:
        raise ValueError("schedule snapshot weekday must use '*' or named weekdays")
    try:
        CronTrigger.from_crontab(crontab, timezone=_UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "schedule snapshot crontab is not a supported five-field UTC cron"
        ) from exc
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != schedule_snapshot_json:
        raise ValueError("schedule_snapshot_json must use canonical JSON encoding")
    if hashlib.sha256(encoded).hexdigest() != fingerprint:
        raise ValueError("schedule_fingerprint does not match canonical snapshot bytes")
    return payload


def _snapshot_crontab(snapshot_json: str) -> str:
    """Read the crontab from a previously validated frozen snapshot."""
    try:
        payload = json.loads(snapshot_json, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ReconciliationJournalCorruptionError(
            "persisted schedule snapshot cannot be parsed"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _SCHEDULE_KEYS:
        raise ReconciliationJournalCorruptionError("persisted schedule snapshot fields are invalid")
    crontab = payload.get("crontab")
    if not isinstance(crontab, str):
        raise ReconciliationJournalCorruptionError("persisted schedule crontab is invalid")
    return crontab


def _validate_occurrences_input(occurrences: tuple[datetime, ...]) -> tuple[int, ...]:
    """Validate a concrete bounded tuple of timezone-aware UTC slots."""
    if type(occurrences) is not tuple or len(occurrences) > MAX_SLOTS_PER_BATCH:
        raise ValueError(f"expected_occurrences must be a tuple of at most {MAX_SLOTS_PER_BATCH}")
    values = tuple(_datetime_to_epoch_us(value, "expected_occurrences") for value in occurrences)
    if tuple(sorted(set(values))) != values:
        raise ValueError("expected_occurrences must be sorted and unique")
    return values


def _validate_grace_window(*, checked_at_us: int, due_through_us: int, grace_seconds: int) -> None:
    """Require the immutable grace cutoff to match the checked instant."""
    expected = checked_at_us - grace_seconds * _MICROSECONDS_PER_SECOND
    if due_through_us != expected:
        raise ValueError("due_through must equal checked_at minus the frozen grace period")


def _validate_expected_occurrence_prefix(
    *,
    crontab: str,
    expected_from_us: int,
    due_through_us: int,
    expected_occurrences_us: tuple[int, ...],
    new_cursor_us: int,
) -> None:
    """Match caller slots to the exact first cron prefix, preventing skips."""
    if due_through_us <= expected_from_us:
        if expected_occurrences_us or new_cursor_us != expected_from_us:
            raise ValueError("a clock rollback or covered window permits only a no-progress batch")
        return
    trigger = CronTrigger.from_crontab(crontab, timezone=_UTC)
    initial = _epoch_us_to_datetime(expected_from_us, "expected_from")
    expected: list[int] = []
    previous: datetime | None = initial
    now = initial
    while len(expected) <= MAX_SLOTS_PER_BATCH:
        try:
            next_fire = trigger.get_next_fire_time(previous, now)
        except (OverflowError, ValueError) as exc:
            raise ValueError("frozen cron schedule could not produce a bounded occurrence") from exc
        if next_fire is None:
            break
        next_us = _datetime_to_epoch_us(next_fire, "cron occurrence")
        if next_us <= (expected[-1] if expected else expected_from_us):
            raise ReconciliationJournalCorruptionError("cron trigger did not advance its UTC slot")
        if next_us > due_through_us:
            break
        expected.append(next_us)
        previous = next_fire
        now = next_fire
    more_due = len(expected) > MAX_SLOTS_PER_BATCH
    bounded = tuple(expected[:MAX_SLOTS_PER_BATCH])
    if expected_occurrences_us != bounded:
        raise ValueError("expected_occurrences do not match the frozen cron prefix")
    expected_cursor = bounded[-1] if more_due else due_through_us
    if new_cursor_us != expected_cursor:
        raise ValueError("new_cursor does not match the bounded cron progress rule")


def _missing_slot_values(
    *,
    job_id: str,
    occurrence_us: int,
    job_version: str,
    fingerprint: str,
    checked_at_us: int,
) -> dict[str, object]:
    """Build immutable first-observation fields for a gap with no group row."""
    return {
        "job_id": job_id,
        "occurrence_at_us": occurrence_us,
        "job_version": job_version,
        "schedule_fingerprint": fingerprint,
        "status": "MISSING",
        "first_checked_at_us": checked_at_us,
        "first_missing_at_us": checked_at_us,
        "first_fanout_status": None,
        "first_manifest_sha256": None,
        "first_selection_evidence_status": UNKNOWN,
        "first_selection_snapshot_ref": None,
        "recovered_at_us": None,
        "recovery_fanout_status": None,
        "recovery_manifest_sha256": None,
        "recovery_group_selection_snapshot_ref": None,
        "recovery_evidence_status": None,
    }


def _present_slot_values(
    *,
    job_id: str,
    occurrence_us: int,
    job_version: str,
    fingerprint: str,
    checked_at_us: int,
    group: _FanoutObservation,
) -> dict[str, object]:
    """Build first-observation fields for an exact matching fanout group."""
    return {
        "job_id": job_id,
        "occurrence_at_us": occurrence_us,
        "job_version": job_version,
        "schedule_fingerprint": fingerprint,
        "status": "PRESENT",
        "first_checked_at_us": checked_at_us,
        "first_missing_at_us": None,
        "first_fanout_status": group.status,
        "first_manifest_sha256": group.manifest_sha256,
        "first_selection_evidence_status": GROUP_REFERENCE_ONLY,
        "first_selection_snapshot_ref": group.selection_snapshot_ref,
        "recovered_at_us": None,
        "recovery_fanout_status": None,
        "recovery_manifest_sha256": None,
        "recovery_group_selection_snapshot_ref": None,
        "recovery_evidence_status": None,
    }


def _slot_from_values(values: dict[str, object]) -> ReconciledSlot:
    """Convert validated inserted values to the public immutable slot record."""
    return ReconciledSlot(
        occurrence=_epoch_us_to_datetime(values["occurrence_at_us"], "occurrence_at_us"),
        status=_slot_status(values["status"]),
        first_checked_at=_epoch_us_to_datetime(
            values["first_checked_at_us"], "first_checked_at_us"
        ),
        first_missing_at=(
            _epoch_us_to_datetime(values["first_missing_at_us"], "first_missing_at_us")
            if values["first_missing_at_us"] is not None
            else None
        ),
        recovered_at=None,
        first_fanout_status=_optional_fanout_status(values["first_fanout_status"]),
        first_manifest_sha256=_optional_string(values["first_manifest_sha256"]),
        first_selection_evidence_status=_selection_evidence_status(
            values["first_selection_evidence_status"]
        ),
        first_selection_snapshot_ref=_optional_string(values["first_selection_snapshot_ref"]),
        recovery_fanout_status=None,
        recovery_manifest_sha256=None,
        recovery_group_selection_snapshot_ref=None,
        recovery_evidence_status=None,
    )


def _fanout_observation_from_row(
    row: Mapping[str, object] | RowMapping, *, expected_job_version: str
) -> _FanoutObservation:
    """Validate an observed fanout group without claiming its work completed."""
    job_id = row["job_id"]
    job_version = row["job_version"]
    occurrence_us = _integer(row["occurrence_at_us"], "fanout occurrence_at_us")
    occurrence_text = _format_occurrence(_epoch_us_to_datetime(occurrence_us, "fanout occurrence"))
    status = _fanout_status(row["status"])
    fanout_key = row["fanout_key"]
    manifest_json = row["manifest_json"]
    stored_hash = row["manifest_sha256"]
    if job_id != SINA_FZB_RECONCILIATION_JOB_ID:
        raise ReconciliationJournalCorruptionError("fanout group job identity is invalid")
    if not isinstance(job_version, str) or job_version != expected_job_version:
        raise ReconciliationScheduleConflictError(
            "fanout group job version differs from the frozen reconciliation generation"
        )
    if (
        not isinstance(fanout_key, str)
        or _SHA256_PATTERN.fullmatch(fanout_key) is None
        or fanout_key != _expected_fanout_key(job_id, occurrence_text)
    ):
        raise ReconciliationJournalCorruptionError(
            "fanout group natural key is invalid or inconsistent"
        )
    if not isinstance(manifest_json, str) or not isinstance(stored_hash, str):
        raise ReconciliationJournalCorruptionError("fanout group manifest evidence is invalid")
    try:
        _validate_sha256(stored_hash, "fanout manifest hash")
    except ValueError as exc:
        raise ReconciliationJournalCorruptionError(
            "fanout group manifest digest is invalid"
        ) from exc
    try:
        encoded = manifest_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReconciliationJournalCorruptionError(
            "fanout group manifest is not valid UTF-8"
        ) from exc
    if hashlib.sha256(encoded).hexdigest() != stored_hash:
        raise ReconciliationJournalCorruptionError("fanout group manifest digest does not match")
    payload = _parse_manifest(manifest_json)
    if (
        set(payload) != _FANOUT_MANIFEST_KEYS
        or payload.get("contract_version") != 1
        or type(payload.get("contract_version")) is not int
        or payload.get("job_id") != job_id
        or payload.get("job_version") != job_version
        or payload.get("occurrence") != occurrence_text
    ):
        raise ReconciliationJournalCorruptionError("fanout manifest identity is inconsistent")
    reference = payload.get("selection_snapshot_ref")
    if not _is_opaque_reference(reference) or not _is_opaque_reference(payload.get("manifest_id")):
        raise ReconciliationJournalCorruptionError("fanout selection reference is invalid")
    if _canonical_json(payload) != manifest_json:
        raise ReconciliationJournalCorruptionError("fanout manifest is not canonical JSON")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20_000:
        raise ReconciliationJournalCorruptionError("fanout manifest symbol set is invalid")
    typed_symbols: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ReconciliationJournalCorruptionError("fanout manifest symbol set is invalid")
        typed_symbols.append(symbol)
    if typed_symbols != sorted(set(typed_symbols)):
        raise ReconciliationJournalCorruptionError("fanout manifest symbol set is invalid")
    return _FanoutObservation(
        occurrence_at_us=occurrence_us,
        status=status,
        manifest_sha256=stored_hash,
        selection_snapshot_ref=reference,
    )


def _expected_fanout_key(job_id: str, occurrence: str) -> str:
    """Recompute the fanout journal's canonical hash of its natural job-slot key."""
    canonical_slot = json.dumps(
        {"contract_version": 1, "job_id": job_id, "occurrence": occurrence},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_slot.encode("utf-8")).hexdigest()


def _parse_manifest(manifest_json: str) -> dict[str, object]:
    """Parse a bounded canonical fanout snapshot with duplicate-key rejection."""
    try:
        encoded = manifest_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReconciliationJournalCorruptionError("fanout manifest is not valid UTF-8") from exc
    if len(encoded) > 2_097_152:
        raise ReconciliationJournalCorruptionError("fanout manifest exceeds its byte limit")
    try:
        payload = json.loads(
            manifest_json,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ReconciliationJournalCorruptionError("fanout manifest JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ReconciliationJournalCorruptionError("fanout manifest must be a JSON object")
    return payload


def _is_opaque_reference(value: object) -> TypeGuard[str]:
    """Validate a bounded printable reference without interpreting its meaning."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_SELECTION_REFERENCE_LENGTH
        and all(ord(character) >= 33 and ord(character) != 127 for character in value)
    )


def _cursor_from_row(row: RowMapping) -> ReconciliationCursor:
    """Validate persisted singleton state before exposing its cursor."""
    try:
        job_id = row["job_id"]
        job_version = row["job_version"]
        snapshot_json = row["schedule_snapshot_json"]
        fingerprint = row["schedule_fingerprint"]
        if not isinstance(job_id, str) or not isinstance(job_version, str):
            raise ValueError("invalid job identity")
        _validate_job_id(job_id)
        _validate_identifier(job_version, "job_version", MAX_JOB_VERSION_LENGTH)
        if not isinstance(snapshot_json, str) or not isinstance(fingerprint, str):
            raise ValueError("invalid frozen schedule fields")
        _validate_schedule_snapshot(
            snapshot_json,
            job_id=job_id,
            job_version=job_version,
            fingerprint=fingerprint,
        )
        if row["calendar_policy"] != CRON_ONLY:
            raise ValueError("invalid calendar policy")
        if row["calendar_id"] is not None or row["calendar_version"] is not None:
            raise ValueError("unexpected calendar identity")
        if row["calendar_sha256"] is not None:
            raise ValueError("unexpected calendar hash")
        grace_seconds = _integer(row["grace_seconds"], "grace_seconds")
        if grace_seconds != RECONCILIATION_GRACE_SECONDS:
            raise ValueError("invalid grace interval")
        if row["earlier_history_status"] != NOT_ASSESSED:
            raise ValueError("invalid earlier history status")
        basis = row["initial_from_basis"]
        if not isinstance(basis, str):
            raise ValueError("invalid initial boundary basis")
        _validate_initial_from_basis(basis)
        initial_from_us = _integer(row["initial_from_us"], "initial_from_us")
        watermark_us = _integer(row["reconciled_through_us"], "reconciled_through_us")
        if watermark_us < initial_from_us:
            raise ValueError("watermark is before initial boundary")
        revision = _integer(row["cursor_revision"], "cursor_revision")
        if revision < 0:
            raise ValueError("negative cursor revision")
        status = _cursor_status(row["status"])
        return ReconciliationCursor(
            job_id=job_id,
            job_version=job_version,
            schedule_fingerprint=fingerprint,
            schedule_snapshot_json=snapshot_json,
            calendar_policy=CRON_ONLY,
            grace_seconds=grace_seconds,
            initial_from_basis=basis,
            initial_from=_epoch_us_to_datetime(initial_from_us, "initial_from_us"),
            reconciled_through=_epoch_us_to_datetime(watermark_us, "reconciled_through_us"),
            cursor_revision=revision,
            earlier_history_status=NOT_ASSESSED,
            status=status,
            created_at=_required_persisted_datetime(row["created_at_us"], "created_at_us"),
            updated_at=_required_persisted_datetime(row["updated_at_us"], "updated_at_us"),
            last_checked_at=_required_persisted_datetime(
                row["last_checked_at_us"], "last_checked_at_us"
            ),
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise ReconciliationJournalCorruptionError(
            "persisted reconciliation cursor is invalid"
        ) from exc


def _same_initial_contract(
    cursor: ReconciliationCursor,
    *,
    job_version: str,
    snapshot_json: str,
    fingerprint: str,
    initial_from: int,
    initial_from_basis: str,
) -> bool:
    """Compare an idempotent initialization replay to all frozen fields."""
    return (
        cursor.job_version == job_version
        and cursor.schedule_snapshot_json == snapshot_json
        and cursor.schedule_fingerprint == fingerprint
        and _datetime_to_epoch_us(cursor.initial_from, "initial_from") == initial_from
        and cursor.initial_from_basis == initial_from_basis
    )


def _datetime_to_epoch_us(value: datetime, field: str) -> int:
    """Validate an aware UTC datetime and convert it to exact microseconds."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    delta = value.astimezone(_UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _epoch_us_to_datetime(value: object, field: str) -> datetime:
    """Convert a validated persisted integer to an aware UTC datetime."""
    timestamp_us = _integer(value, field)
    try:
        return _EPOCH + timedelta(microseconds=timestamp_us)
    except OverflowError as exc:
        raise ReconciliationJournalCorruptionError(
            f"persisted {field} is outside the datetime range"
        ) from exc


def _persisted_datetime(value: object, field: str) -> datetime | None:
    """Convert nullable persisted time while translating bad data to caller."""
    if value is None:
        return None
    return _epoch_us_to_datetime(value, field)


def _required_persisted_datetime(value: object, field: str) -> datetime:
    """Convert a required persisted timestamp without a nullable result."""
    converted = _persisted_datetime(value, field)
    if converted is None:
        raise ReconciliationJournalCorruptionError(f"persisted {field} is unexpectedly null")
    return converted


def _format_occurrence(value: datetime) -> str:
    """Format an aware UTC occurrence as the fanout contract's fixed ISO text."""
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    """Serialize canonical JSON under the frozen UTF-8 contract."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise ReconciliationJournalCorruptionError(
            "persisted JSON cannot be canonicalized"
        ) from exc


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys in persisted or incoming contract JSON."""
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON object key")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    """Reject non-standard JSON values such as NaN and Infinity."""
    raise ValueError(f"invalid JSON constant: {value}")


def _integer(value: object, field: str) -> int:
    """Read an exact integer field, rejecting booleans and driver coercions."""
    if type(value) is not int:
        raise ReconciliationJournalCorruptionError(f"persisted {field} is invalid")
    return value


def _validate_revision(value: int) -> None:
    """Validate an exact non-negative cursor revision."""
    if type(value) is not int or not 0 <= value < 9_223_372_036_854_775_807:
        raise ValueError("expected_cursor_revision must be a bounded non-negative integer")


def _slot_status(value: object) -> SlotStatus:
    """Narrow one persisted or newly inserted slot status."""
    if value == "PRESENT":
        return "PRESENT"
    if value == "MISSING":
        return "MISSING"
    if value == "RECOVERED":
        return "RECOVERED"
    raise ReconciliationJournalCorruptionError("reconciled slot status is invalid")


def _cursor_status(value: object) -> CursorStatus:
    """Narrow one persisted cursor state."""
    if value == "ACTIVE":
        return "ACTIVE"
    if value == "BLOCKED":
        return "BLOCKED"
    raise ValueError("invalid cursor status")


def _fanout_status(value: object) -> FanoutStatus:
    """Narrow a validated fanout group status."""
    if value == "PENDING":
        return "PENDING"
    if value == "ENQUEUED":
        return "ENQUEUED"
    if value == "BLOCKED":
        return "BLOCKED"
    raise ReconciliationJournalCorruptionError("fanout group status is invalid")


def _optional_fanout_status(value: object) -> FanoutStatus | None:
    """Validate a nullable fanout state stored in a new slot record."""
    return None if value is None else _fanout_status(value)


def _optional_string(value: object) -> str | None:
    """Validate a nullable string in a locally constructed record."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReconciliationJournalCorruptionError("reconciled slot text field is invalid")
    return value


def _selection_evidence_status(value: object) -> SelectionEvidenceStatus:
    """Narrow selection provenance while distinguishing UNKNOWN from a group ref."""
    if value == UNKNOWN:
        return UNKNOWN
    if value == GROUP_REFERENCE_ONLY:
        return GROUP_REFERENCE_ONLY
    raise ReconciliationJournalCorruptionError("reconciled selection evidence status is invalid")


def _begin_sqlite_write(connection: Connection) -> None:
    """Reserve SQLite's single writer before reading the cursor for CAS."""
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _database_clock_us(connection: Connection) -> int:
    """Read database UTC time on the same transaction connection."""
    dialect_name = connection.dialect.name
    if dialect_name == "mysql":
        value = connection.exec_driver_sql("SELECT UTC_TIMESTAMP(6)").scalar_one()
    elif dialect_name == "postgresql":
        value = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    elif dialect_name == "sqlite":
        value = connection.exec_driver_sql(
            "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')"
        ).scalar_one()
        if not isinstance(value, str):
            raise RuntimeError("SQLite database clock returned a non-text timestamp")
        try:
            value = datetime.fromisoformat(value).replace(tzinfo=_UTC)
        except ValueError as exc:
            raise RuntimeError("SQLite database clock returned an invalid timestamp") from exc
    else:
        raise RuntimeError(f"database clock is unsupported for {dialect_name!r}")
    if not isinstance(value, datetime):
        raise RuntimeError(f"{dialect_name} database clock returned an invalid timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return _datetime_to_epoch_us(value.astimezone(_UTC), "database clock")


__all__ = [
    "CRON_ONLY",
    "GROUP_REFERENCE_ONLY",
    "LATE_GROUP_ONLY",
    "MAX_RECOVERY_SLOTS_PER_BATCH",
    "MAX_SLOTS_PER_BATCH",
    "NOT_ASSESSED",
    "RECONCILIATION_GRACE_SECONDS",
    "ReconciledSlot",
    "ReconciliationBatch",
    "ReconciliationCASConflictError",
    "ReconciliationContractConflictError",
    "ReconciliationCursor",
    "ReconciliationJournalCorruptionError",
    "ReconciliationScheduleConflictError",
    "SinaFzbSlotReconciliationJournal",
    "SINA_FZB_RECONCILIATION_JOB_ID",
    "SINA_FZB_RECONCILED_SLOT_TABLE",
    "SINA_FZB_SLOT_RECONCILIATION_METADATA",
    "SINA_FZB_SLOT_RECONCILIATION_STATE_TABLE",
]
