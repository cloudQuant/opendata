"""Production trigger for the built-in pipeline jobs (AC-13, design §9.3).

:mod:`opendata.pipeline.templates` declares the jobs as *data* (cron,
payload, kind) and :class:`~opendata.pipeline.runner.DataPipeline` runs
the six steps, but nothing joined the two: a pipeline could only be
driven from a test. This module is that join.

Two entry points, because the acceptance scenarios need both:

* :func:`run_incremental_job` - execute one incremental batch now (the
  manual trigger behind ``POST /api/v1/data/pipeline/run``, and the
  fallback 验收文档 §0-4 allows before the cron has ever fired);
* :func:`register_builtin_jobs` - hand the cron definitions to the
  scheduler service so the same run happens on schedule.

A dual-source batch runs one pipeline per source, secondary first: each
run fills its own ods table (step 2), so the authoritative run - the one
that carries ``second_source`` and therefore cross-check plus
dual-source merge - finds both tables populated. Earlier runs merge in
passthrough mode; the last one overwrites their dwd rows with the
merged, flagged result (the dwd write is a key-level upsert).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger
from sqlalchemy import text

from opendata.data.domains import dwd_table, ods_table, require_domain
from opendata.pipeline.freshness import dwd_freshness, ods_freshness
from opendata.pipeline.templates import (
    PIPELINE_TEMPLATES,
    TemplateKind,
    build_stock_daily_pipeline,
    default_source,
    incremental_window,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from opendata.data.protocol import Fetcher
    from opendata.pipeline.runner import PipelineOutcome, Window
    from opendata.pipeline.templates import ScheduleTemplate

#: Domains with a six-step builder today (one builder per chain).
SUPPORTED_DOMAINS = frozenset({"stock_daily"})

#: Template kinds this module can execute.
EXECUTABLE_KINDS = frozenset({TemplateKind.INCREMENTAL})

#: Universe cap for one batch when the caller asks for "all" symbols: a
#: scheduled run must stay finite even as the warehouse grows.
DEFAULT_SYMBOL_LIMIT = 5000


@dataclass(frozen=True)
class JobResult:
    """What one pipeline job run produced.

    Attributes:
        domain: Domain identifier the batch covered.
        sources: Sources written by this run, authoritative source last.
        window: Inclusive date window of the run.
        symbols: Number of symbols in the universe.
        outcomes: Runner outcome per source, in run order.
        dwd_rows: Rows of ``dwd_<domain>`` inside the window after the run.
        freshness: Latest date per ods table and for dwd, as ISO strings.
    """

    domain: str
    sources: tuple[str, ...]
    window: Window
    symbols: int
    outcomes: dict[str, PipelineOutcome] = field(default_factory=dict)
    dwd_rows: int = 0
    freshness: dict[str, str | None] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        """Total failed shards across the run's sources."""
        return sum(outcome.shards_failed for outcome in self.outcomes.values())

    def as_dict(self) -> dict[str, Any]:
        """Serialize the result for an API response or a log line.

        Returns:
            A JSON-friendly mapping (counters only, no failure bodies).
        """
        return {
            "domain": self.domain,
            "sources": list(self.sources),
            "window": {
                "start": self.window.start.isoformat(),
                "end": self.window.end.isoformat(),
            },
            "symbols": self.symbols,
            "per_source": {
                source: {
                    "pipeline_id": outcome.pipeline_id,
                    "rows_written": outcome.rows_written,
                    "shards_total": outcome.shards_total,
                    "shards_done": outcome.shards_done,
                    "shards_failed": outcome.shards_failed,
                    "resumed_shards": outcome.resumed_shards,
                }
                for source, outcome in self.outcomes.items()
            },
            "dwd_rows": self.dwd_rows,
            "freshness": self.freshness,
        }


@lru_cache(maxsize=1)
def warehouse_engine() -> Engine:
    """Process-wide sync engine of the data warehouse.

    The pipeline writers and readers are synchronous (SQLAlchemy Core)
    while the application runs on async engines; the same engine backs
    the data-query endpoints.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from opendata.core.config import settings

    return create_engine(settings.data_database_url, poolclass=NullPool)


def resolve_fetcher(domain: str, source: str) -> Fetcher[Any, Any]:
    """Route one (domain, source) pair through the provider registry.

    Args:
        domain: Domain identifier, e.g. ``stock_daily``.
        source: Routing source label; explicit, never ``auto`` - a
            scheduled batch must not change feed under its own feet.

    Returns:
        The registered fetcher.

    Raises:
        LookupError: If the source declares no capability for the domain.
    """
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry().resolve_domain(domain, source=source)


def make_fetch_symbol(
    domain: str, source: str, *, timeout: float = 30.0
) -> Callable[[str, Window], pd.DataFrame]:
    """Build the step-1 fetch callable for one source.

    The frame returned is what that source's ods table keeps: the
    upstream's own columns when the fetcher hands back a raw frame (the
    ported akshare chains), and the mapping's source columns when the
    adapter normalizes before returning (the fuyao feed) - the ods
    layer keeps source naming (design §8.1), so contract rows are
    projected back through :func:`~opendata.data.mapping.denormalize_frame`.

    Args:
        domain: Domain identifier.
        source: Routing source label.
        timeout: Per-call upstream timeout, in seconds.

    Returns:
        ``(symbol, window) -> frame``.

    Raises:
        LookupError: If the source declares no capability for the domain.
    """
    from opendata.data.protocol import FetchContext

    fetcher = resolve_fetcher(domain, source)
    ctx = FetchContext(timeout=timeout)

    def fetch(symbol: str, window: Window) -> pd.DataFrame:
        query = fetcher.transform_query(symbol=symbol, start_date=window.start, end_date=window.end)
        raw = fetcher.extract_data(query, ctx)
        if isinstance(raw, pd.DataFrame):
            return raw
        from opendata.data.mapping import denormalize_frame, require_domain_mapping

        rows = [row if isinstance(row, dict) else row.model_dump(mode="python") for row in raw]
        mapping = require_domain_mapping(source, domain)
        return denormalize_frame(pd.DataFrame(rows), mapping)

    return fetch


def symbol_universe(
    engine: Engine,
    domain: str,
    *,
    limit: int | None = DEFAULT_SYMBOL_LIMIT,
) -> list[str]:
    """Read the symbol universe of a domain from the warehouse.

    ``dwd_<domain>`` is the list of what this deployment actually covers
    (design §8.1: dwd is the consumer-facing truth), so an incremental
    batch stays inside the known universe instead of re-deriving it from
    the source on every run.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        limit: Cap on the universe; None means :data:`DEFAULT_SYMBOL_LIMIT`.

    Returns:
        Ascending symbol list (empty when the table is not there yet).
    """
    table = dwd_table(domain)
    cap = DEFAULT_SYMBOL_LIMIT if limit is None else max(limit, 1)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT DISTINCT symbol FROM `{table}` ORDER BY symbol LIMIT :cap"  # noqa: S608  # derived table
                ),
                {"cap": cap},
            ).all()
    except Exception as exc:  # a missing table is a normal first-run state
        logger.warning(f"symbol universe unavailable from {table}: {exc!s}")
        return []
    return [str(row[0]) for row in rows]


async def run_incremental_job(
    *,
    domain: str = "stock_daily",
    source: str | None = None,
    second_source: str | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = DEFAULT_SYMBOL_LIMIT,
    as_of: date | None = None,
    lookback_days: int = 0,
    shard_size: int = 200,
    engine: Engine | None = None,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    resume: bool = True,
) -> JobResult:
    """Run one incremental batch of a domain through the six steps.

    Args:
        domain: Domain identifier; only chains with a builder are
            supported (:data:`SUPPORTED_DOMAINS`).
        source: Authoritative source; defaults to the mapped source.
        second_source: Second source; wires cross-check and dual-source
            merge (AC-9) into the authoritative run.
        symbols: Explicit universe; defaults to the warehouse dwd list.
        limit: Cap for the derived universe.
        as_of: End date of the window; defaults to today.
        lookback_days: Days to prepend to the window (repair runs).
        shard_size: Symbols per shard.
        engine: Warehouse engine; defaults to the application's.
        session_maker: Main-database session factory; defaults to the
            application's.
        resume: Skip shards an earlier run already completed.

    Returns:
        The :class:`JobResult` of the batch.

    Raises:
        ValueError: On an unsupported domain or an empty universe.
    """
    from opendata.core.database import async_session_maker

    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(
            f"no pipeline builder for domain {domain!r}; supported: {sorted(SUPPORTED_DOMAINS)}"
        )
    require_domain(domain)
    warehouse = engine or warehouse_engine()
    maker = session_maker or async_session_maker
    authoritative = source or default_source(domain)
    window = incremental_window(as_of or date.today(), lookback_days=lookback_days)
    universe = list(symbols) if symbols else symbol_universe(warehouse, domain, limit=limit)
    if not universe:
        raise ValueError(
            f"empty symbol universe for {domain}: pass symbols= or load {dwd_table(domain)}"
        )

    feeds = [authoritative]
    if second_source is not None and second_source != authoritative:
        feeds.insert(0, second_source)
    outcomes: dict[str, PipelineOutcome] = {}
    for rank, feed in enumerate(feeds):
        is_authoritative = rank == len(feeds) - 1
        pipeline = build_stock_daily_pipeline(
            engine=warehouse,
            session_maker=maker,
            fetch_symbol=make_fetch_symbol(domain, feed),
            symbols=universe,
            source=feed,
            second_source=second_source if is_authoritative else None,
            shard_size=shard_size,
        )
        outcomes[feed] = await pipeline.run(window, resume=resume)

    return JobResult(
        domain=domain,
        sources=tuple(feeds),
        window=window,
        symbols=len(universe),
        outcomes=outcomes,
        dwd_rows=_count_in_window(warehouse, domain, window),
        freshness=_freshness(warehouse, domain, feeds, expected=window.end),
    )


def _count_in_window(engine: Engine, domain: str, window: Window) -> int:
    """Count dwd rows whose trade date falls inside ``window``.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        window: Inclusive window.

    Returns:
        The row count, or 0 when the table is not readable yet.
    """
    table = dwd_table(domain)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT COUNT(*) FROM `{table}` WHERE trade_date BETWEEN :s AND :e"  # noqa: S608  # derived table
                ),
                {"s": window.start, "e": window.end},
            ).one()
    except Exception as exc:
        logger.warning(f"dwd count of {table} unavailable: {exc!s}")
        return 0
    return int(row[0])


def _freshness(
    engine: Engine,
    domain: str,
    sources: Sequence[str],
    *,
    expected: date,
) -> dict[str, str | None]:
    """Report the latest data date per ods table and for dwd.

    Reuses the A5 freshness readers (they know ods tables keep the
    source's own column names) and flattens them to ISO dates; an
    unreadable table reports None instead of failing the run.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        sources: Sources whose ods tables should be inspected.
        expected: Date the data should have reached.

    Returns:
        ``{"ods:<source>": iso|None, "dwd": iso|None}``.
    """
    reports: dict[str, str | None] = {}
    for source in sources:
        try:
            report = ods_freshness(
                engine, domain, source, table=ods_table(domain, source), expected=expected
            )
        except LookupError as exc:  # unmapped source: report, never raise
            logger.warning(f"ods freshness of {source}: {exc!s}")
            reports[f"ods:{source}"] = None
            continue
        reports[f"ods:{source}"] = _iso(report.latest)
    reports["dwd"] = _iso(dwd_freshness(engine, domain, expected=expected).latest)
    return reports


def _iso(value: object) -> str | None:
    """ISO-format a date-like value, or None."""
    return value.isoformat() if isinstance(value, date) else None


async def register_builtin_jobs(
    # The A1 scheduler wrapper is untyped; the contract used here is add_job.
    scheduler_service: Any,  # noqa: ANN401  # A1 wrapper
    *,
    templates: Sequence[ScheduleTemplate] = PIPELINE_TEMPLATES,
    run_job: Callable[[ScheduleTemplate], Awaitable[dict[str, Any]]] | None = None,
) -> list[str]:
    """Register the built-in pipeline jobs with the scheduler service.

    Args:
        scheduler_service: The ``SchedulerService`` wrapper; jobs go
            through its ``add_job`` so the in-memory store and the
            job-executed broadcast behave like script tasks.
        templates: Template rows to register (defaults to the shipped
            ``schedules.yaml``).
        run_job: Executor override (tests, or a caller injecting its own
            engine and session factory).

    Returns:
        The job ids registered, in template order. Kinds without an
        executor are skipped with a warning rather than half-wired.
    """
    executor = run_job or _execute_template
    job_ids: list[str] = []
    for template in templates:
        if template.kind not in EXECUTABLE_KINDS:
            logger.info(f"pipeline job {template.name}: kind {template.kind.value} has no executor")
            continue
        job_id = f"pipeline_{template.name}"

        async def run(bound: ScheduleTemplate = template) -> dict[str, Any]:
            result = await executor(bound)
            logger.info(f"pipeline job {bound.name}: {result}")
            return result

        await scheduler_service.add_job(
            job_id=job_id,
            func=run,
            trigger_type="cron",
            trigger_args={"cron_expression": template.cron},
            job_name=f"pipeline:{template.name}",
        )
        job_ids.append(job_id)
    return job_ids


class _CronSink:
    """Adapter from the job contract to a live APScheduler instance.

    ``SchedulerService.add_job`` reports ``job.next_run_time``, which
    APScheduler 3.11 only sets once the scheduler is running - a pending
    job raises ``AttributeError`` there. The pipeline jobs are
    registered at startup, so they go straight to the scheduler and keep
    the same ``add_job`` contract :func:`register_builtin_jobs` uses.
    """

    def __init__(self, scheduler: Any) -> None:  # noqa: ANN401  # A1-owned instance
        self._scheduler = scheduler

    async def add_job(
        self,
        *,
        job_id: str,
        func: Callable[[], Awaitable[object]],
        trigger_type: str,
        trigger_args: Mapping[str, object],
        job_name: str | None = None,
    ) -> None:
        """Add one cron job to the underlying scheduler."""
        from apscheduler.triggers.cron import CronTrigger

        args = dict(trigger_args)
        # ``cron_expression`` is the service wrapper's own vocabulary;
        # APScheduler wants a trigger instance.
        expression = args.pop("cron_expression", None)
        trigger = CronTrigger.from_crontab(str(expression)) if expression else trigger_type
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            name=job_name,
            replace_existing=True,
            **args,
        )


async def attach_builtin_jobs() -> list[str]:
    """Register the built-in pipeline jobs with the running scheduler.

    Called from application startup once the scheduler owns a live
    service; without one the jobs simply stay unregistered (the manual
    trigger still works), which is what ``ENABLE_SCHEDULER=false`` means.

    Returns:
        The job ids registered (empty when the scheduler is off).
    """
    from opendata.services.scheduler_service import get_scheduler_service

    service = get_scheduler_service()
    scheduler = service.get_scheduler() if service is not None else None
    if scheduler is None:
        logger.warning("scheduler service absent: built-in pipeline jobs not registered")
        return []
    return await register_builtin_jobs(_CronSink(scheduler))


async def _execute_template(template: ScheduleTemplate) -> dict[str, Any]:
    """Run one template's batch (the scheduler's coroutine body).

    Args:
        template: The schedule row being fired.

    Returns:
        The serialized :class:`JobResult`.

    Raises:
        ValueError: If the payload targets a domain without a builder or
            declares a window the code cannot resolve yet.
    """
    payload = template.payload
    domain = str(payload.get("domain", "stock_daily"))
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(f"{template.name!r} targets unsupported domain {domain!r}")
    # The cron already restricts the fire to trading weekdays, so the
    # "last trading day" window is the run date itself; a real holiday
    # calendar would replace this (A4.7 calibration item).
    window_kind = str(payload.get("window", "last-trading-day"))
    if window_kind != "last-trading-day":
        raise ValueError(
            f"{template.name!r} declares window {window_kind!r}; only "
            "last-trading-day is executable without a trading calendar"
        )
    result = await run_incremental_job(
        domain=domain,
        source=payload.get("source"),
        second_source=payload.get("second_source"),
    )
    return result.as_dict()


__all__ = [
    "SUPPORTED_DOMAINS",
    "JobResult",
    "make_fetch_symbol",
    "register_builtin_jobs",
    "resolve_fetcher",
    "run_incremental_job",
    "symbol_universe",
    "warehouse_engine",
]
