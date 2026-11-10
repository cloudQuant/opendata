"""One clock for the akshare scheduler surface.

``ak_task_executions.created_at``/``end_time`` and ``ak_scheduled_tasks.last_execution_at``/
``next_execution_at`` are naive ``DATETIME`` columns that are compared against each other by the
scheduler reload, the AC14 accrual reporter and any freshness read. APScheduler hands back
``next_run_time`` in ``AKSHARE_SCHEDULER_TIMEZONE`` (Asia/Shanghai on this host) and
``datetime.now()`` returns host-local time, so writing either of those straight into a naive
column stores wall-clock Shanghai while the execution rows store UTC -- eight hours apart.
Everything that lands in those columns goes through here.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Current UTC without tzinfo, truncated to seconds like the columns it feeds."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Convert a scheduler-provided timestamp to the naive-UTC shape the columns expect.

    A naive value is already host-local wall time, so it is interpreted in the host timezone
    before conversion; assuming it is UTC would silently move it by the host offset again.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone().astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    return value.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
