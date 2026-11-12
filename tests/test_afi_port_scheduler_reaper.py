"""Startup reaping of interrupted RUNNING executions.

A process restart orphans any execution still marked RUNNING; the scheduler
must reclassify those rows on start so the running-count metric cannot stay
inflated by work whose worker no longer exists.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import opendata.models  # noqa: F401  # register all tables on Base.metadata
from opendata.afi_port.runtime.scheduler import AkshareScheduler
from opendata.core.database import Base
from opendata.models.task import TaskExecution, TaskStatus, TriggeredBy


def _execution(status: TaskStatus, execution_id: int) -> TaskExecution:
    return TaskExecution(
        execution_id=f"exec-{execution_id}",
        status=status,
        triggered_by=TriggeredBy.SCHEDULER,
    )


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_reap_marks_running_rows_failed_and_keeps_others(session_factory) -> None:
    async with session_factory() as db:
        db.add_all(
            [
                _execution(TaskStatus.RUNNING, 1),
                _execution(TaskStatus.RUNNING, 2),
                _execution(TaskStatus.COMPLETED, 3),
                _execution(TaskStatus.FAILED, 4),
            ]
        )
        await db.commit()

    scheduler = AkshareScheduler()
    await scheduler._reap_interrupted_executions(session_factory)

    async with session_factory() as db:
        statuses = dict(
            (await db.execute(select(TaskExecution.execution_id, TaskExecution.status))).all()
        )
        assert statuses["exec-1"] == TaskStatus.FAILED
        assert statuses["exec-2"] == TaskStatus.FAILED
        # Terminal states are untouched.
        assert statuses["exec-3"] == TaskStatus.COMPLETED
        assert statuses["exec-4"] == TaskStatus.FAILED

        failed_running = (
            await db.execute(
                select(func.count())
                .select_from(TaskExecution)
                .where(TaskExecution.status == TaskStatus.RUNNING)
            )
        ).scalar_one()
        assert failed_running == 0


@pytest.mark.asyncio
async def test_reap_is_a_noop_with_no_running_rows(session_factory) -> None:
    async with session_factory() as db:
        db.add(_execution(TaskStatus.COMPLETED, 9))
        await db.commit()

    scheduler = AkshareScheduler()
    await scheduler._reap_interrupted_executions(session_factory)  # must not raise

    async with session_factory() as db:
        count = (await db.execute(select(func.count()).select_from(TaskExecution))).scalar_one()
        assert count == 1
