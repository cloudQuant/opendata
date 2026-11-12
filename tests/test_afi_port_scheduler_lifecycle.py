"""Assembly tests for the fail-closed ported-scheduler lifecycle switch."""

from __future__ import annotations

import pytest

from opendata.afi_port.runtime import lifecycle
from opendata.core.config import get_settings


class _FakeSchedulerService:
    """Records start/shutdown calls instead of touching APScheduler or a DB."""

    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


@pytest.mark.asyncio
async def test_scheduler_is_fail_closed_by_default(monkeypatch) -> None:
    """With the default setting off, the scheduler never starts."""
    settings = get_settings().model_copy(update={"akshare_scheduler_enabled": False})
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)

    fake = _FakeSchedulerService()
    monkeypatch.setattr(lifecycle, "get_akshare_scheduler_service", lambda: fake)

    started = await lifecycle.start_ported_akshare_scheduler()
    assert started is False
    assert fake.started is False


@pytest.mark.asyncio
async def test_scheduler_starts_only_under_the_explicit_switch(monkeypatch) -> None:
    """Flipping the ownership switch starts the ported scheduler."""
    settings = get_settings().model_copy(update={"akshare_scheduler_enabled": True})
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)

    fake = _FakeSchedulerService()
    monkeypatch.setattr(lifecycle, "get_akshare_scheduler_service", lambda: fake)

    started = await lifecycle.start_ported_akshare_scheduler()
    assert started is True
    assert fake.started is True

    await lifecycle.stop_ported_akshare_scheduler()
    assert fake.shutdown_calls == 1


class _FailingSchedulerService:
    """A scheduler whose arming fails, as on a transient database error."""

    async def start(self) -> None:
        raise RuntimeError("transient DB failure while loading armed tasks")

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_scheduler_start_failure_is_contained_not_raised(monkeypatch) -> None:
    """A scheduler arming failure must not crash the API lifespan.

    The function returns False (scheduler considered stopped) and logs instead
    of propagating, so the rest of the application stays available.
    """
    settings = get_settings().model_copy(update={"akshare_scheduler_enabled": True})
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(
        lifecycle,
        "get_akshare_scheduler_service",
        lambda: _FailingSchedulerService(),
    )

    started = await lifecycle.start_ported_akshare_scheduler()
    assert started is False


def test_ported_scheduler_running_reflects_wrapper_state(monkeypatch) -> None:
    """The read-only health helper reports the underlying APScheduler state."""

    class _Aps:
        def __init__(self, running: bool) -> None:
            self.running = running

    class _Wrapper:
        def __init__(self, aps: _Aps | None) -> None:
            # Mirrors AkshareScheduler, whose .scheduler holds the APScheduler.
            self.scheduler = aps

    class _Service:
        def __init__(self, aps: _Aps | None) -> None:
            self.scheduler = _Wrapper(aps)

    monkeypatch.setattr(lifecycle, "get_akshare_scheduler_service", lambda: _Service(_Aps(True)))
    assert lifecycle.ported_akshare_scheduler_running() is True

    monkeypatch.setattr(lifecycle, "get_akshare_scheduler_service", lambda: _Service(_Aps(False)))
    assert lifecycle.ported_akshare_scheduler_running() is False

    # Wrapper exists but no APScheduler has been started yet.
    monkeypatch.setattr(lifecycle, "get_akshare_scheduler_service", lambda: _Service(None))
    assert lifecycle.ported_akshare_scheduler_running() is False
