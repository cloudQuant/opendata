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

import asyncio
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger
from sqlalchemy import text

from opendata.data.domains import dwd_table, ods_table, require_domain
from opendata.data.models import Instrument
from opendata.pipeline.alert_matrix import run_alert_matrix, warehouse_tables
from opendata.pipeline.freshness import dwd_freshness, ods_freshness
from opendata.pipeline.templates import (
    PIPELINE_TEMPLATES,
    TemplateKind,
    build_stock_daily_pipeline,
    default_source,
    incremental_window,
)
from opendata.pipeline.trading_calendar import CalendarView, resolve_calendar

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from opendata.data.protocol import Fetcher
    from opendata.pipeline.alerts import AlertDecision
    from opendata.pipeline.cross_check import DiffSummary
    from opendata.pipeline.runner import PipelineOutcome, Window
    from opendata.pipeline.templates import ScheduleTemplate

#: Domains with a six-step builder today (one builder per chain).
SUPPORTED_DOMAINS = frozenset({"stock_daily"})

#: Template kinds this module can execute. Every kind the shipped
#: ``schedules.yaml`` declares is here: a row whose kind has no executor
#: is skipped at registration, which is how ``partition_maintenance`` and
#: ``freshness`` sat declared-but-dead until C48 (matrix delivery) and C57
#: (the ``REORGANIZE`` half) picked them up.
EXECUTABLE_KINDS = frozenset(
    {
        TemplateKind.INCREMENTAL,
        TemplateKind.FRESHNESS,
        TemplateKind.FULL_CHECK,
        TemplateKind.PARTITION_MAINTENANCE,
    }
)

#: Universe cap for one batch when the caller asks for "all" symbols: a
#: scheduled run must stay finite even as the warehouse grows.
DEFAULT_SYMBOL_LIMIT = 5000

#: Warehouse table holding the landed ``Instrument`` catalog.
INSTRUMENT_TABLE = dwd_table("instrument")

#: Cap on the catalog read. The instrument snapshot carries every asset
#: class at once (C14 measured 5,578 a-share rows on their own), so it is
#: not the size of one bar domain; the read still has to stay finite.
DEFAULT_INSTRUMENT_LIMIT = 50_000


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
        expectation: How the window end was chosen (run date, expected
            data date, calendar tier and which tier decided), as strings.
        universe: How the symbol list was chosen and what the instrument
            catalog did to it, as strings.
    """

    domain: str
    sources: tuple[str, ...]
    window: Window
    symbols: int
    outcomes: dict[str, PipelineOutcome] = field(default_factory=dict)
    dwd_rows: int = 0
    freshness: dict[str, str | None] = field(default_factory=dict)
    expectation: dict[str, str] = field(default_factory=dict)
    universe: dict[str, str] = field(default_factory=dict)

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
            "expectation": self.expectation,
            "universe": self.universe,
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


@lru_cache(maxsize=1)
def control_engine() -> Engine:
    """Process-wide sync engine of the control database.

    The alert matrix's pipeline-failure rows come from
    ``pipeline_progress``, which lives beside ``scheduled_tasks`` rather
    than in the warehouse - so the matrix needs two engines and saying so
    is the point: reading the warehouse for it would quietly find nothing
    and report every leg as healthy.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from opendata.core.config import settings

    return create_engine(settings.database_url_sync, poolclass=NullPool)


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


def landed_instruments(
    engine: Engine, *, limit: int = DEFAULT_INSTRUMENT_LIMIT
) -> list[Instrument]:
    """Read the landed instrument catalog back through its contract.

    ``dwd_instrument`` is the metadata backbone the other domains join
    on (design §4.1 fixes the field set, C14 landed the producer side).
    The rows go through :meth:`Instrument.from_frame`, so a table that
    does not carry the contract's required columns is reported as "no
    catalog" rather than half-believed from whichever columns exist.

    Args:
        engine: Warehouse engine.
        limit: Cap on the rows read (the catalog is a snapshot of every
            asset class, so it is larger than one bar domain).

    Returns:
        Contract rows in ascending ``symbol`` order; empty when the
        catalog is not landed or not readable.
    """
    try:
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        f"SELECT * FROM `{INSTRUMENT_TABLE}` ORDER BY `symbol` LIMIT :cap"  # noqa: S608  # derived table
                    ),
                    {"cap": max(limit, 1)},
                )
                .mappings()
                .all()
            )
        return Instrument.from_frame(pd.DataFrame(list(rows)))
    except Exception as exc:  # an unlanded catalog is the normal state before C41's DDL
        logger.warning(f"instrument catalog unavailable from {INSTRUMENT_TABLE}: {exc!s}")
        return []


def drop_inactive_symbols(
    symbols: Sequence[str], catalog: Sequence[Instrument], *, window: Window
) -> tuple[list[str], list[str], int]:
    """Keep the codes that could have traded inside ``window``.

    The catalog is a snapshot and its date columns are a weaker member of
    its own field set (C14/C19: ``list_date`` came back hollow on 22 of 36
    a-share reads and always for ETFs), so an absent date is *unknown*, not
    "not listed" - only a known date that excludes the window drops a
    symbol. Plain codes collide across asset classes (``000001`` is a stock
    and an index), so a code the catalog holds under more than one suffix is
    left alone rather than dropped on the strength of the wrong row.

    Args:
        symbols: Candidate universe (warehouse symbols are plain codes).
        catalog: Rows from :func:`landed_instruments`; empty means no
            catalog to consult.
        window: Inclusive window the batch is about to fetch.

    Returns:
        ``(kept, dropped, ambiguous_codes)`` in the input's order.
    """
    by_code: dict[str, list[Instrument]] = {}
    for row in catalog:
        by_code.setdefault(row.symbol.split(".", maxsplit=1)[0], []).append(row)
    kept: list[str] = []
    dropped: list[str] = []
    ambiguous = 0
    for symbol in symbols:
        rows = by_code.get(symbol)
        if rows is None:
            kept.append(symbol)
            continue
        if len(rows) > 1:
            ambiguous += 1
            kept.append(symbol)
            continue
        row = rows[0]
        inactive = (row.delist_date is not None and row.delist_date < window.start) or (
            row.list_date is not None and row.list_date > window.end
        )
        if inactive:
            dropped.append(symbol)
        else:
            kept.append(symbol)
    return kept, dropped, ambiguous


def derive_universe(
    engine: Engine,
    domain: str,
    *,
    symbols: Sequence[str] | None,
    limit: int | None,
    window: Window,
) -> tuple[list[str], dict[str, str]]:
    """Choose the symbol list of one batch and say how it was chosen.

    A non-empty ``symbols=`` is the caller's own list and is used as
    given: a repair run asks for a symbol precisely because the derived
    universe did not include it. A derived universe comes from
    ``dwd_<domain>`` and is then checked against the landed
    :class:`~opendata.data.models.Instrument` catalog, which is what
    tells a code that stopped existing from one that merely has no rows
    yet. Either way the answer says which tier spoke, the same way the
    calendar's does.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        symbols: Explicit universe; None or empty means derive it.
        limit: Cap for the derived universe.
        window: Inclusive window the batch is about to fetch.

    Returns:
        ``(symbols, provenance)`` - provenance carries ``from``,
        ``catalog``, ``dropped_inactive``, ``ambiguous_codes`` and, when
        a caller handed in a list, ``requested``.
    """
    if symbols:
        return list(symbols), {"from": "caller", "requested": str(len(symbols))}
    universe = symbol_universe(engine, domain, limit=limit)
    provenance = {"from": dwd_table(domain), "catalog": "absent"}
    if not universe:
        return universe, provenance
    catalog = landed_instruments(engine)
    if not catalog:
        return universe, provenance
    provenance["catalog"] = INSTRUMENT_TABLE
    kept, dropped, ambiguous = drop_inactive_symbols(universe, catalog, window=window)
    provenance["dropped_inactive"] = str(len(dropped))
    provenance["ambiguous_codes"] = str(ambiguous)
    if dropped:
        logger.info(
            f"{domain} universe: {len(dropped)} of {len(universe)} codes did not exist in "
            f"{window.label()} per {INSTRUMENT_TABLE} (first: {dropped[0]})"
        )
    if ambiguous:
        logger.info(f"{domain} universe: {ambiguous} codes match several catalog rows and stay")
    return kept, provenance


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
    calendar: CalendarView | None = None,
) -> JobResult:
    """Run one incremental batch of a domain through the six steps.

    Args:
        domain: Domain identifier; only chains with a builder are
            supported (:data:`SUPPORTED_DOMAINS`).
        source: Authoritative source; defaults to the mapped source.
        second_source: Second source; wires cross-check and dual-source
            merge (AC-9) into the authoritative run.
        symbols: Explicit universe, used as given; defaults to the
            warehouse dwd list narrowed by the landed instrument catalog.
        limit: Cap for the derived universe.
        as_of: Run date; the window ends on the latest date on or before
            it that should carry data (defaults to today).
        lookback_days: Days to prepend to the window (repair runs).
        shard_size: Symbols per shard.
        engine: Warehouse engine; defaults to the application's.
        session_maker: Main-database session factory; defaults to the
            application's.
        resume: Skip shards an earlier run already completed.
        calendar: Trading calendar used to pick the window end; defaults
            to the warehouse calendar, falling back to the weekday rule.

    Returns:
        The :class:`JobResult` of the batch.

    Raises:
        ValueError: On an unsupported domain, an empty universe, or a
            calendar with no trading day in reach.
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
    run_date = as_of or date.today()
    day_calendar = calendar if calendar is not None else resolve_calendar(warehouse)
    window = incremental_window(run_date, lookback_days=lookback_days, calendar=day_calendar)
    expected = window.end
    universe, universe_notes = derive_universe(
        warehouse, domain, symbols=symbols, limit=limit, window=window
    )
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
        expectation={
            "run_date": run_date.isoformat(),
            "expected_data_date": expected.isoformat(),
            "calendar_tier": day_calendar.tier,
            "decided_by": day_calendar.answered_from(expected),
        },
        universe=universe_notes,
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
    if template.kind is TemplateKind.FRESHNESS:
        return await _execute_freshness(template)
    if template.kind is TemplateKind.FULL_CHECK:
        return await _execute_full_check(template)
    if template.kind is TemplateKind.PARTITION_MAINTENANCE:
        return await _execute_partition_maintenance(template)
    payload = template.payload
    domain = str(payload.get("domain", "stock_daily"))
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(f"{template.name!r} targets unsupported domain {domain!r}")
    # The cron already restricts the fire to trading weekdays; the
    # window end then comes from the trading calendar (A4.7), so a
    # holiday run re-covers the last day that should have data instead
    # of fetching a day that never happened.
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


async def _execute_freshness(template: ScheduleTemplate) -> dict[str, Any]:
    """Run the A4.8 alert matrix and put what it decides on a channel (AC-18).

    This is the matrix's only production caller. Before it existed the
    freshness job was declared in ``schedules.yaml`` while its kind had no
    executor, so :func:`register_builtin_jobs` skipped the row: a domain
    whose table had vanished produced an ``Alert`` object that no channel
    ever carried, and the readings themselves were never taken on schedule.

    The lag is measured against the trading calendar's expected data date
    (A4.7), never the wall clock, so a weekend cannot page anyone.

    Args:
        template: The ``freshness`` schedule row being fired.

    Returns:
        The serialized :class:`~opendata.pipeline.alert_matrix.MatrixRun`.

    Raises:
        ValueError: If the payload's ``domains`` token is neither ``all``
            nor a list of registered domains.
    """
    from opendata.api.websocket import ws_manager
    from opendata.core.config import settings

    domains = _payload_domains(template.payload)
    engine = warehouse_engine()
    calendar = await asyncio.to_thread(resolve_calendar, engine)
    expected = calendar.expected_data_date(date.today())
    run = await run_alert_matrix(
        engine,
        expected=expected,
        domains=domains,
        # The other three rows of the matrix read places freshness cannot:
        # the shard checkpoint lives in the control database, the partition
        # horizon needs a year (the calendar answers "which day is data due
        # for", not "which partitions must exist"), and the water level is a
        # filesystem reading on the volume this process writes to.
        control_engine=control_engine(),
        current_year=date.today().year,
        years_ahead=int(template.payload.get("years_ahead", 2)),
        disk_path=settings.data_dir,
        broadcast=ws_manager.broadcast,
    )
    logger.info(f"alert matrix {template.name}: {run.as_dict()}")
    return run.as_dict()


async def _execute_full_check(template: ScheduleTemplate) -> dict[str, Any]:
    """Run the weekly full cross-check over the days both legs hold (AC-9).

    This is the comparison's only scheduled trigger. C48 measured why that
    mattered: ``CrossCheckService`` and the alert policy existed, the
    six-step template wired the hook, and nothing ever called it on
    schedule with a notifier attached - so ``dq_diff_report`` held only the
    rows a hand-driven test batch had written, none of them under the
    ``xcheck:`` batch id the hook produces, and every decision was computed
    then dropped.

    The window comes from the data, not the payload
    (:func:`~opendata.pipeline.templates.cross_check_window`): the two legs
    end months apart, and a calendar-shaped window would report the coverage
    gap as a difference.

    Args:
        template: The ``full_check`` schedule row being fired.

    Returns:
        Counters of the comparison plus one delivery record per alert
        decision - including the suppressed ones, which is how "governance
        kept this quiet" stays distinguishable from "nothing was decided".

    Raises:
        ValueError: On an unsupported domain, a payload window token other
            than ``full``, or legs with no overlapping days.
    """
    from opendata.pipeline.cross_check_service import batch_id_for
    from opendata.pipeline.diff_alerts import default_notifier
    from opendata.pipeline.templates import build_cross_check, cross_check_window

    payload = template.payload
    domain = str(payload.get("domain", "stock_daily"))
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(f"{template.name!r} targets unsupported domain {domain!r}")
    window_kind = str(payload.get("window", "full"))
    if window_kind != "full":
        raise ValueError(
            f"{template.name!r} declares window {window_kind!r}; the full check derives its "
            "window from the days both legs hold and reads no other token"
        )

    warehouse = warehouse_engine()
    production = default_notifier()
    deliveries: list[dict[str, Any]] = []

    async def notify(summary: DiffSummary, decision: AlertDecision) -> None:
        """Deliver on the production channels and keep the record for the job."""
        deliveries.append((await production(summary, decision)).as_dict())

    service = build_cross_check(warehouse, domain=domain, notifier=notify)
    window = await asyncio.to_thread(cross_check_window, warehouse, domain, service.sources)
    batch_id = batch_id_for(domain, window)
    summary = await service.run(batch_id, window)
    result: dict[str, Any] = {
        "domain": domain,
        "sources": list(service.sources),
        "batch_id": batch_id,
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "compared_keys": summary.compared_keys,
        "deviations": summary.deviation_count,
        "missing": summary.missing_count,
        "diff_rate": round(summary.diff_rate, 6),
        "verdict": summary.verdict.value,
        "per_field": dict(summary.per_field),
        "deliveries": deliveries,
    }
    logger.info(f"full cross-check {template.name}: {result}")
    return result


@dataclass(frozen=True)
class PartitionRun:
    """What one partition-horizon pass did.

    Attributes:
        current_year: Year the horizon was measured from.
        years_ahead: Years beyond ``current_year`` that must exist.
        measured: Tables the pass looked at, in registration order.
        partitioned: How many of them are partitioned at all.
        applied: Table to the partitions this pass created.
        remaining: Table to the yearly bounds still missing afterwards.
    """

    current_year: int
    years_ahead: int
    measured: tuple[str, ...]
    partitioned: int
    applied: dict[str, list[str]] = field(default_factory=dict)
    remaining: dict[str, list[str]] = field(default_factory=dict)

    @property
    def applied_total(self) -> int:
        """How many partitions the pass created across all tables."""
        return sum(len(names) for names in self.applied.values())

    def as_dict(self) -> dict[str, Any]:
        """Serialize the pass for an API response or a log line.

        Returns:
            A JSON-friendly mapping; a non-empty ``remaining`` is the reading
            that says the pass ran and the horizon is still short.
        """
        return {
            "current_year": self.current_year,
            "years_ahead": self.years_ahead,
            "measured": list(self.measured),
            "partitioned": self.partitioned,
            "applied": {table: list(names) for table, names in self.applied.items()},
            "applied_total": self.applied_total,
            "remaining": {table: list(names) for table, names in self.remaining.items()},
        }


def maintain_partition_horizon(
    engine: Engine,
    *,
    current_year: int,
    years_ahead: int,
    tables: Sequence[str] | None = None,
    domains: Sequence[str] | None = None,
) -> PartitionRun:
    """Apply the A4.3 horizon rule, then re-read it.

    This is the half :func:`opendata.pipeline.alert_matrix.collect_partitions`
    leaves alone - a report can be read without touching DDL, and the
    warehouse it points at is the production one. The scheduled job is the
    opposite case: ``schedules.yaml`` ships a ``partition-maintenance`` row
    whose note reads "落后即 REORGANIZE", so the plan gets applied here and
    re-planned per table afterwards. The re-read is what keeps an ``ALTER``
    that did not close the gap from reading as success: the leftover lands in
    :attr:`PartitionRun.remaining`.

    Args:
        engine: Warehouse engine.
        current_year: Year the horizon is measured from (caller's clock).
        years_ahead: Years beyond ``current_year`` that must exist.
        tables: Tables to maintain (None = every registered warehouse table).
        domains: Registered domains to expand when ``tables`` is None.

    Returns:
        The pass report.
    """
    from opendata.pipeline.partitions import PartitionMaintainer, plan_yearly_partitions

    if tables is not None:
        wanted = tuple(tables)
    else:
        # ``warehouse_tables`` is a pure read of the provider registry, and that
        # registry fills lazily: a process that has not resolved a fetcher yet
        # knows zero capabilities, so the census silently degrades to the 20
        # ``dwd_*`` names and drops all 33 ``ods_*`` legs -- the very tables the
        # patrol writes into daily. ``_resolve_fetcher`` registers before it
        # reads for the same reason; nothing guarantees an incremental job ran
        # before the 03:00 maintenance row in this process.
        from opendata.data.providers import register_providers

        register_providers()
        wanted = warehouse_tables(domains)
    maintainer = PartitionMaintainer(engine)
    applied: dict[str, list[str]] = {}
    remaining: dict[str, list[str]] = {}
    partitioned = 0
    for table in wanted:
        if not maintainer.is_partitioned(table):
            continue
        partitioned += 1
        created = maintainer.ensure(table, current_year=current_year, years_ahead=years_ahead)
        if created:
            applied[table] = created
        gaps = plan_yearly_partitions(
            maintainer.state(table), current_year=current_year, years_ahead=years_ahead
        )
        if gaps:
            remaining[table] = [name for name, _ in gaps]
    return PartitionRun(
        current_year=current_year,
        years_ahead=years_ahead,
        measured=wanted,
        partitioned=partitioned,
        applied=applied,
        remaining=remaining,
    )


async def _execute_partition_maintenance(template: ScheduleTemplate) -> dict[str, Any]:
    """Extend the warehouse partition horizon on schedule (AC-8 items 5/6).

    C48 could only measure the plan half, and the shipped row was skipped at
    registration because its kind had no executor: nothing ever split
    ``pmax``, so the horizon just lagged until a human did it by hand. The
    failure mode is silent - rows for a new year land in the fallback
    partition and the write succeeds, so every counter stays green while the
    yearly-partition design stops meaning anything.

    Args:
        template: The ``partition_maintenance`` schedule row being fired.

    Returns:
        The serialized :class:`PartitionRun`.
    """
    run = await asyncio.to_thread(
        maintain_partition_horizon,
        warehouse_engine(),
        current_year=date.today().year,
        years_ahead=int(template.payload.get("years_ahead", 2)),
    )
    logger.info(f"partition maintenance {template.name}: {run.as_dict()}")
    return run.as_dict()


def _payload_domains(payload: Mapping[str, object]) -> tuple[str, ...] | None:
    """Resolve the ``domains`` token of a freshness template.

    ``all`` (or no key at all) measures every registered domain. A comma
    list restricts the run. There is deliberately no ``p0`` token: nothing
    in the code registers a P0 domain tier, so a job that claimed to watch
    "the P0 domains" could only be measuring an invented set.

    Args:
        payload: The template payload.

    Returns:
        The domains to measure, or None for all of them.

    Raises:
        ValueError: If the token is empty or of an unexpected type.
    """
    raw = payload.get("domains", "all")
    if isinstance(raw, str):
        if raw.strip().lower() == "all":
            return None
        wanted = tuple(part.strip() for part in raw.split(",") if part.strip())
        if wanted:
            return wanted
    raise ValueError(
        f"freshness payload domains={raw!r}: use 'all' or a comma-separated domain list"
    )


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
