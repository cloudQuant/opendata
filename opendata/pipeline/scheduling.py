"""Explicit scheduler switch (design §9.3, milestone A4.7).

Scheduler ownership is an **explicit** decision (``ENABLE_SCHEDULER``),
not an inference from the worker count - the previous behaviour
silently disabled the scheduler and looked like a working deployment:

* production without an explicit value fails at startup
  (:class:`SchedulerConfigError`);
* an explicit ``false`` disables it everywhere;
* ``true`` with multiple workers and no Redis disables it with a
  warning: the in-memory APScheduler store would run every job once
  per worker (the design's split-brain case), and the documented
  degradation is "scheduler off + alert + manual intervention";
* development without a value keeps the convenient default (enabled,
  one worker).
"""

from __future__ import annotations

from dataclasses import dataclass


class SchedulerConfigError(RuntimeError):
    """Raised when the scheduler cannot be configured safely."""


@dataclass(frozen=True)
class SchedulerDecision:
    """Whether the scheduler may run, and why.

    Attributes:
        enabled: Whether to start the scheduler.
        reason: Human-readable justification (for logs and alerts).
    """

    enabled: bool
    reason: str


def scheduler_decision(
    *,
    enable_scheduler: bool | None,
    is_production: bool,
    redis_url: str | None,
    workers: int,
) -> SchedulerDecision:
    """Decide whether the scheduler may start.

    Args:
        enable_scheduler: The explicit setting; None means unset.
        is_production: Whether this is a production deployment.
        redis_url: Configured Redis URL, if any.
        workers: Configured worker processes.

    Returns:
        The decision with its reason.

    Raises:
        SchedulerConfigError: In production with no explicit setting
            (fail closed instead of guessing).
    """
    if enable_scheduler is None:
        if is_production:
            raise SchedulerConfigError(
                "ENABLE_SCHEDULER must be set explicitly in production "
                "(true or false); refusing to guess"
            )
        return SchedulerDecision(True, "development default (explicit ENABLE_SCHEDULER unset)")
    if not enable_scheduler:
        return SchedulerDecision(False, "explicitly disabled by ENABLE_SCHEDULER")
    if workers > 1 and not redis_url:
        return SchedulerDecision(
            False,
            f"{workers} workers without Redis: the in-memory job store would run every "
            "job once per worker (set REDIS_URL or run a single worker)",
        )
    return SchedulerDecision(True, f"explicitly enabled ({workers} worker(s))")
