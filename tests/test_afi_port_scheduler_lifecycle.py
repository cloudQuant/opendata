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
