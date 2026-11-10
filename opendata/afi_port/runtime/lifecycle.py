"""Explicit lifecycle for the ported AFI akshare scheduler (stage-5 cutover).

The ported :class:`AkshareScheduler` must not start implicitly: until
cutover, AFI still owns the armed task set, and two schedulers firing the
same jobs would double-run collection. This module is the single place
that turns the scheduler on, gated by ``akshare_scheduler_enabled`` which
defaults to ``False`` (fail-closed). A deployment that is ready to own the
task set flips the setting and calls :func:`start_ported_akshare_scheduler`
from its composition root (or the app lifespan); nothing starts otherwise.
"""

from __future__ import annotations

from loguru import logger

from opendata.afi_port.runtime.scheduler_service import get_akshare_scheduler_service
from opendata.core.config import get_settings


async def start_ported_akshare_scheduler() -> bool:
    """Start the ported scheduler only when the explicit ownership switch is on.

    Returns ``True`` when the scheduler was started, ``False`` when the
    fail-closed default left it stopped. Repeated calls are safe because the
    underlying scheduler service is a cached singleton.
    """
    settings = get_settings()
    if not settings.akshare_scheduler_enabled:
        logger.warning(
            "Ported AFI akshare scheduler left DISABLED "
            "(set AKSHARE_SCHEDULER_ENABLED=true at cutover to own the task set)"
        )
        return False
    service = get_akshare_scheduler_service()
    await service.start()
    logger.info("Ported AFI akshare scheduler started under explicit cutover switch")
    return True


async def stop_ported_akshare_scheduler() -> None:
    """Stop the ported scheduler if it was started; safe to call when disabled."""
    service = get_akshare_scheduler_service()
    await service.shutdown()
    logger.info("Ported AFI akshare scheduler shut down")
