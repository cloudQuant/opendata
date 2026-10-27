"""Offline behavior tests for UTC Sina FZB missing-slot reconciliation."""

from __future__ import annotations

import builtins
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, cast

import pytest

import opendata.pipeline.sina_fzb_slot_reconciliation as reconciliation_module
from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
from opendata.pipeline.sina_fzb_slot_reconciliation import (
    MAX_SLOTS_PER_BATCH,
    SLOT_GRACE_SECONDS,
    SinaFzbSlotReconciler,
    UnsupportedSinaFzbScheduleError,
)
from opendata.pipeline.sina_fzb_slot_reconciliation_journal import (
    ReconciliationBatch,
    ReconciliationCursor,
)

UTC = timezone.utc
INITIAL_FROM = datetime(2026, 10, 26, 7, 59, tzinfo=UTC)
INITIAL_FROM_BASIS = "operator-selected-pilot-boundary"

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeReconciliationJournal:
    """Capture the journal contract without opening a database connection."""

    def __init__(
        self,
        *,
        reconciled_through: datetime | None = None,
        database_clock: datetime | None = None,
    ) -> None:
        """Build a cursor response and an empty recorded batch list."""
        self.reconciled_through = reconciled_through
        self.database_clock = database_clock
        self.database_now_calls = 0
        self.initialize_calls: list[dict[str, object]] = []
        self.reconcile_calls: list[dict[str, object]] = []
        self.cursor: ReconciliationCursor | None = None
        self.initialize_cursor_transform: (
            Callable[[ReconciliationCursor], ReconciliationCursor] | None
        ) = None
        self.batch_cursor_transform: (
            Callable[[ReconciliationCursor], ReconciliationCursor] | None
        ) = None

    def database_now(self) -> datetime:
        """Return a fixture clock without opening a database connection."""
        self.database_now_calls += 1
        if self.database_clock is None:
            raise AssertionError("explicit clock expected in this test")
        return self.database_clock

    def initialize(
        self,
        *,
        job_id: str,
        job_version: str,
        schedule_snapshot_json: str,
        schedule_fingerprint: str,
        initial_from: datetime,
        initial_from_basis: str,
        now: datetime,
    ) -> ReconciliationCursor:
        """Return a cursor matching the exact initialization contract."""
        self.initialize_calls.append(
            {
                "job_id": job_id,
                "job_version": job_version,
                "schedule_snapshot_json": schedule_snapshot_json,
                "schedule_fingerprint": schedule_fingerprint,
                "initial_from": initial_from,
                "initial_from_basis": initial_from_basis,
                "now": now,
            }
        )
        if self.cursor is not None:
            if (
                self.cursor.job_id != job_id
                or self.cursor.job_version != job_version
                or self.cursor.schedule_snapshot_json != schedule_snapshot_json
                or self.cursor.schedule_fingerprint != schedule_fingerprint
                or self.cursor.initial_from != initial_from
                or self.cursor.initial_from_basis != initial_from_basis
            ):
                raise ValueError("existing fake journal contract conflicts")
            return self.cursor
        if initial_from > now:
            raise ValueError("initial_from must not be later than first initialization time")
        initial_watermark = (
            initial_from if self.reconciled_through is None else self.reconciled_through
        )
        self.cursor = ReconciliationCursor(
            job_id=job_id,
            job_version=job_version,
            schedule_fingerprint=schedule_fingerprint,
            schedule_snapshot_json=schedule_snapshot_json,
            calendar_policy="cron_only",
            grace_seconds=SLOT_GRACE_SECONDS,
            initial_from_basis=initial_from_basis,
            initial_from=initial_from,
            reconciled_through=initial_watermark,
            cursor_revision=7,
            earlier_history_status="NOT_ASSESSED",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
            last_checked_at=now,
        )
        if self.initialize_cursor_transform is not None:
            self.cursor = self.initialize_cursor_transform(self.cursor)
        return self.cursor

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
        """Record a bounded journal call without performing persistence."""
        self.reconcile_calls.append(
            {
                "job_id": job_id,
                "job_version": job_version,
                "expected_schedule_fingerprint": expected_schedule_fingerprint,
                "expected_cursor_revision": expected_cursor_revision,
                "expected_from": expected_from,
                "checked_at": checked_at,
                "due_through": due_through,
                "new_cursor": new_cursor,
                "expected_occurrences": expected_occurrences,
            }
        )
        if self.cursor is None:
            raise AssertionError("initialize() must run before reconcile_batch()")
        self.cursor = ReconciliationCursor(
            job_id=job_id,
            job_version=job_version,
            schedule_fingerprint=expected_schedule_fingerprint,
            schedule_snapshot_json=self.cursor.schedule_snapshot_json,
            calendar_policy="cron_only",
            grace_seconds=SLOT_GRACE_SECONDS,
            initial_from_basis=self.cursor.initial_from_basis,
            initial_from=self.cursor.initial_from,
            reconciled_through=new_cursor,
            cursor_revision=expected_cursor_revision + 1,
            earlier_history_status="NOT_ASSESSED",
            status="ACTIVE",
            created_at=self.cursor.created_at,
            updated_at=checked_at,
            last_checked_at=checked_at,
        )
        if self.batch_cursor_transform is not None:
            self.cursor = self.batch_cursor_transform(self.cursor)
        return ReconciliationBatch(
            cursor=self.cursor,
            slots=(),
            recovered_slots=(),
        )


def _task(
    *,
    expression: str = "30 8 * * *",
    timezone_name: str = "UTC",
    calendar: str | None = None,
    job_id: str = "sina-fzb-capture",
    version: str = "process-v1",
    kind: Literal["cron", "interval", "date"] | None = "cron",
) -> TaskDefinition:
    """Build an inert task definition with a caller-selected schedule field."""
    return TaskDefinition(
        job_id=job_id,
        version=version,
        dataset_id="sina-fzb-observations",
        provider="sina",
        model="financial-statement",
        timezone=timezone_name,
        calendar=calendar,
        schedule=(None if kind is None else ScheduleMetadata(kind=kind, expression=expression)),
        enabled=False,
    )


def _reconciler(
    journal: FakeReconciliationJournal,
    *,
    task: TaskDefinition | None = None,
    initial_from: datetime = INITIAL_FROM,
    clock: Callable[[], datetime] | None = None,
) -> SinaFzbSlotReconciler:
    """Build the inert reconciler with an explicit declared lower boundary."""
    return SinaFzbSlotReconciler(
        journal,
        task or _task(),
        initial_from=initial_from,
        initial_from_basis=INITIAL_FROM_BASIS,
        clock=clock,
    )


def _last_call(journal: FakeReconciliationJournal) -> dict[str, object]:
    """Return the most recent captured journal batch call."""
    assert journal.reconcile_calls
    return journal.reconcile_calls[-1]


def test_schedule_snapshot_and_fingerprint_match_the_v1_golden_vector() -> None:
    """Freeze the canonical cron-only contract and its fingerprint bytes."""
    reconciler = _reconciler(FakeReconciliationJournal())
    expected_snapshot = (
        '{"calendar_policy":"cron_only","contract_version":1,'
        '"crontab":"30 8 * * *","grace_seconds":120,'
        '"job_id":"sina-fzb-capture","job_version":"process-v1",'
        '"max_recovery_slots_per_batch":128,"max_slots_per_batch":128,'
        '"schedule_kind":"cron","selection_policy":"unknown_if_no_slot_evidence",'
        '"timezone":"UTC"}'
    )

    assert reconciler.schedule_snapshot_json == expected_snapshot
    assert reconciler.schedule_fingerprint == (
        "adddf9bed2140737c5d1451c59b6961480019567431aa6263a213e083af39a42"
    )


@pytest.mark.parametrize(
    "task",
    [
        _task(timezone_name="America/New_York"),
        _task(calendar="shanghai-exchange"),
        _task(kind="interval"),
        _task(job_id="other-job"),
        _task(expression="30 8 * * 1"),
    ],
)
def test_unsupported_schedule_contract_fails_before_journal_use(task: TaskDefinition) -> None:
    """Reject DST, trading calendars, unsupported jobs, and ambiguous cron."""
    journal = FakeReconciliationJournal()

    with pytest.raises(UnsupportedSinaFzbScheduleError):
        _reconciler(journal, task=task)

    assert journal.initialize_calls == []
    assert journal.reconcile_calls == []


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("30 8 * *", "five fields"),
        ("30 8 * * foo", "weekday cron field"),
        ("61 8 * * *", "invalid APScheduler cron expression"),
    ],
)
def test_malformed_cron_contract_fails_closed(expression: str, message: str) -> None:
    """Reject malformed field counts, weekdays, and impossible field values."""
    with pytest.raises(UnsupportedSinaFzbScheduleError, match=message):
        _reconciler(FakeReconciliationJournal(), task=_task(expression=expression))


def test_missing_schedule_is_rejected_by_task_contract() -> None:
    """A schedule-less task cannot reach cron snapshot construction."""
    with pytest.raises(UnsupportedSinaFzbScheduleError, match="explicit cron"):
        _reconciler(FakeReconciliationJournal(), task=_task(kind=None))


@pytest.mark.parametrize(
    "initial_from",
    [
        datetime(2026, 10, 26, 7, 59),
        datetime(2026, 10, 26, 7, 59, tzinfo=timezone(timedelta(hours=8))),
        "not-a-datetime",
    ],
)
def test_initial_boundary_must_be_an_aware_utc_datetime(initial_from: object) -> None:
    """Reject ambiguous or non-datetime initial lower boundaries at construction."""
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        SinaFzbSlotReconciler(
            FakeReconciliationJournal(),
            _task(),
            initial_from=cast("datetime", initial_from),
            initial_from_basis=INITIAL_FROM_BASIS,
        )


def test_initial_boundary_basis_accepts_its_maximum_supported_identifier() -> None:
    """The 255-character boundary reference is accepted at the inclusive edge."""
    reconciler = SinaFzbSlotReconciler(
        FakeReconciliationJournal(),
        _task(),
        initial_from=INITIAL_FROM,
        initial_from_basis="a" * 255,
    )

    assert reconciler.schedule_snapshot_json


def test_non_string_initial_boundary_basis_is_rejected() -> None:
    """Reject non-string boundary metadata before persistence is attempted."""
    with pytest.raises(ValueError, match="must be a string"):
        SinaFzbSlotReconciler(
            FakeReconciliationJournal(),
            _task(),
            initial_from=INITIAL_FROM,
            initial_from_basis=None,
        )


def test_constructor_rejects_incomplete_journal_contracts() -> None:
    """Require typed task, callable clock, and DB clock for default operation."""
    with pytest.raises(TypeError, match="journal must implement"):
        SinaFzbSlotReconciler(
            object(),
            _task(),
            initial_from=INITIAL_FROM,
            initial_from_basis=INITIAL_FROM_BASIS,
            clock=lambda: INITIAL_FROM,
        )

    journal = FakeReconciliationJournal()
    with pytest.raises(TypeError, match="TaskDefinition"):
        SinaFzbSlotReconciler(
            journal,
            object(),
            initial_from=INITIAL_FROM,
            initial_from_basis=INITIAL_FROM_BASIS,
            clock=lambda: INITIAL_FROM,
        )

    with pytest.raises(TypeError, match="clock must be callable"):
        SinaFzbSlotReconciler(
            journal,
            _task(),
            initial_from=INITIAL_FROM,
            initial_from_basis=INITIAL_FROM_BASIS,
            clock=object(),
        )

    class JournalWithoutDatabaseClock:
        def initialize(self, **kwargs: object) -> None:
            raise AssertionError(kwargs)

        def reconcile_batch(self, **kwargs: object) -> None:
            raise AssertionError(kwargs)

    with pytest.raises(TypeError, match="database_now"):
        SinaFzbSlotReconciler(
            JournalWithoutDatabaseClock(),
            _task(),
            initial_from=INITIAL_FROM,
            initial_from_basis=INITIAL_FROM_BASIS,
        )


def test_schedule_snapshot_guard_rejects_contract_key_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch accidental schedule snapshot shape edits without a version bump."""
    monkeypatch.setattr(reconciliation_module, "_SCHEDULE_SNAPSHOT_KEYS", frozenset())

    with pytest.raises(RuntimeError, match="contract version update"):
        _reconciler(FakeReconciliationJournal())


def test_snapshot_serializer_rejects_a_missing_schedule() -> None:
    """Keep the private canonical serializer defensive when used directly."""
    with pytest.raises(UnsupportedSinaFzbScheduleError, match="explicit cron"):
        reconciliation_module._canonical_schedule_snapshot(_task(kind=None))


def test_snapshot_contract_detects_cron_expression_drift() -> None:
    """The exact cron expression is included in the immutable fingerprint."""
    daily = _reconciler(FakeReconciliationJournal(), task=_task(expression="30 8 * * *"))
    hourly = _reconciler(FakeReconciliationJournal(), task=_task(expression="30 * * * *"))

    assert daily.schedule_fingerprint != hourly.schedule_fingerprint
    assert '"crontab":"30 * * * *"' in hourly.schedule_snapshot_json


def test_enumerator_rejects_nonpositive_limit_and_skips_empty_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate the bound and avoid constructing a trigger for an empty range."""
    instant = INITIAL_FROM

    def unexpected_trigger(expression: str) -> object:
        raise AssertionError(expression)

    monkeypatch.setattr(reconciliation_module, "_cron_trigger", unexpected_trigger)
    with pytest.raises(ValueError, match="limit must be positive"):
        reconciliation_module._enumerate_occurrences("* * * * *", instant, instant, limit=0)
    assert (
        reconciliation_module._enumerate_occurrences("* * * * *", instant, instant, limit=1) == []
    )
    assert (
        reconciliation_module._enumerate_occurrences(
            "* * * * *", instant + timedelta(seconds=1), instant, limit=1
        )
        == []
    )


def test_enumerator_rejects_a_cron_trigger_that_repeats_the_lower_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if a trigger violates the exclusive lower-bound contract."""
    instant = INITIAL_FROM

    class RepeatingTrigger:
        def get_next_fire_time(
            self,
            previous_fire_time: datetime | None,
            now: datetime,
        ) -> datetime:
            return instant

    monkeypatch.setattr(
        reconciliation_module, "_cron_trigger", lambda expression: RepeatingTrigger()
    )
    with pytest.raises(RuntimeError, match="exclusive window"):
        reconciliation_module._enumerate_occurrences(
            "* * * * *", instant, instant + timedelta(minutes=1), limit=1
        )


def test_reconciler_advances_empty_safe_window_when_trigger_has_no_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid but nonmatching cron window advances without inventing slots."""
    checked_at = datetime(2026, 10, 27, 8, 32, tzinfo=UTC)
    journal = FakeReconciliationJournal()
    reconciler = _reconciler(journal, clock=lambda: checked_at)

    class NoOccurrenceTrigger:
        def get_next_fire_time(
            self,
            previous_fire_time: datetime | None,
            now: datetime,
        ) -> None:
            return None

    monkeypatch.setattr(
        reconciliation_module,
        "_cron_trigger",
        lambda expression: NoOccurrenceTrigger(),
    )
    result = reconciler.reconcile()

    assert result.slots == ()
    assert _last_call(journal)["expected_occurrences"] == ()
    assert result.cursor.reconciled_through == checked_at - timedelta(seconds=SLOT_GRACE_SECONDS)


def test_enumerator_rejects_a_non_utc_cron_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cron results are checked independently of the reconciliation clock."""
    instant = INITIAL_FROM

    class LocalTimeTrigger:
        def get_next_fire_time(
            self,
            previous_fire_time: datetime | None,
            now: datetime,
        ) -> datetime:
            return instant.astimezone(timezone(timedelta(hours=8)))

    monkeypatch.setattr(
        reconciliation_module, "_cron_trigger", lambda expression: LocalTimeTrigger()
    )
    with pytest.raises(ValueError, match="cron occurrence.*timezone-aware UTC"):
        reconciliation_module._enumerate_occurrences(
            "* * * * *", instant - timedelta(minutes=1), instant + timedelta(minutes=1), limit=1
        )


def test_cron_trigger_has_a_clear_error_when_apscheduler_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the optional scheduler dependency failure actionable."""
    original_import = builtins.__import__

    def reject_apscheduler(name: str, *args: object, **kwargs: object) -> object:
        if name == "apscheduler.triggers.cron":
            raise ImportError("simulated missing dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_apscheduler)
    with pytest.raises(RuntimeError, match="APScheduler is required"):
        reconciliation_module._cron_trigger("* * * * *")


@pytest.mark.parametrize(
    ("field", "value", "exception", "message"),
    [
        ("job_id", "other-job", RuntimeError, "requested job version"),
        ("job_version", "other-version", RuntimeError, "requested job version"),
        ("schedule_fingerprint", "0" * 64, RuntimeError, "fingerprint does not match"),
        ("schedule_snapshot_json", "{}", RuntimeError, "snapshot does not match"),
        ("calendar_policy", "weekday", RuntimeError, "cron-only calendar policy"),
        ("status", "BLOCKED", RuntimeError, "cursor is not active"),
        (
            "initial_from",
            INITIAL_FROM + timedelta(seconds=1),
            RuntimeError,
            "initial boundary does not match",
        ),
        ("initial_from_basis", "different-boundary", RuntimeError, "basis does not match"),
        ("earlier_history_status", "ASSESSED", RuntimeError, "NOT_ASSESSED"),
        ("grace_seconds", 119, RuntimeError, "grace period does not match"),
        (
            "reconciled_through",
            datetime(2026, 10, 26, 8, 0),
            ValueError,
            "watermark.*timezone-aware UTC",
        ),
        ("cursor_revision", True, RuntimeError, "revision is invalid"),
        ("cursor_revision", -1, RuntimeError, "revision is invalid"),
        (
            "reconciled_through",
            INITIAL_FROM - timedelta(seconds=1),
            RuntimeError,
            "watermark precedes",
        ),
    ],
)
def test_initialized_cursor_contract_corruption_fails_closed(
    field: str,
    value: object,
    exception: type[Exception],
    message: str,
) -> None:
    """Reject journal cursors that disagree with the frozen service contract."""
    journal = FakeReconciliationJournal()
    journal.initialize_cursor_transform = lambda cursor: replace(cursor, **{field: value})

    with pytest.raises(exception, match=message):
        _reconciler(journal, clock=lambda: INITIAL_FROM + timedelta(minutes=1)).reconcile()

    assert journal.reconcile_calls == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "reconciled_through",
            INITIAL_FROM + timedelta(seconds=1),
            "unexpected reconciliation watermark",
        ),
        ("cursor_revision", 9, "advance the cursor revision exactly once"),
    ],
)
def test_reconciled_batch_cursor_must_match_the_committed_fence(
    field: str,
    value: object,
    message: str,
) -> None:
    """Reject a batch response that reports a different watermark or CAS revision."""
    journal = FakeReconciliationJournal()
    journal.batch_cursor_transform = lambda cursor: replace(cursor, **{field: value})

    with pytest.raises(RuntimeError, match=message):
        _reconciler(
            journal,
            clock=lambda: INITIAL_FROM + timedelta(minutes=1),
        ).reconcile()


@pytest.mark.parametrize("basis", ["", "not a basis", "bad\nreason", "x" * 256])
def test_initial_boundary_basis_is_required_bounded_and_single_line(basis: str) -> None:
    """Require a safe caller-declared reason for the unassessed history edge."""
    with pytest.raises(ValueError):
        SinaFzbSlotReconciler(
            FakeReconciliationJournal(),
            _task(),
            initial_from=INITIAL_FROM,
            initial_from_basis=basis,
        )


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 26, 8, 30),
        datetime(2026, 10, 26, 8, 30, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_reconcile_rejects_naive_or_non_utc_clock_before_journal_use(now: datetime) -> None:
    """Do not evaluate schedule windows from an ambiguous or local wall clock."""
    journal = FakeReconciliationJournal()

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _reconciler(journal, clock=lambda: now).reconcile()

    assert journal.initialize_calls == []
    assert journal.reconcile_calls == []


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 26, 8, 1, 59, tzinfo=UTC), ()),
        (
            datetime(2026, 10, 26, 8, 2, 0, tzinfo=UTC),
            (datetime(2026, 10, 26, 8, 0, tzinfo=UTC),),
        ),
        (
            datetime(2026, 10, 26, 8, 2, 1, tzinfo=UTC),
            (datetime(2026, 10, 26, 8, 0, tzinfo=UTC),),
        ),
    ],
)
def test_grace_boundary_is_exclusive_before_120_seconds_and_inclusive_after(
    now: datetime,
    expected: tuple[datetime, ...],
) -> None:
    """Only process occurrences at or before the UTC grace cutoff."""
    initial_from = datetime(2026, 10, 26, 7, 59, 59, tzinfo=UTC)
    journal = FakeReconciliationJournal(reconciled_through=initial_from)
    reconciler = _reconciler(
        journal,
        task=_task(expression="* * * * *"),
        initial_from=initial_from,
        clock=lambda: now,
    )

    reconciler.reconcile()

    call = _last_call(journal)
    assert call["due_through"] == now - timedelta(seconds=SLOT_GRACE_SECONDS)
    assert call["expected_occurrences"] == expected


@pytest.mark.parametrize(
    ("due_through", "expected_count", "expected_cursor"),
    [
        (
            datetime(2026, 10, 26, 2, 8, tzinfo=UTC),
            128,
            datetime(2026, 10, 26, 2, 8, tzinfo=UTC),
        ),
        (
            datetime(2026, 10, 26, 2, 9, tzinfo=UTC),
            128,
            datetime(2026, 10, 26, 2, 8, tzinfo=UTC),
        ),
    ],
)
def test_batch_limit_never_skips_unprocessed_occurrences(
    due_through: datetime,
    expected_count: int,
    expected_cursor: datetime,
) -> None:
    """Stop an oversized backlog at its last committed occurrence."""
    initial_from = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    now = due_through + timedelta(seconds=SLOT_GRACE_SECONDS)
    journal = FakeReconciliationJournal(reconciled_through=initial_from)
    reconciler = _reconciler(
        journal,
        task=_task(expression="* * * * *"),
        initial_from=initial_from,
        clock=lambda: now,
    )

    reconciler.reconcile()

    call = _last_call(journal)
    occurrences = call["expected_occurrences"]
    assert isinstance(occurrences, tuple)
    assert len(occurrences) == expected_count
    assert len(occurrences) <= MAX_SLOTS_PER_BATCH
    assert occurrences[0] == datetime(2026, 10, 26, 0, 1, tzinfo=UTC)
    assert occurrences[-1] == expected_cursor
    assert call["new_cursor"] == expected_cursor
    if due_through > expected_cursor:
        assert datetime(2026, 10, 26, 2, 9, tzinfo=UTC) not in occurrences


def test_completed_empty_window_advances_only_to_the_safe_boundary() -> None:
    """Advance an empty fully enumerated window without inventing a slot."""
    initial_from = datetime(2026, 10, 26, 8, 31, tzinfo=UTC)
    now = datetime(2026, 10, 27, 8, 29, tzinfo=UTC)
    journal = FakeReconciliationJournal(reconciled_through=initial_from)
    reconciler = _reconciler(
        journal,
        task=_task(expression="30 8 * * *"),
        initial_from=initial_from,
        clock=lambda: now,
    )

    reconciler.reconcile()

    call = _last_call(journal)
    assert call["expected_occurrences"] == ()
    assert call["new_cursor"] == now - timedelta(seconds=SLOT_GRACE_SECONDS)


def test_day_of_month_and_weekday_follow_apscheduler_and_semantics() -> None:
    """A first-of-month Monday cron fires only when both fields match."""
    initial_from = datetime(2026, 5, 31, 23, 59, tzinfo=UTC)
    checked_at = datetime(2026, 7, 1, 0, 2, tzinfo=UTC)
    journal = FakeReconciliationJournal(reconciled_through=initial_from)
    reconciler = _reconciler(
        journal,
        task=_task(expression="0 0 1 * mon"),
        initial_from=initial_from,
        clock=lambda: checked_at,
    )

    reconciler.reconcile()

    # In June 2026, only June 1 is both the first of the month and Monday.
    # Traditional cron OR semantics would also include June 8, 15, 22, and 29.
    assert _last_call(journal)["expected_occurrences"] == (datetime(2026, 6, 1, 0, 0, tzinfo=UTC),)


def test_clock_rewind_keeps_watermark_and_still_invokes_bounded_recovery() -> None:
    """A clock rewind never moves the cursor backward or skips late recovery."""
    initial_from = datetime(2026, 10, 26, 7, 50, tzinfo=UTC)
    first_check = datetime(2026, 10, 26, 8, 35, tzinfo=UTC)
    rewound_check = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    clock_values = iter((first_check, rewound_check))
    journal = FakeReconciliationJournal()
    reconciler = _reconciler(
        journal,
        initial_from=initial_from,
        clock=lambda: next(clock_values),
    )

    first = reconciler.reconcile()
    rewound = reconciler.reconcile()

    call = _last_call(journal)
    assert first.cursor.reconciled_through == datetime(2026, 10, 26, 8, 33, tzinfo=UTC)
    assert rewound.cursor.reconciled_through == first.cursor.reconciled_through
    assert call["expected_occurrences"] == ()
    assert call["checked_at"] == rewound_check
    assert call["due_through"] == rewound_check - timedelta(seconds=SLOT_GRACE_SECONDS)
    assert call["new_cursor"] == first.cursor.reconciled_through


def test_initialize_persists_explicit_not_assessed_boundary_basis() -> None:
    """Pass the caller's boundary basis and exact fingerprint to the journal."""
    journal = FakeReconciliationJournal()
    checked_at = datetime(2026, 10, 26, 8, 32, tzinfo=UTC)
    reconciler = _reconciler(journal, clock=lambda: checked_at)

    reconciler.reconcile()

    assert len(journal.initialize_calls) == 1
    call = journal.initialize_calls[0]
    assert call["initial_from"] == INITIAL_FROM
    assert call["initial_from_basis"] == INITIAL_FROM_BASIS
    assert call["schedule_fingerprint"] == reconciler.schedule_fingerprint
    assert call["schedule_snapshot_json"] == reconciler.schedule_snapshot_json


def test_first_initialization_rejects_a_future_lower_boundary() -> None:
    """Let the journal reject a boundary later than the first selected clock."""
    checked_at = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    initial_from = checked_at + timedelta(minutes=1)
    journal = FakeReconciliationJournal()

    with pytest.raises(ValueError, match="initial_from"):
        _reconciler(
            journal,
            initial_from=initial_from,
            clock=lambda: checked_at,
        ).reconcile()

    assert journal.initialize_calls[0]["now"] == checked_at
    assert journal.reconcile_calls == []


def test_exact_initialized_contract_can_be_replayed_after_clock_rollback() -> None:
    """A rollback does not turn an existing boundary into a new future one."""
    initial_clock = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    rolled_back_clock = initial_clock - timedelta(minutes=1)
    clock_values = iter((initial_clock, rolled_back_clock))
    journal = FakeReconciliationJournal()
    reconciler = _reconciler(
        journal,
        initial_from=initial_clock,
        clock=lambda: next(clock_values),
    )

    first_batch = reconciler.reconcile()
    second_batch = reconciler.reconcile()

    assert first_batch.cursor.reconciled_through == initial_clock
    assert second_batch.cursor.reconciled_through == initial_clock
    assert len(journal.initialize_calls) == 2
    assert journal.reconcile_calls[1]["checked_at"] == rolled_back_clock
    assert journal.reconcile_calls[1]["new_cursor"] == initial_clock


def test_default_clock_comes_from_the_journal_database_clock() -> None:
    """Use the database clock by default so host clock skew cannot widen grace."""
    checked_at = datetime(2026, 10, 26, 8, 32, tzinfo=UTC)
    journal = FakeReconciliationJournal(database_clock=checked_at)

    _reconciler(journal).reconcile()

    assert journal.database_now_calls == 1
    assert journal.initialize_calls[0]["now"] == checked_at
    assert _last_call(journal)["due_through"] == checked_at - timedelta(seconds=SLOT_GRACE_SECONDS)
