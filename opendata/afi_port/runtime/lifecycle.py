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

    A failure while arming the scheduler is contained: the API process must
    stay up so the control plane stays queryable and the problem is observable
    in the logs, rather than a transient database/scheduler error aborting the
    whole application lifespan. The operator can retry by restarting the service.
    """
    settings = get_settings()
    if not settings.akshare_scheduler_enabled:
        logger.warning(
            "Ported AFI akshare scheduler left DISABLED "
            "(set AKSHARE_SCHEDULER_ENABLED=true at cutover to own the task set)"
        )
        return False
    try:
        service = get_akshare_scheduler_service()
        await service.start()
    except Exception:
        # Logged with the traceback, then swallowed deliberately: see the
        # docstring. Never let background-scheduler arming crash the API.
        logger.opt(exception=True).error(
            "Ported AFI akshare scheduler failed to start; the API remains up "
            "but no collection jobs will fire until the service is restarted"
        )
        return False
    logger.info("Ported AFI akshare scheduler started under explicit cutover switch")
    return True


async def stop_ported_akshare_scheduler() -> None:
    """Stop the ported scheduler if it was started; safe to call when disabled."""
    service = get_akshare_scheduler_service()
    await service.shutdown()
    logger.info("Ported AFI akshare scheduler shut down")
