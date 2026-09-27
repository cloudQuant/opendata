"""The production caller of the A4.8 alert matrix (AC-18).

:mod:`opendata.pipeline.freshness` decides *whether* a freshness reading
is an alert, and :func:`~opendata.api.websocket.ws_manager.broadcast`
delivers a WS frame - but until now nothing joined them: the matrix ran
only inside its own tests, so a domain whose table had vanished produced
an ``Alert`` object no channel ever carried. This module is that join.

Two decisions are worth stating because the criterion ("缺失触发告警")
does not decide them:

* **what is measured** - every registered domain's dwd table plus every
  registered ``(domain, source)`` leg's ods table. A leg whose source has
  no field mapping cannot be measured at all; that is counted
  (``unmapped_legs``) rather than reported as ``missing``, because "no
  mapping registered" and "no rows in the table" are different claims.
* **what the lag is measured against** - the caller supplies ``expected``
  (the trading calendar's expected data date, A4.7). Nothing here reads
  the wall clock: a weekend must not page anyone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from opendata.data.domains import load_domains, ods_table
from opendata.pipeline.freshness import (
    SEVERITY_CRITICAL,
    Alert,
    AlertSettings,
    FreshnessReport,
    dwd_freshness,
    evaluate_alerts,
    ods_freshness,
)
from opendata.pipeline.watermark import utcnow

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import date

    from sqlalchemy import Engine

    #: Delivery channel (the WS broadcast); None disables delivery.
    Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class MatrixScope:
    """What one matrix run could and could not measure.

    Attributes:
        domains: dwd tables measured.
        source_legs: ods tables measured.
        unmapped_legs: legs whose source has no field mapping for the
            domain, so no column exists to read.
    """

    domains: int
    source_legs: int
    unmapped_legs: int

    def as_dict(self) -> dict[str, int]:
        """Serialize the scope for a job result or a log line."""
        return {
            "domains": self.domains,
            "source_legs": self.source_legs,
            "unmapped_legs": self.unmapped_legs,
        }


@dataclass(frozen=True)
class MatrixRun:
    """One run of the alert matrix.

    Attributes:
        expected: Date the readings were measured against.
        reports: Freshness readings behind the alerts.
        alerts: The alerts the matrix decided on.
        delivered: Alerts the broadcast channel accepted.
        scope: What was measured, and what could not be.
    """

    expected: date
    reports: tuple[FreshnessReport, ...]
    alerts: tuple[Alert, ...]
    delivered: int
    scope: MatrixScope

    def as_dict(self) -> dict[str, Any]:
        """Serialize the run (counters plus the alert bodies).

        Returns:
            A JSON-friendly mapping; report bodies stay out of it, since
            a full report list is the catalog's job, not an alert's.
        """
        return {
            "expected": self.expected.isoformat(),
            "reports": len(self.reports),
            "alerts": [alert.as_dict() for alert in self.alerts],
            "delivered": self.delivered,
            "scope": self.scope.as_dict(),
        }


def registered_legs(domains: Sequence[str] | None = None) -> dict[str, tuple[str, ...]]:
    """Registered ``(domain, sources)`` legs to measure.

    Args:
        domains: Restrict to these domain ids; None measures every
            registered domain.

    Returns:
        Domain id to its registered source ids (sorted, deduplicated).
        Domains with no leg still appear, with an empty tuple.

    Raises:
        ValueError: If a requested domain is not registered.
    """
    from opendata.data.registry import get_registry

    known = set(load_domains())
    if domains is None:
        wanted = sorted(known)
    else:
        unknown = sorted({domain for domain in domains if domain not in known})
        if unknown:
            raise ValueError(f"domains are not registered: {unknown}")
        wanted = sorted(set(domains))
    legs: dict[str, set[str]] = {domain: set() for domain in wanted}
    for capability in get_registry().capabilities():
        if capability.domain in legs:
            legs[capability.domain].add(capability.source)
    return {domain: tuple(sorted(sources)) for domain, sources in legs.items()}


def collect_freshness(
    engine: Engine,
    *,
    expected: date,
    domains: Sequence[str] | None = None,
) -> tuple[tuple[FreshnessReport, ...], MatrixScope]:
    """Read the latest date of every registered table.

    Both layers are measured: ``dwd_<domain>`` answers "is the merged
    data current?" and ``ods_<domain>_<source>`` answers "did this source
    deliver?", which is the reading that separates an upstream outage
    from a merge that never ran.

    Args:
        engine: Warehouse engine.
        expected: Date the data should have reached (trading calendar).
        domains: Restrict the measurement (None = all registered).

    Returns:
        The reports plus what the measurement covered.
    """
    reports: list[FreshnessReport] = []
    unmapped = 0
    legs = registered_legs(domains)
    for domain, sources in legs.items():
        reports.append(dwd_freshness(engine, domain, expected=expected))
        for source in sources:
            report = _ods_leg(engine, domain, source, expected=expected)
            if report is None:
                unmapped += 1
            else:
                reports.append(report)
    scope = MatrixScope(
        domains=len(legs),
        source_legs=len(reports) - len(legs),
        unmapped_legs=unmapped,
    )
    return tuple(reports), scope


def _ods_leg(
    engine: Engine,
    domain: str,
    source: str,
    *,
    expected: date,
) -> FreshnessReport | None:
    """One source leg's ods reading, None when the leg has no column to read.

    ``ods_freshness`` fails closed when the source registers no field
    mapping for the domain. That is not a freshness failure - nothing was
    measured - so it returns None and the caller counts it, rather than
    turning an unmapped leg into a ``missing`` alert nobody can act on.
    """
    try:
        return ods_freshness(
            engine,
            domain,
            source,
            table=ods_table(domain, source),
            expected=expected,
        )
    except LookupError as exc:
        logger.info(f"ods freshness of {domain}/{source} is not measurable: {exc}")
        return None


async def run_alert_matrix(
    engine: Engine,
    *,
    expected: date,
    settings: AlertSettings | None = None,
    domains: Sequence[str] | None = None,
    broadcast: Broadcast | None = None,
) -> MatrixRun:
    """Measure freshness, apply the matrix and deliver what it decides.

    Delivery is best effort in one direction only: a channel that refuses
    a frame is logged, never raised through, because the readings the
    alerts are made of are already computed and the caller still needs
    them. ``delivered`` counts what the channel accepted so a silently
    dead broadcast stays visible in the job result.

    Args:
        engine: Warehouse engine.
        expected: Date the data should have reached.
        settings: Matrix thresholds; the shipped defaults when omitted.
        domains: Restrict the measurement (None = all registered).
        broadcast: Delivery channel; None disables delivery (the alerts
            are still decided and returned).

    Returns:
        The run: reports, alerts, delivered count and scope.
    """
    reports, scope = await asyncio.to_thread(
        collect_freshness, engine, expected=expected, domains=domains
    )
    alerts = evaluate_alerts(
        settings=settings or AlertSettings(),
        freshness=reports,
        failures=(),
        partition_plans={},
        disk=None,
    )
    delivered = 0
    for alert in alerts:
        line = f"[{alert.severity}] {alert.rule} {alert.subject}: {alert.detail}"
        if alert.severity == SEVERITY_CRITICAL:
            logger.critical(line)
        else:
            logger.warning(line)
        if broadcast is None:
            continue
        try:
            await broadcast(freshness_message(alert, expected=expected))
        except Exception as exc:  # a dead channel must not lose the readings
            logger.warning(f"freshness alert broadcast failed: {exc}")
            continue
        delivered += 1
    return MatrixRun(
        expected=expected,
        reports=reports,
        alerts=tuple(alerts),
        delivered=delivered,
        scope=scope,
    )


def freshness_message(alert: Alert, *, expected: date) -> dict[str, Any]:
    """Render one freshness alert as a WS frame (``data.freshness_alert``).

    The domain and source are split out of ``subject`` (which the matrix
    renders as ``domain:source``, or ``domain:dwd`` for the merged layer) so
    a subscriber can filter without parsing prose. ``source`` stays None for
    the merged layer and ``layer`` says which of the two an outage is in -
    "the pipeline did not run" and "this source stopped delivering" are
    different pages.

    Args:
        alert: The decided alert.
        expected: Date the reading was measured against.

    Returns:
        The broadcast message.
    """
    subject, _, leg = alert.subject.partition(":")
    merged = leg in ("", "dwd")
    return {
        "type": "data.freshness_alert",
        "rule": alert.rule,
        "severity": alert.severity,
        "domain": subject,
        "layer": "dwd" if merged else "ods",
        "source": None if merged else leg,
        "detail": alert.detail,
        "expected": expected.isoformat(),
        "created_at": utcnow().isoformat(),
    }


__all__ = [
    "MatrixRun",
    "MatrixScope",
    "collect_freshness",
    "freshness_message",
    "registered_legs",
    "run_alert_matrix",
]
