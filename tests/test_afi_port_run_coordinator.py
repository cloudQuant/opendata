"""Unit tests for the ported durable AkShare run coordinator claim plane.

These tests drive ``AkshareRunCoordinator`` against an isolated in-memory
SQLite database that only this file owns. SQLite accepts but ignores
``SELECT ... FOR UPDATE``, so the row-lock serialization itself (control
row locked before claim rows) is a MySQL-plane property verified by the
SQL shape in the code, not by these tests. Everything else exercised
here — atomic PK claim insertion with IntegrityError retry, fenced
conditional UPDATEs, lease expiry, retry backoff, source capacity and
gating, and read-only recovery — is portable SQL and fully verified.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import opendata.afi_port.runtime.run_coordinator as rc
from opendata.afi_port.runtime.run_coordinator import (
    COMPLETED,
    FAILED,
    RETRY_WAIT,
    RUNNING,
    AkshareRunCoordinator,
    RunLease,
    _as_naive_utc,
    _json_parameters,
    utcnow,
)
from opendata.core.config import get_settings
from opendata.core.database import Base
from opendata.models.data_script import DataScript
from opendata.models.run_claim import SchedulerControl, TaskRunClaim
from opendata.models.task import ScheduledTask, ScheduleType, TaskExecution, TaskStatus, TriggeredBy


@pytest.fixture
def engine_factory():
    def _make() -> Any:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        return engine

    return _make


async def _build(engine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


def _coordinator(
    sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    **settings_updates: Any,
) -> AkshareRunCoordinator:
    defaults: dict[str, Any] = {
        "akshare_scheduler_max_concurrent_tasks": 4,
        "akshare_scheduler_per_source_concurrent_tasks": 4,
        "akshare_scheduler_lease_seconds": 300,
        "akshare_scheduler_source_min_interval_seconds": 0.0,
    }
    defaults.update(settings_updates)
    settings = get_settings().model_copy(update=defaults)
    monkeypatch.setattr(rc, "get_settings", lambda: settings)
    coordinator = AkshareRunCoordinator()
    monkeypatch.setattr(
        rc.AkshareRunCoordinator,
        "_session_factory",
        staticmethod(lambda: sessions()),
    )
    return coordinator


def _claim_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_key": "k1",
        "task_id": None,
        "script_id": "script_a",
        "source": "akshare",
        "period_key": "cron:2026-11-05T00:45:00Z",
        "parameter_hash": "hash-a",
        "parameters": {"start_date": "19000101"},
        "max_attempts": 3,
    }
    base.update(overrides)
    return base


async def _reload(sessions, claim_key: str) -> TaskRunClaim | None:
    async with sessions() as session:
        return await session.get(TaskRunClaim, claim_key)


async def _expire_lease(sessions, claim_key: str) -> None:
    async with sessions() as session, session.begin():
        claim = await session.get(TaskRunClaim, claim_key)
        assert claim is not None
        claim.lease_expires_at = utcnow() - timedelta(seconds=1)


async def _force_blind_first_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the first locked claim read return None, simulating the PK race."""
    original_get = AsyncSession.get
    state = {"blind": True}

    async def racing_get(self, entity, ident, *args, **kwargs):
        if state["blind"] and entity is TaskRunClaim:
            state["blind"] = False
            return None
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", racing_get)


@pytest.fixture
async def sessions(engine_factory):
    engine = engine_factory()
    maker = await _build(engine)
    yield maker
    await engine.dispose()


async def test_acquire_new_claim_persists_fenced_running_row(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    outcome = await coordinator.try_claim(**_claim_kwargs())
    assert outcome.state == "acquired"
    assert outcome.lease is not None and outcome.lease.fencing_token == 1

    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == RUNNING
    assert claim.owner_id == outcome.lease.owner_id
    assert claim.execution_id is None
    for column in (claim.created_at, claim.updated_at, claim.lease_expires_at):
        assert column.tzinfo is None
    assert claim.lease_expires_at > utcnow()

    # The first acquisition creates exactly one shared control row.
    async with sessions() as session:
        controls = (await session.execute(select(SchedulerControl))).all()
    assert len(controls) == 1


async def test_completed_claim_is_never_reacquired(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert lease is not None
    assert await coordinator.complete(lease, "exec-1") is True

    outcome = await coordinator.try_claim(**_claim_kwargs())
    assert outcome.state == "completed"
    assert outcome.execution_id == "exec-1"
    assert outcome.lease is None


async def test_two_contenders_with_same_claim_key_yield_one_owner(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    first = await coordinator.try_claim(**_claim_kwargs(owner_id="owner-a"))

    # The second contender races the PK: its first locked read misses the
    # committed row (as it would between read and INSERT), the INSERT hits the
    # primary key, and the retry re-reads the committed winner.
    await _force_blind_first_read(monkeypatch)
    attempts = {"count": 0}
    original_once = coordinator._try_claim_once

    async def counted_once(**kwargs):
        attempts["count"] += 1
        return await original_once(**kwargs)

    monkeypatch.setattr(coordinator, "_try_claim_once", counted_once)
    second = await coordinator.try_claim(**_claim_kwargs(owner_id="owner-b"))

    assert attempts["count"] == 2
    assert first.lease is not None
    assert second.state == "in_progress"
    assert second.lease is None
    assert second.wait_seconds >= 0.25
    claim = await _reload(sessions, "k1")
    assert claim is not None and claim.owner_id == "owner-a"
    assert claim.fencing_token == 1 and claim.attempt_count == 1


async def test_second_pk_collision_reraises_integrity_error(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    await coordinator.try_claim(**_claim_kwargs())

    original_get = AsyncSession.get

    async def always_blind(self, entity, ident, *args, **kwargs):
        if entity is TaskRunClaim:
            return None
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", always_blind)
    with pytest.raises(IntegrityError):
        await coordinator.try_claim(**_claim_kwargs())


async def test_fencing_token_increases_across_reacquisitions(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    first = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert first is not None
    await coordinator.fail(first, "boom", None, utcnow() - timedelta(seconds=1))

    second = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    assert second.attempt_count == first.attempt_count + 1

    claim = await _reload(sessions, "k1")
    assert claim is not None and claim.fencing_token == 2


async def test_renew_extends_lease_without_touching_fence(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert lease is not None

    assert await coordinator.renew(lease, 3600) is True
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.fencing_token == lease.fencing_token
    assert claim.owner_id == lease.owner_id
    assert claim.lease_expires_at > lease.lease_expires_at

    stale = RunLease(
        claim_key=lease.claim_key,
        owner_id=lease.owner_id,
        fencing_token=lease.fencing_token + 1,
        lease_expires_at=lease.lease_expires_at,
        attempt_count=lease.attempt_count,
    )
    assert await coordinator.renew(stale, 3600) is False

    await _expire_lease(sessions, "k1")
    assert await coordinator.renew(lease, 3600) is False


async def test_complete_refuses_stale_token_and_commits_live_owner(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    stale = (await coordinator.try_claim(**_claim_kwargs(owner_id="owner-old"))).lease
    assert stale is not None
    await _expire_lease(sessions, "k1")
    live = (await coordinator.try_claim(**_claim_kwargs(owner_id="owner-new"))).lease
    assert live is not None

    assert await coordinator.complete(stale, "exec-stale") is False
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == RUNNING and claim.owner_id == "owner-new"
    assert claim.execution_id is None

    assert await coordinator.set_execution_id(stale, "exec-stale") is False
    assert await coordinator.set_execution_id(live, "exec-live") is True

    assert await coordinator.complete(live, "exec-live") is True
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == COMPLETED
    assert claim.execution_id == "exec-live"
    assert claim.lease_expires_at is None and claim.completed_at is not None


async def test_fail_persists_retry_backoff_or_terminal_failure(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert lease is not None

    retry_at = utcnow() + timedelta(seconds=120)
    assert await coordinator.fail(lease, "transient", "exec-1", retry_at) is True
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == RETRY_WAIT
    assert claim.next_retry_at is not None and claim.next_retry_at > utcnow()
    assert claim.lease_expires_at is None  # the run slot was released
    assert claim.last_error == "transient"

    outcome = await coordinator.try_claim(**_claim_kwargs())
    assert outcome.state == "retry_wait"
    assert outcome.wait_seconds >= 0.25

    # A stale fenced owner cannot fail the current row again.
    assert await coordinator.fail(lease, "late", None, None) is False

    fresh = (await coordinator.try_claim(**_claim_kwargs())).lease  # bumps token?
    # fresh is None: retry_wait is still in the future. Fail directly on the row.
    assert fresh is None
    await _expire_retry(sessions, "k1")
    reacquired = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert reacquired is not None
    assert await coordinator.fail(reacquired, "fatal", "exec-2", None) is True
    claim = await _reload(sessions, "k1")
    assert claim is not None and claim.status == FAILED

    blocked = await coordinator.try_claim(**_claim_kwargs())
    assert blocked.state == "failed"
    manual = await coordinator.try_claim(**_claim_kwargs(allow_manual_retry=True))
    assert manual.state == "acquired"


async def _expire_retry(sessions, claim_key: str) -> None:
    async with sessions() as session, session.begin():
        claim = await session.get(TaskRunClaim, claim_key)
        assert claim is not None
        claim.next_retry_at = utcnow() - timedelta(seconds=1)


async def test_exhausted_attempts_become_terminal_failed(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs(max_attempts=1))).lease
    assert lease is not None
    assert await coordinator.fail(lease, "one strike", None, utcnow() - timedelta(seconds=1))

    outcome = await coordinator.try_claim(**_claim_kwargs(max_attempts=1))
    assert outcome.state == "exhausted"
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == FAILED
    assert claim.next_retry_at is None and claim.lease_expires_at is None


async def test_cancel_revokes_even_a_just_completed_owner(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs())).lease
    assert lease is not None
    assert await coordinator.complete(lease, "exec-race") is True

    # Counterfactual: a non-owner lease cannot revoke.
    impostor = RunLease(
        claim_key=lease.claim_key,
        owner_id="someone-else",
        fencing_token=lease.fencing_token,
        lease_expires_at=lease.lease_expires_at,
        attempt_count=lease.attempt_count,
    )
    assert await coordinator.cancel(impostor, "not mine", None) is False

    assert await coordinator.cancel(lease, "user cancelled", "exec-race") is True
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == FAILED
    assert claim.completed_at is None
    assert claim.last_error == "user cancelled"


async def test_cancel_unreturned_claim_only_touches_that_owner(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    lease = (await coordinator.try_claim(**_claim_kwargs(owner_id="ghost-owner"))).lease
    assert lease is not None

    assert await coordinator.cancel_unreturned_claim("k1", "other-owner", "wrong") is False
    claim = await _reload(sessions, "k1")
    assert claim is not None and claim.status == RUNNING

    assert await coordinator.cancel_unreturned_claim("k1", "ghost-owner", "interrupted") is True
    claim = await _reload(sessions, "k1")
    assert claim is not None
    assert claim.status == FAILED
    assert claim.lease_expires_at is None and claim.next_retry_at is None

    # A completed claim is not an unreturned acquisition.
    reacquired = (
        await coordinator.try_claim(**_claim_kwargs(claim_key="k2", owner_id="owner-next"))
    ).lease
    assert reacquired is not None
    assert await coordinator.complete(reacquired, "exec-ok") is True
    assert await coordinator.cancel_unreturned_claim("k2", "owner-next", "late") is False


async def test_recoverable_claims_is_read_only_and_selects_lease_work(
    sessions, monkeypatch
) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    waiting = (
        await coordinator.try_claim(**_claim_kwargs(claim_key="c-wait", source="east"))
    ).lease
    assert waiting is not None
    await coordinator.fail(waiting, "retry later", None, utcnow() + timedelta(seconds=300))

    expired = (
        await coordinator.try_claim(**_claim_kwargs(claim_key="c-expired", source="sina"))
    ).lease
    assert expired is not None
    await _expire_lease(sessions, "c-expired")

    done = (await coordinator.try_claim(**_claim_kwargs(claim_key="c-done", source="x"))).lease
    assert done is not None
    await coordinator.complete(done, "exec-done")

    before = {
        "c-wait": await _reload(sessions, "c-wait"),
        "c-expired": await _reload(sessions, "c-expired"),
    }
    recovered = await coordinator.recoverable_claims()
    keys = {claim.claim_key for claim in recovered}
    assert keys == {"c-wait", "c-expired"}

    for key, snapshot in before.items():
        after = await _reload(sessions, key)
        assert after is not None and snapshot is not None
        assert after.status == snapshot.status
        assert after.owner_id == snapshot.owner_id
        assert after.next_retry_at == snapshot.next_retry_at


async def test_shared_capacity_bounds_concurrent_runs_per_source(sessions, monkeypatch) -> None:
    coordinator = _coordinator(
        sessions,
        monkeypatch,
        akshare_scheduler_max_concurrent_tasks=2,
        akshare_scheduler_per_source_concurrent_tasks=1,
    )
    sina = (await coordinator.try_claim(**_claim_kwargs(claim_key="s1", source="sina"))).lease
    assert sina is not None

    same_source = await coordinator.try_claim(**_claim_kwargs(claim_key="s2", source="sina"))
    assert same_source.state == "busy"
    assert same_source.wait_seconds >= 0.25

    other = (await coordinator.try_claim(**_claim_kwargs(claim_key="e1", source="east"))).lease
    assert other is not None

    global_full = await coordinator.try_claim(**_claim_kwargs(claim_key="x1", source="x"))
    assert global_full.state == "busy"

    await _expire_lease(sessions, "s1")
    assert (
        await coordinator.try_claim(**_claim_kwargs(claim_key="s2", source="sina"))
    ).state == "acquired"


async def test_source_cooldown_gate_is_honoured_via_control_row(sessions, monkeypatch) -> None:
    coordinator = _coordinator(
        sessions,
        monkeypatch,
        akshare_scheduler_source_min_interval_seconds=120,
    )
    first = (await coordinator.try_claim(**_claim_kwargs(claim_key="g1", source="gate"))).lease
    assert first is not None

    async with sessions() as session:
        control = await session.get(SchedulerControl, 1)
        assert control is not None
        assert "gate" in control.source_next_allowed_at

    blocked = await coordinator.try_claim(**_claim_kwargs(claim_key="g2", source="gate"))
    assert blocked.state == "busy"
    assert blocked.wait_seconds > 100

    # A different source is not gated by the same cooldown.
    assert (
        await coordinator.try_claim(**_claim_kwargs(claim_key="g3", source="other"))
    ).state == "acquired"


async def test_due_live_tasks_defer_backfill_claims(sessions, monkeypatch) -> None:
    coordinator = _coordinator(
        sessions,
        monkeypatch,
        akshare_scheduler_max_concurrent_tasks=1,
        akshare_scheduler_per_source_concurrent_tasks=1,
    )
    now = utcnow()
    async with sessions() as session, session.begin():
        session.add(
            DataScript(
                script_id="script_live",
                script_name="live",
                category="stocks",
                source="live",
                is_active=True,
            )
        )
        session.add(
            ScheduledTask(
                id=77,
                name="live task",
                user_id="u1",
                script_id="script_live",
                schedule_type=ScheduleType.DAILY,
                schedule_expression="09:00",
                parameters={},
                is_active=True,
                next_execution_at=now - timedelta(seconds=5),
            )
        )

    async with sessions() as session:
        counts = await AkshareRunCoordinator._due_scheduled_task_counts(session, now)
    assert counts == {"live": 1}

    # A backfill (task_id=None) defers while the live period is due and unclaimed.
    deferred = await coordinator.try_claim(
        **_claim_kwargs(claim_key="b1", source="live", defer_for_live_tasks=True)
    )
    assert deferred.state == "busy"
    assert deferred.reason == "live_task_due"

    # Once the live occurrence has been consumed by a scheduler execution,
    # the same backfill claim proceeds.
    async with sessions() as session, session.begin():
        session.add(
            TaskExecution(
                execution_id="exec-live-1",
                task_id=77,
                script_id="script_live",
                status=TaskStatus.COMPLETED,
                start_time=now,
                triggered_by=TriggeredBy.SCHEDULER,
            )
        )
    acquired = await coordinator.try_claim(
        **_claim_kwargs(claim_key="b1", source="live", defer_for_live_tasks=True)
    )
    assert acquired.state == "acquired"


async def test_manual_identity_does_not_consume_cron_claim_or_next_execution(
    sessions, monkeypatch
) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    now = utcnow()
    async with sessions() as session, session.begin():
        session.add(
            ScheduledTask(
                id=88,
                name="cron task",
                user_id="u1",
                script_id="script_b",
                schedule_type=ScheduleType.CRON,
                schedule_expression="45 0 * * *",
                parameters={},
                is_active=True,
                next_execution_at=now + timedelta(hours=8),
            )
        )
    expected_next = now + timedelta(hours=8)

    cron = (
        await coordinator.try_claim(
            **_claim_kwargs(
                claim_key="cron-k",
                task_id=88,
                period_key="cron:2026-11-05T00:45:00Z",
            )
        )
    ).lease
    assert cron is not None

    # Manual/direct identity per the invariant: day:<Shanghai date>.
    manual = await coordinator.try_claim(
        **_claim_kwargs(
            claim_key="day-k",
            task_id=88,
            source="akshare",
            period_key="day:2026-11-06",
        )
    )
    assert manual.state == "acquired"
    assert manual.lease is not None
    assert manual.lease.claim_key != cron.claim_key

    cron_claim = await _reload(sessions, "cron-k")
    assert cron_claim is not None and cron_claim.status == RUNNING
    assert cron_claim.owner_id == cron.owner_id

    assert await coordinator.complete(manual.lease, "exec-manual") is True

    async with sessions() as session:
        task = await session.get(ScheduledTask, 88)
        assert task is not None
        assert task.next_execution_at == expected_next


async def test_direct_run_parameters_are_json_safe_and_recoverable(sessions, monkeypatch) -> None:
    coordinator = _coordinator(sessions, monkeypatch)
    raw_parameters: dict[str, Any] = {
        "start_date": datetime(2026, 1, 1),
        "codes": ["510300", 600000],
        "nested": {"flag": True},
    }
    lease = (
        await coordinator.try_claim(
            **_claim_kwargs(period_key="day:2026-11-06", parameters=raw_parameters)
        )
    ).lease
    assert lease is not None

    # "Restart": a brand-new coordinator and session must read the claim back.
    restarted = _coordinator(sessions, monkeypatch)
    claim = await restarted.get_claim("k1")
    assert claim is not None
    assert claim.parameters == {
        "start_date": "2026-01-01 00:00:00",
        "codes": ["510300", 600000],
        "nested": {"flag": True},
    }

    stale = RunLease(
        claim_key="missing",
        owner_id="nobody",
        fencing_token=9,
        lease_expires_at=utcnow(),
        attempt_count=1,
    )
    assert await restarted.complete(stale, "exec-ghost") is False
    assert await restarted.get_claim("missing") is None


def test_time_helpers_are_naive_utc() -> None:
    assert utcnow().tzinfo is None
    naive = datetime(2026, 11, 6, 12, 0, 0)
    assert _as_naive_utc(naive) is naive
    from datetime import timezone as tz

    aware = datetime(2026, 11, 6, 20, 0, 0, tzinfo=tz(timedelta(hours=8)))
    assert _as_naive_utc(aware) == datetime(2026, 11, 6, 12, 0, 0)
    assert _json_parameters({"d": datetime(2026, 1, 2, 3, 4)}) == {"d": "2026-01-02 03:04:00"}


def test_model_matches_live_claim_table_shape() -> None:
    table = TaskRunClaim.__table__
    assert table.name == "task_run_claims"
    assert [column.name for column in table.primary_key.columns] == ["claim_key"]
    assert "id" not in table.c
    expected = {
        "claim_key": "VARCHAR(64)",
        "task_id": "INTEGER",
        "script_id": "VARCHAR(100)",
        "source": "VARCHAR(50)",
        "period_key": "VARCHAR(255)",
        "parameter_hash": "VARCHAR(64)",
        "parameters": "JSON",
        "status": "VARCHAR(20)",
        "owner_id": "VARCHAR(64)",
        "fencing_token": "BIGINT",
        "lease_expires_at": "DATETIME",
        "attempt_count": "INTEGER",
        "next_retry_at": "DATETIME",
        "execution_id": "VARCHAR(100)",
        "last_error": "TEXT",
        "completed_at": "DATETIME",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    assert {name: str(column.type) for name, column in table.c.items()} == expected
    nullable = {name: column.nullable for name, column in table.c.items()}
    assert nullable == {
        "claim_key": False,
        "task_id": True,
        "script_id": False,
        "source": False,
        "period_key": False,
        "parameter_hash": False,
        "parameters": False,
        "status": False,
        "owner_id": True,
        "fencing_token": False,
        "lease_expires_at": True,
        "attempt_count": False,
        "next_retry_at": True,
        "execution_id": True,
        "last_error": True,
        "completed_at": True,
        "created_at": False,
        "updated_at": False,
    }
    indexed = {index.name for index in table.indexes}
    for column in (
        "task_id",
        "script_id",
        "source",
        "status",
        "lease_expires_at",
        "next_retry_at",
        "execution_id",
    ):
        assert f"ix_task_run_claims_{column}" in indexed

    control = SchedulerControl.__table__
    assert control.name == "scheduler_control"
    assert [column.name for column in control.primary_key.columns] == ["control_id"]
    assert str(control.c.source_next_allowed_at.type) == "JSON"
    assert control.c.source_next_allowed_at.nullable is False
    assert control.c.control_id.default is not None
    assert control.c.updated_at.default is not None
