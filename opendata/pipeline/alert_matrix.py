"""The production caller of the A4.8 alert matrix (AC-18, AC-13).

:mod:`opendata.pipeline.freshness` decides *whether* a reading is an
alert, and :func:`~opendata.api.websocket.ws_manager.broadcast` delivers a
WS frame - but until now nothing joined them: the matrix ran only inside
its own tests, so a domain whose table had vanished produced an ``Alert``
object no channel ever carried. This module is that join, and it is where
each of the matrix's four signals gets its measurement:

* **freshness** - the warehouse: one dwd reading per domain plus one ods
  reading per registered ``(domain, source)`` leg.
* **pipeline failure / consecutive failures** - the control database's
  ``pipeline_progress`` checkpoint, aggregated per run and then per leg.
* **partition gaps** - the A4.3 horizon planner, over the warehouse tables
  that are actually partitioned. Reported, never repaired: the engine it
  reads is the production one.
* **disk water level** - the filesystem, through ``shutil.disk_usage``
  (no psutil: the package ships without third-party SDKs).

Two decisions are worth stating because the criteria ("缺失触发告警",
"告警矩阵生效") do not decide them:

* **what is measured** - a leg whose source has no field mapping cannot be
  measured at all; that is counted (``unmapped_legs``) rather than reported
  as ``missing``, because "no mapping registered" and "no rows in the
  table" are different claims. The same rule holds for the other three:
  ``failure_legs`` stays None without a control engine, and ``disk_path``
  records which volume was actually walked.
* **what the lag is measured against** - the caller supplies ``expected``
  (the trading calendar's expected data date, A4.7). Nothing here reads the
  wall clock: a weekend must not page anyone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

from opendata.data.domains import load_domains, ods_table
from opendata.pipeline.freshness import (
    SEVERITY_CRITICAL,
    Alert,
    AlertSettings,
    DiskUsage,
    FreshnessReport,
    PipelineFailure,
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
        failure_legs: ``(domain, source)`` pairs with any run history in
            ``pipeline_progress``, None when the control database was not
            given (nothing was read, as opposed to nothing failed).
        partitioned_tables: warehouse tables the A4.3 planner reported
            on; zero means nothing is partitioned here, not that every
            partition exists.
        disk_path: volume whose water level was read, None when the
            reading was not taken.
    """

    domains: int
    source_legs: int
    unmapped_legs: int
    failure_legs: int | None = None
    partitioned_tables: int = 0
    disk_path: str | None = None

    def as_dict(self) -> dict[str, int | str | None]:
        """Serialize the scope for a job result or a log line."""
        return {
            "domains": self.domains,
            "source_legs": self.source_legs,
            "unmapped_legs": self.unmapped_legs,
            "failure_legs": self.failure_legs,
            "partitioned_tables": self.partitioned_tables,
            "disk_path": self.disk_path,
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


#: Cap on the *runs* one matrix read aggregates out of ``pipeline_progress``.
#: The read aggregates by run, so the cap bounds what comes back by the number
#: of runs rather than by shard rows - one full-market run writes thousands of
#: shards, and a shard level limit would cut a run in half (a half-read run can
#: confirm a failure but never confirm health, which is the reading that keeps a
#: broken leg quiet).
FAILURE_RUN_LIMIT = 1000

#: How many recent runs of one leg take part in the streak count. A leg that
#: has never worked is reported as failing its whole window, not as infinite.
FAILURE_RUN_WINDOW = 10

#: Truncation of the error text carried into an alert body.
FAILURE_ERROR_CHARS = 200


class _RunVerdict(NamedTuple):
    """One run's aggregated verdict: how many shards failed, and an error.

    A ``pipeline_id`` is domain + source + window, so this is the whole
    checkpoint table's unit of health - the shard rows beneath it are
    already counted by the database.
    """

    failed: int
    error: str | None


def collect_failures(
    engine: Engine,
    *,
    run_limit: int = FAILURE_RUN_LIMIT,
) -> tuple[tuple[PipelineFailure, ...], int]:
    """Pipeline failure state per leg, read from ``pipeline_progress``.

    ``pipeline_progress`` is a shard checkpoint, not a run log: one run holds a
    row per symbol shard. So the verdict is aggregated **in the database**, one
    row per run, and a run counts as failed when **any** of its shards failed -
    a batch that lost one symbol did not deliver that window, whichever way the
    rest went. The streak then walks back over the leg's recent runs, which is
    what separates "failed once" from "has not worked since last week".

    Args:
        engine: Control-database engine (``pipeline_progress`` is not a
            warehouse table).
        run_limit: Cap on the runs aggregated, newest window first.

    Returns:
        The failures of the legs whose newest run failed, plus how many legs
        have any run history at all - the second number is what tells an empty
        checkpoint table apart from a healthy pipeline.
    """
    from sqlalchemy import case, func, select

    from opendata.models.pipeline import PipelineProgress, ShardStatus

    failed_shards = func.sum(case((PipelineProgress.status == ShardStatus.FAILED, 1), else_=0))
    # MIN() skips the NULLs the healthy rows contribute, so some stored error of
    # the run comes back. Which one is not a claim: the alert body quotes it as
    # "an error of this run", and a stable choice beats a driver-dependent one.
    run_error = func.min(
        case((PipelineProgress.status == ShardStatus.FAILED, PipelineProgress.error))
    )
    statement = (
        select(
            PipelineProgress.domain,
            PipelineProgress.source,
            PipelineProgress.window_end,
            failed_shards.label("failed_shards"),
            run_error.label("error"),
        )
        .group_by(
            PipelineProgress.pipeline_id,
            PipelineProgress.domain,
            PipelineProgress.source,
            PipelineProgress.window_end,
        )
        .order_by(PipelineProgress.window_end.desc(), PipelineProgress.pipeline_id)
        .limit(run_limit)
    )
    with engine.connect() as conn:
        readings = conn.execute(statement).all()

    legs: dict[tuple[str, str], list[_RunVerdict]] = {}
    for domain, source, _window_end, failed, error in readings:
        legs.setdefault((domain, source), []).append(
            _RunVerdict(failed=int(failed or 0), error=None if error is None else str(error))
        )

    failures: list[PipelineFailure] = []
    for (domain, source), history in sorted(legs.items()):
        streak = 0
        for run in history[:FAILURE_RUN_WINDOW]:
            if not run.failed:
                break
            streak += 1
        if not streak:
            continue
        newest = history[0]
        failures.append(
            PipelineFailure(
                domain=domain,
                source=source,
                consecutive_failures=streak,
                last_error=(newest.error or "no error recorded")[:FAILURE_ERROR_CHARS],
            )
        )
    return tuple(failures), len(legs)


def warehouse_tables(domains: Sequence[str] | None = None) -> tuple[str, ...]:
    """Every warehouse table the matrix can measure: ``dwd_*`` plus each leg's ``ods_*``.

    Args:
        domains: Restrict the table list (None = every registered domain).

    Returns:
        Table names, sorted and deduplicated.
    """
    from opendata.data.domains import dwd_table

    tables: set[str] = set()
    for domain, sources in registered_legs(domains).items():
        tables.add(dwd_table(domain))
        tables.update(ods_table(domain, source) for source in sources)
    return tuple(sorted(tables))


def collect_partitions(
    engine: Engine,
    *,
    current_year: int,
    years_ahead: int = 2,
    domains: Sequence[str] | None = None,
) -> tuple[dict[str, list[tuple[str, date]]], int]:
    """Which yearly partitions the A4.3 horizon planner says are missing.

    The planner is the same one :mod:`opendata.pipeline.partitions` uses to
    add partitions, so "missing" means one thing in this codebase. Only the
    gap is reported here - this module never runs DDL, because the warehouse
    it points at is the production one and splitting a partition is not a
    change to make on someone else's behalf.

    Args:
        engine: Warehouse engine.
        current_year: Year the horizon is measured from (caller's clock).
        years_ahead: Years beyond ``current_year`` that must already exist.
        domains: Restrict the tables measured (None = all registered).

    Returns:
        Table to its missing ``(partition, bound)`` pairs, plus how many of
        the measured tables are partitioned at all.
    """
    from opendata.pipeline.freshness import partition_plans_for
    from opendata.pipeline.partitions import PartitionMaintainer

    maintainer = PartitionMaintainer(engine)
    partitioned = tuple(
        name for name in warehouse_tables(domains) if maintainer.is_partitioned(name)
    )
    if not partitioned:
        return {}, 0
    plans = partition_plans_for(
        engine,
        partitioned,
        current_year=current_year,
        years_ahead=years_ahead,
    )
    return plans, len(partitioned)


def collect_disk(path: Path | None) -> tuple[DiskUsage | None, str | None]:
    """Water level of the volume holding ``path``, and the path actually used.

    ``shutil.disk_usage`` needs a directory that exists, and ``data_dir`` is
    a relative default that may never have been created - so the probe walks
    up to the nearest existing ancestor and reports which one it measured.
    A reading that names its own subject is the difference between "the disk
    is fine" and "nothing was measured here".

    Args:
        path: Path on the volume to measure (None disables the reading).

    Returns:
        The usage and the measured path, ``(None, None)`` without a path.
    """
    import shutil

    if path is None:
        return None, None
    volume = Path(path)
    while not volume.is_dir():
        if volume.parent == volume:
            return None, None
        volume = volume.parent
    usage = shutil.disk_usage(volume)
    return DiskUsage(used_bytes=usage.used, total_bytes=usage.total), str(volume)


async def run_alert_matrix(
    engine: Engine,
    *,
    expected: date,
    settings: AlertSettings | None = None,
    domains: Sequence[str] | None = None,
    control_engine: Engine | None = None,
    current_year: int | None = None,
    years_ahead: int = 2,
    disk_path: Path | None = None,
    broadcast: Broadcast | None = None,
) -> MatrixRun:
    """Measure the four operational signals, apply the matrix and deliver.

    Every kind of the matrix now has a producer, and each one reports what
    it could not measure instead of reporting health: ``failure_legs`` stays
    None without a control database, partitions are only read when the caller
    supplies the year the horizon is measured from, and the disk reading
    names the volume it walked. Omitting an input therefore cannot turn into
    a silently green run.

    Delivery is best effort in one direction only: a channel that refuses a
    frame is logged, never raised through, because the readings the
    alerts are made of are already computed and the caller still needs
    them. ``delivered`` counts what the channel accepted so a silently
    dead broadcast stays visible in the job result.

    Args:
        engine: Warehouse engine.
        expected: Date the freshness readings were measured against.
        settings: Matrix thresholds; the shipped defaults when omitted.
        domains: Restrict the measurement (None = all registered).
        control_engine: Engine for ``pipeline_progress``; None skips the
            pipeline-failure rows of the matrix.
        current_year: Year to check the partition horizon from; None skips
            the partition rows.
        years_ahead: Years beyond ``current_year`` that must exist.
        disk_path: Path whose volume sets the water level; None skips it.
        broadcast: Delivery channel; None disables delivery (the alerts
            are still decided and returned).

    Returns:
        The run: reports, alerts, delivered count and scope.
    """
    reports, scope = await asyncio.to_thread(
        collect_freshness, engine, expected=expected, domains=domains
    )
    failures, failure_legs = await asyncio.to_thread(_failures_or_empty, control_engine)
    plans, partitioned = await asyncio.to_thread(
        _partitions_or_empty,
        engine,
        current_year=current_year,
        years_ahead=years_ahead,
        domains=domains,
    )
    disk, disk_path_used = collect_disk(disk_path)
    alerts = evaluate_alerts(
        settings=settings or AlertSettings(),
        freshness=reports,
        failures=failures,
        partition_plans=plans,
        disk=disk,
    )
    scope = MatrixScope(
        domains=scope.domains,
        source_legs=scope.source_legs,
        unmapped_legs=scope.unmapped_legs,
        failure_legs=failure_legs,
        partitioned_tables=partitioned,
        disk_path=disk_path_used,
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
            await broadcast(alert_message(alert, expected=expected))
        except Exception as exc:  # a dead channel must not lose the readings
            logger.warning(f"alert broadcast failed: {exc}")
            continue
        delivered += 1
    return MatrixRun(
        expected=expected,
        reports=reports,
        alerts=tuple(alerts),
        delivered=delivered,
        scope=scope,
    )


def _failures_or_empty(engine: Engine | None) -> tuple[tuple[PipelineFailure, ...], int | None]:
    """Pipeline failure rows, or ``((), None)`` when nothing was measured.

    ``None`` rather than ``0``: the scope has to distinguish "the control
    database was not given" from "every leg's last run worked".
    """
    if engine is None:
        return (), None
    return collect_failures(engine)


def _partitions_or_empty(
    engine: Engine,
    *,
    current_year: int | None,
    years_ahead: int,
    domains: Sequence[str] | None,
) -> tuple[dict[str, list[tuple[str, date]]], int]:
    """Partition rows of the matrix, empty when no horizon year was given."""
    if current_year is None:
        return {}, 0
    return collect_partitions(
        engine,
        current_year=current_year,
        years_ahead=years_ahead,
        domains=domains,
    )


def alert_message(alert: Alert, *, expected: date) -> dict[str, Any]:
    """Render one alert of any rule as a WS frame with a common envelope.

    Every frame carries ``rule``/``severity``/``subject``/``detail``/
    ``created_at`` so a subscriber can page on severity without parsing
    prose, and adds the fields its own rule is about: freshness splits the
    subject into the domain and the leg it belongs to, a pipeline alert says
    which leg and how many runs in a row, a partition alert names the table,
    and a disk alert states its volume. One renderer per rule is also what
    keeps the subjects honest - ``domain:source`` means something in a
    freshness frame and nothing in a disk frame.

    Args:
        alert: The decided alert.
        expected: Date the freshness reading was measured against.

    Returns:
        The broadcast message.
    """
    if alert.rule == "freshness":
        return freshness_message(alert, expected=expected)
    body: dict[str, Any] = {
        "type": f"data.{alert.rule}_alert",
        "rule": alert.rule,
        "severity": alert.severity,
        "subject": alert.subject,
        "detail": alert.detail,
        "created_at": utcnow().isoformat(),
    }
    if alert.rule in ("pipeline_failure", "consecutive_failures"):
        domain, _, source = alert.subject.partition(":")
        body["domain"] = domain
        body["source"] = source or None
    return body


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
    "alert_message",
    "collect_disk",
    "collect_failures",
    "collect_freshness",
    "collect_partitions",
    "freshness_message",
    "registered_legs",
    "run_alert_matrix",
    "warehouse_tables",
]
