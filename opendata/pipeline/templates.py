"""P0 pipeline templates (design §9.3, milestone A4.7).

Wires the built-for-purpose pieces into one runnable template:

* :func:`build_stock_daily_pipeline` - the stock-daily pipeline of the
  P0 daily chain: the registry-routed fetcher per symbol, the A4.2 ods
  writer, the A4.5 cross-check (only when a second source exists - it
  needs the A3 THS feed), the A4.6 dwd merge and the schedule window;
* :func:`incremental_window` - the daily incremental window; it ends on
  the trading day the calendar view names when one is given (A4.7), and
  on ``as_of`` itself when the caller is not calendar-aware yet; the
  17:00/18:00 staggering stays pending the real-network calibration;
* :func:`refresh_metadata_backbone` - the step zero design §4.1 gives a
  full-market backfill: put ``Instrument`` and ``TradingCalendar`` rows
  through their own contract and land them, which is what gives the
  window and universe reads a producer behind them;
* :data:`PIPELINE_TEMPLATES` - the built-in jobs of §9.3: the P0
  incremental, the weekly full cross-check, partition maintenance and
  the freshness check;
* :func:`normalize_ods_rows` / :func:`ods_frame_reader` - the bridge
  from ods rows (source naming) to the contract shape, so the merge
  and cross-check layers can read ods directly.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, cast

import pandas as pd
from pydantic import BaseModel, ValidationError

from opendata.data.mapping import normalize_frame, require_domain_mapping
from opendata.data.models import Instrument, TradingCalendar
from opendata.data.models.base import ContractModel
from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from opendata.pipeline.alerts import AlertPolicy, Notifier
    from opendata.pipeline.cross_check_service import CrossCheckService
    from opendata.pipeline.dwd_merge import DwdMergeService
    from opendata.pipeline.runner import Hook
    from opendata.pipeline.trading_calendar import CalendarView


class TemplateKind(str, enum.Enum):
    """Kind of a built-in schedule template."""

    INCREMENTAL = "incremental"
    FULL_CHECK = "full_check"
    PARTITION_MAINTENANCE = "partition_maintenance"
    FRESHNESS = "freshness"


@dataclass(frozen=True)
class ScheduleTemplate:
    """One built-in scheduled job.

    Attributes:
        name: Job name.
        cron: Standard five-field cron expression.
        kind: What the job does.
        payload: Job-specific parameters.
        note: Calibration note (e.g. the staggering window).
    """

    name: str
    cron: str
    kind: TemplateKind
    payload: Mapping[str, str]
    note: str = ""


#: Templates file next to this module (data, not code).
SCHEDULES_PATH = Path(__file__).parent / "schedules.yaml"


def load_schedule_templates(path: Path | None = None) -> tuple[ScheduleTemplate, ...]:
    """Load the built-in schedule templates.

    The definitions live in ``schedules.yaml`` so the routing labels
    (source identifiers) stay out of the runtime ``*.py`` files, as the
    zero-dependency assertion requires.

    Args:
        path: Override for tests; defaults to the shipped file.

    Returns:
        The templates in file order.

    Raises:
        RuntimeError: If the file is missing or malformed (fail closed:
            a deployment without its schedule definitions must not
            start silently empty).
    """
    import yaml

    source_path = path or SCHEDULES_PATH
    try:
        payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"schedule templates {source_path} are unreadable: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("templates"), list):
        raise RuntimeError(f"schedule templates {source_path} must declare a templates list")
    return tuple(_parse_template(entry, source_path) for entry in payload["templates"])


def _parse_template(entry: object, source_path: Path) -> ScheduleTemplate:  # parsed YAML
    """Parse one template entry, failing closed on malformed input.

    Args:
        entry: Raw mapping from the YAML file.
        source_path: File the entry came from (for error messages).

    Returns:
        The parsed template.

    Raises:
        RuntimeError: If a required field is missing or invalid.
    """
    if not isinstance(entry, dict):
        raise RuntimeError(f"schedule template {entry!r} in {source_path} is not a mapping")
    try:
        return ScheduleTemplate(
            name=str(entry["name"]),
            cron=str(entry["cron"]),
            kind=TemplateKind(str(entry["kind"])),
            payload=dict(entry.get("payload") or {}),
            note=str(entry.get("note", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"schedule template {entry!r} in {source_path} is malformed: {exc}"
        ) from exc


#: Built-in jobs (design §9.3), loaded from ``schedules.yaml``.
PIPELINE_TEMPLATES = load_schedule_templates()


def incremental_window(
    as_of: date, *, lookback_days: int = 0, calendar: CalendarView | None = None
) -> Window:
    """Build the daily incremental window.

    Args:
        as_of: The run's reference date (the scheduler passes the day it
            fired on, not necessarily a day that carries data).
        lookback_days: Extra days to re-fetch (0 = just the expected day).
        calendar: Landed trading calendar deciding which date the window
            ends on; None means nothing was consulted and ``as_of`` is
            taken at face value - what a weekend run used to do.

    Returns:
        The inclusive window.

    Raises:
        ValueError: If ``lookback_days`` is negative, or the calendar has
            no trading day in reach (A4.7 fails closed there rather than
            fetching a month-old window).
    """
    if lookback_days < 0:
        raise ValueError(f"lookback_days must be >= 0, got {lookback_days}")
    end = as_of if calendar is None else calendar.expected_data_date(as_of)
    return Window(start=end - timedelta(days=lookback_days), end=end)


#: The two domains design §4.1 makes the backbone of the warehouse: every
#: other domain joins on them, and both provider legs publish *snapshot*
#: rows, so refreshing them is the step zero of a full-market backfill.
#: Typed as the base contract so a domain looked up at runtime still has one
#: ``to_frame``/``model_validate`` face to use - the two subclasses differ
#: only in their fields.
BACKBONE_CONTRACTS: dict[str, type[ContractModel]] = {
    "instrument": Instrument,
    "trading_calendar": TradingCalendar,
}

#: Business key of each backbone table (the dwd upsert target). There is no
#: source mapping to read this from: these two domains are landed as contract
#: rows, not through the per-source column mappings.
BACKBONE_KEYS: dict[str, tuple[str, ...]] = {
    "instrument": ("symbol",),
    "trading_calendar": ("exchange", "date"),
}

BackboneModelT = TypeVar("BackboneModelT", bound=ContractModel)


@dataclass(frozen=True)
class BackboneReport:
    """What one metadata-backbone refresh landed.

    Attributes:
        source: Source label the legs were read from.
        window: Window the calendar leg was refreshed for.
        landed: Domain to rows written to its ``dwd_`` table.
        rejected: Domain to per-row contract refusals (empty when the leg
            matched its contract).
    """

    source: str
    window: Window
    landed: Mapping[str, int]
    rejected: Mapping[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, object]:
        """JSON-shaped report for the job result and the run log."""
        return {
            "source": self.source,
            "window": [self.window.start.isoformat(), self.window.end.isoformat()],
            "landed": dict(self.landed),
            "rejected": {domain: list(reasons) for domain, reasons in self.rejected.items()},
        }


def refresh_metadata_backbone(
    engine: Engine | None,
    *,
    source: str,
    fetch_instruments: Callable[[], Sequence[object]],
    fetch_calendar: Callable[[], Sequence[object]],
    window: Window,
    land: Callable[[str, pd.DataFrame, tuple[str, ...]], int] | None = None,
    merged_at: datetime | None = None,
) -> BackboneReport:
    """Land the metadata backbone the other domains join on (design §4.1).

    This is the step zero the design promises a full-market backfill starts
    with, and it is what gives :func:`opendata.pipeline.jobs.landed_instruments`
    and :func:`opendata.pipeline.trading_calendar.warehouse_calendar` a
    producer: until it runs against a warehouse, both read an absent table
    and the batch keeps deriving from ``dwd_<domain>`` alone.

    Every row is put through its own contract before a table sees it, and a
    row the contract refuses is *reported* rather than landed half-parsed or
    dropped quietly -- the legs are snapshot publishers whose date columns
    were measured hollow (C14/C19), so a shape drift must be visible here.
    Two rows of one leg that carry the same dwd key are refused the same way:
    the upsert would let the later one overwrite the earlier inside a single
    write while the reported count named both, so a collision is reported and
    nothing of it is landed (see :func:`_refuse_colliding_keys`).

    Args:
        engine: Warehouse engine for the default landing writer. May be None
            only together with ``land`` (a dry run reads and contract-checks
            the legs without touching a database).
        source: Source label the two legs were read from; kept as the dwd
            ``source`` trace column.
        fetch_instruments: Reads the instrument catalog (contract rows or
            anything validating as one).
        fetch_calendar: Reads the trading calendar for ``window``.
        window: Window the refresh covers; its end is the ``_as_of`` stamp.
        land: ``(domain, frame, key) -> rows`` writer override; None uses
            the key-level dwd upsert. Injected because creating and writing
            ``dwd_instrument`` / ``dwd_trading_calendar`` is a deployment
            decision, not something an import should do.
        merged_at: Trace timestamp (tests pin it); None means now.

    Returns:
        :class:`BackboneReport` with per-domain landed counts and refusals.

    Raises:
        ValueError: If ``engine`` is None without a ``land`` override --
            there would be nowhere to land the rows that passed.
    """
    from opendata.pipeline.dwd_merge import SOURCE_COLUMN

    stamp = (merged_at or datetime.now(timezone.utc)).replace(tzinfo=None)
    if land is None:
        if engine is None:
            raise ValueError("refresh_metadata_backbone needs an engine without a land override")
        write = _land_backbone(engine)
    else:
        write = land
    legs: Mapping[str, Sequence[object]] = {
        "instrument": list(fetch_instruments()),
        "trading_calendar": list(fetch_calendar()),
    }
    landed: dict[str, int] = {}
    rejected: dict[str, tuple[str, ...]] = {}
    for domain, raw in legs.items():
        model = BACKBONE_CONTRACTS[domain]
        rows, refusals = _backbone_rows(domain, raw, model)
        rows, collisions = _refuse_colliding_keys(
            domain, rows, model=model, key=BACKBONE_KEYS[domain]
        )
        refusals = refusals + tuple(collisions)
        if not rows:
            landed[domain], rejected[domain] = 0, refusals
            continue
        frame = model.to_frame(rows)
        frame[SOURCE_COLUMN] = source
        frame["_merged_at"] = stamp
        frame["_diff_flag"] = 0
        frame["_as_of"] = window.end
        landed[domain] = write(domain, frame, BACKBONE_KEYS[domain])
        rejected[domain] = refusals
    return BackboneReport(source=source, window=window, landed=landed, rejected=rejected)


def _backbone_rows(
    domain: str, raw: Sequence[object], model: type[BackboneModelT]
) -> tuple[list[BackboneModelT], tuple[str, ...]]:
    """Put one leg's rows through the domain's contract, row by row.

    Args:
        domain: Backbone domain identifier.
        raw: Rows as the leg published them (contract instances, pydantic
            models or mappings).
        model: The contract class that decides whether a row is landable.

    Returns:
        ``(accepted, refusals)`` -- refusals name the row and the reason.
    """
    accepted: list[BackboneModelT] = []
    refusals: list[str] = []
    for index, row in enumerate(raw):
        if isinstance(row, model):
            accepted.append(row)
            continue
        try:
            payload = (
                row.model_dump(mode="python")
                if isinstance(row, BaseModel)
                else dict(cast("Mapping[str, object]", row))
            )
            accepted.append(model.model_validate(payload))
        except (TypeError, ValueError, ValidationError) as exc:
            refusals.append(f"row {index}: {_refusal(exc)}")
    return accepted, tuple(refusals)


def _refuse_colliding_keys(
    domain: str, rows: Sequence[BackboneModelT], *, model: type[ContractModel], key: Sequence[str]
) -> tuple[list[BackboneModelT], list[str]]:
    """Take back every row whose dwd key another row of the same leg carries.

    The backbone lands with a key-level upsert, so a key handed over twice in
    one frame is written twice and the **last row wins** - while the count the
    run publishes is ``len(frame)``, i.e. it names rows the table never kept.
    Which of the two rows is right is not knowable here (the pages carry no
    version), so neither is landed and the collision is reported instead.

    That is the safer direction, not the stricter one: a symbol the catalog
    does not answer for is *unknown* to
    :func:`opendata.pipeline.jobs.drop_inactive_symbols`, which keeps it in the
    universe, whereas a symbol landed from the wrong page silently drops or
    resurrects it on the strength of that page's dates.

    C42 measured the live face of this: nine catalog pages, 65,895 contract
    rows, zero collisions - so nothing is being lost today. What is missing is
    a guard, and the day a page starts publishing an index under a stock's
    symbol the run would have reported a landing that quietly overwrote rows.

    Args:
        domain: Backbone domain identifier, for the refusal text.
        rows: Contract-accepted rows, in the leg's own order.
        model: The domain's contract, used to name the fields that disagree.
        key: The domain's dwd business key (:data:`BACKBONE_KEYS`).

    Returns:
        ``(landable, refusals)`` - every member of a colliding group is refused.
    """
    groups: dict[tuple[str, ...], list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(_key_signature(row, key), []).append(index)

    landable = [row for row in rows if len(groups[_key_signature(row, key)]) == 1]
    refusals: list[str] = []
    for signature, indexes in groups.items():
        if len(indexes) < 2:
            continue
        rendered = ", ".join(
            f"{column}={value}" for column, value in zip(key, signature, strict=True)
        )
        differing = [
            field
            for field in model.model_fields
            if len({str(getattr(rows[index], field)) for index in indexes}) > 1
        ]
        agreement = (
            f"and disagrees on {', '.join(differing)}" if differing else "with identical values"
        )
        refusals.append(
            f"key {rendered}: {domain} published it on {len(indexes)} rows {agreement}; none landed"
        )
    return landable, refusals


def _key_signature(row: ContractModel, key: Sequence[str]) -> tuple[str, ...]:
    """The upsert key of one contract row, as comparable text."""
    return tuple(str(getattr(row, column)) for column in key)


def _refusal(exc: Exception) -> str:
    """One-line reason for a contract refusal (pydantic reports per field)."""
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        return f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
    return str(exc)


def _land_backbone(engine: Engine) -> Callable[[str, pd.DataFrame, tuple[str, ...]], int]:
    """The default writer: key-level upsert into ``dwd_<domain>``."""
    from opendata.data.domains import dwd_table
    from opendata.pipeline.dwd_merge import DwdWriter

    writer = DwdWriter(engine)

    def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
        return writer.write(frame, table=dwd_table(domain), key=key)

    return land


def normalize_ods_rows(
    rows: Sequence[Mapping[str, object]], *, domain: str, source: str
) -> pd.DataFrame:
    """Project ods rows onto the contract shape through the mapping.

    Args:
        rows: Raw ods rows (source column names).
        domain: Registered domain identifier.
        source: Source identifier.

    Returns:
        The contract-shaped frame.

    Raises:
        ValueError: If a mapped column is missing (fail closed).
    """
    mapping = require_domain_mapping(source, domain)
    return normalize_frame(pd.DataFrame(list(rows)), mapping)


def default_source(domain: str) -> str:
    """Resolve the source that maps a domain, from the mapping files.

    The label never appears as a literal in code (AC-16); the shipped
    mappings are the registry of mapped sources.

    Args:
        domain: Registered domain identifier.

    Returns:
        The source identifier.

    Raises:
        LookupError: If no mapping covers the domain (fail closed).
    """
    from opendata.data.mapping import load_mapping, mapping_sources

    for source in mapping_sources():
        if domain in load_mapping(source).domains:
            return source
    raise LookupError(f"no source mapping covers domain {domain!r}")


def build_cross_check(
    engine: Engine,
    *,
    domain: str = "stock_daily",
    sources: tuple[str, str] | None = None,
    notifier: Notifier | None = None,
    policy: AlertPolicy | None = None,
) -> CrossCheckService:
    """Wire one domain's step-3 cross-check, delivery channels included.

    Both defaults are what AC-9 asks for. The pair comes from the
    domain's authority baseline (the two feeds the merge already ranks),
    and the notifier/policy default to the production ones, so a caller
    cannot build a cross-check that computes a decision and drops it.
    Before this factory the only production ``CrossCheckService`` was
    built with neither argument and a fresh per-instance policy.

    Args:
        engine: Warehouse engine (raw ods readers, ``dq_diff_report``).
        domain: Registered domain identifier.
        sources: The (A, B) pair; None takes the first two feeds of the
            domain's authority baseline.
        notifier: Delivery hook; None uses the production dispatcher (WS
            broadcast + subscription hub + SMTP). Tests pass a recorder.
        policy: Alert policy; None uses the process-scoped one, so the
            "this difference was already alerted" state is shared by the
            incremental pipeline and the weekly full check instead of
            dying with the service instance.

    Returns:
        The wired service.

    Raises:
        LookupError: The baseline names fewer than two feeds, or a feed of
            the pair has no mapping for the domain. Fail closed: comparing
            a source with itself is not a cross-check.
    """
    from opendata.data.registry import authority_baseline
    from opendata.pipeline.cross_check_service import CrossCheckService
    from opendata.pipeline.diff_alerts import default_notifier, shared_policy
    from opendata.pipeline.diff_report import DiffReportWriter

    if sources is None:
        baseline = tuple(authority_baseline().get(domain, ()))
        if len(baseline) < 2:
            raise LookupError(
                f"domain {domain!r} has no second feed in authority.json "
                f"({list(baseline) or 'none'}); a cross-check needs two"
            )
        sources = (baseline[0], baseline[1])
    return CrossCheckService(
        domain,
        sources=sources,
        mappings={source: require_domain_mapping(source, domain) for source in sources},
        readers={source: ods_raw_reader(engine, domain, source) for source in sources},
        write_report=DiffReportWriter(engine).write,
        notifier=default_notifier() if notifier is None else notifier,
        policy=shared_policy() if policy is None else policy,
    )


def build_stock_daily_pipeline(
    *,
    engine: Engine,
    session_maker: async_sessionmaker[AsyncSession],
    fetch_symbol: Callable[[str, Window], pd.DataFrame],
    symbols: Sequence[str],
    source: str | None = None,
    second_source: str | None = None,
    shard_size: int = 500,
    batch_id: str | None = None,
    notify: Hook | None = None,
) -> DataPipeline:
    """Build the stock-daily pipeline of the P0 daily chain.

    Args:
        engine: Warehouse engine (ods/dwd writers, readers).
        session_maker: Main-database session factory (progress rows).
        fetch_symbol: Fetch one symbol for a window.
        symbols: Symbol universe to shard.
        source: Source identifier; defaults to the mapping's source.
        second_source: The second source identifier; when given, the
            A4.5 cross-check is wired through :func:`build_cross_check`
            (so its alert reaches the channels), otherwise the step is
            skipped.
        shard_size: Symbols per shard.
        batch_id: Fixed ods batch id (tests); None generates one.
        notify: Step-5 hook override; None wires the default batch
            notifier, which records the watermark and pushes the
            ``data.update`` event (design §10.2).

    Returns:
        The wired pipeline, ready for ``run(window)``.
    """
    from uuid import uuid4

    from opendata.pipeline.dwd_merge import DwdMergeService
    from opendata.pipeline.notify import build_notify_hook
    from opendata.pipeline.ods_writer import OdsWriter

    resolved_source = source or default_source("stock_daily")
    # The ods table keeps the source's own column names (design §8.1),
    # so the shard keys the writer and the ods readers filter on are the
    # mapping's *source* spelling; the contract key stays what the dwd
    # merge upserts.
    ods_key = require_domain_mapping(resolved_source, "stock_daily").source_key
    contract_key = ("symbol", "trade_date")
    spec = PipelineSpec(
        domain="stock_daily",
        source=resolved_source,
        key=ods_key,
        symbols=tuple(symbols),
        shard_size=shard_size,
    )
    writer = OdsWriter(engine)
    batch = batch_id or str(uuid4())

    def write_ods(frame: pd.DataFrame) -> int:
        return writer.write(
            frame,
            table=spec.table,
            key=ods_key,
            source=spec.source,
            batch_id=batch,
        ).rows

    from opendata.data.registry import authority_baseline

    sources = (resolved_source,) if second_source is None else (resolved_source, second_source)
    authority = (
        tuple(entry for entry in authority_baseline().get(spec.domain, ()) if entry in sources)
        or sources
    )
    merge: DwdMergeService = DwdMergeService(
        "stock_daily",
        sources=sources,
        authority=authority,
        readers={source: ods_frame_reader(engine, "stock_daily", source) for source in sources},
        # The mappings travel with the merge because step 4's affected keys
        # arrive in the writing source's spelling and have to be re-spelled
        # before they can mark anything.
        mappings={source: require_domain_mapping(source, "stock_daily") for source in sources},
        write_dwd=lambda frame: _write_dwd(engine, frame),
        key=contract_key,
    )
    cross_check = None
    if second_source is not None:
        cross_check = build_cross_check(
            engine,
            domain="stock_daily",
            sources=(resolved_source, second_source),
        )
    step_five: Hook | None = notify
    if step_five is None:
        # The batch id is shared with the writer on purpose: the
        # notifier reads a `full` payload back by `_batch_id`, so a
        # different id would return an empty frame.
        step_five = build_notify_hook(engine, batch_id=batch, layer="ods", table=spec.table)
    return DataPipeline(
        spec,
        session_maker=session_maker,
        write_ods=write_ods,
        fetch_symbol=fetch_symbol,
        cross_check=cross_check.run_hook if cross_check else None,
        merge=merge.run_hook,
        notify=step_five,
    )


def ods_frame_reader(engine: Engine, domain: str, source: str) -> Callable:
    """Build the reader that feeds the merge/cross-check from ods.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        source: Source identifier.

    Returns:
        A reader ``(start, end, affected_keys) -> contract frame``.
    """
    from opendata.data.domains import ods_table
    from opendata.pipeline.query import resolve_time_field

    # The ods table keeps the SOURCE's column names, so the window filter
    # must use the source's mapped date column - not the contract field
    # name, which only matches for sources that happen to name it the
    # same (the ths tables do; the akshare ones use 日期).
    mapping = require_domain_mapping(source, domain)
    contract_date_field = resolve_time_field(domain)
    source_date_column = mapping.fields[contract_date_field].source_column

    def read(start: date, end: date, affected_keys: set[tuple]) -> pd.DataFrame:
        # The affected keys arrive in the ods spelling of the source that
        # was just written; every other source spells its symbol
        # differently (``thscode`` vs ``股票代码``). So the read stays a
        # window read, widened to cover the affected dates, and the keys
        # are matched after normalization - which is source-agnostic.
        keys = {mapping.to_contract_key(key) for key in affected_keys}
        dates = [key[-1] for key in keys if isinstance(key[-1], date)]
        lower = min([start, *dates]) if dates else start
        upper = max([end, *dates]) if dates else end
        rows = _load_ods_rows(
            engine,
            ods_table(domain, source),
            domain,
            lower,
            upper,
            set(),
            time_column=source_date_column,
        )
        if not rows:
            return pd.DataFrame()
        frame = normalize_ods_rows(rows, domain=domain, source=source)
        if not keys:
            return frame
        in_window = frame[contract_date_field].between(start, end)
        matches = frame[list(mapping.key)].apply(
            lambda row: tuple(row) in keys, axis=1
        )  # affected keys outside the window
        return frame[in_window | matches]

    return read


def ods_raw_reader(engine: Engine, domain: str, source: str) -> Callable[[Window], pd.DataFrame]:
    """Build the RAW ods reader the cross-check needs.

    ``CrossCheckService`` normalizes each side itself (it owns the
    per-source field mappings), so its readers must hand back the
    source's own columns. Handing it :func:`ods_frame_reader` instead
    double-normalizes and fails closed on every run.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        source: Source identifier.

    Returns:
        A reader ``(window) -> raw ods frame``.
    """
    from opendata.data.domains import ods_table

    source_date_column = _source_date_column(domain, source)
    table = ods_table(domain, source)

    def read(window: Window) -> pd.DataFrame:
        rows = _load_ods_rows(
            engine,
            table,
            domain,
            window.start,
            window.end,
            set(),
            time_column=source_date_column,
        )
        return pd.DataFrame(rows)

    return read


#: Longest window the scheduled full cross-check compares in one run.
#: ``window: full`` in schedules.yaml names the kind of check, not an
#: unbounded scan: C48 measured the ths leg at 10.3M rows over ten years,
#: and a reader that loaded both legs whole would exhaust the process
#: before it compared a single key.
FULL_CHECK_MAX_DAYS = 31

#: How far back the span probe looks for one leg's oldest day.
FULL_CHECK_PROBE_DAYS = 400


def ods_leg_span(
    engine: Engine, domain: str, source: str, *, probe_days: int = FULL_CHECK_PROBE_DAYS
) -> tuple[date, date]:
    """Oldest and newest ods date of one leg within the last ``probe_days``.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        source: Source identifier.
        probe_days: Lookback of the probe; the read stays a bounded range
            because a full-table ``MIN`` over a ten-year partitioned leg is
            not something to put on a schedule.

    Returns:
        ``(oldest, newest)`` of the leg's own date column.

    Raises:
        ValueError: The leg holds no rows inside the probe window. Naming a
            span for an empty leg would let the check compare a source with
            nothing and report the result as a difference.
    """
    from sqlalchemy import text

    from opendata.data.domains import ods_table

    column = _source_date_column(domain, source)
    table = ods_table(domain, source)
    floor = date.today() - timedelta(days=probe_days)
    sql = f"SELECT MIN(`{column}`), MAX(`{column}`) FROM `{table}` WHERE `{column}` >= :floor"  # noqa: S608  # names derived from the mapping
    with engine.connect() as connection:
        oldest, newest = connection.execute(text(sql), {"floor": floor}).one()
    if oldest is None or newest is None:
        raise ValueError(f"ods leg {table!r} has no rows since {floor}; nothing to compare")
    return _as_iso_date(oldest), _as_iso_date(newest)


def cross_check_window(
    engine: Engine,
    domain: str,
    sources: Sequence[str],
    *,
    max_days: int = FULL_CHECK_MAX_DAYS,
) -> Window:
    """The date window that both cross-checked legs actually cover.

    The scheduled check cannot take its window from the payload: the two
    feeds reach different far sides (the ported akshare leg stops wherever
    the last run ended while the authoritative feed runs years deep), so a
    window picked from the calendar would compare rows only one side holds
    and report the whole gap as a difference. The overlap is what the two
    sources can actually be agreed on.

    Args:
        engine: Warehouse engine.
        domain: Registered domain identifier.
        sources: The pair being compared.
        max_days: Cap on the window width, walking back from the newest
            shared day.

    Returns:
        The inclusive window.

    Raises:
        ValueError: A leg is empty (from :func:`ods_leg_span`) or the spans
            do not overlap.
    """
    spans = [ods_leg_span(engine, domain, source) for source in sources]
    lower = max(span[0] for span in spans)
    upper = min(span[1] for span in spans)
    if lower > upper:
        raise ValueError(
            f"{domain}: legs {list(sources)} cover {spans} and do not overlap; "
            "a cross-check needs days both feeds hold"
        )
    return Window(start=max(lower, upper - timedelta(days=max_days - 1)), end=upper)


def _source_date_column(domain: str, source: str) -> str:
    """The ods column one source keeps this domain's trade date in."""
    from opendata.pipeline.query import resolve_time_field

    mapping = require_domain_mapping(source, domain)
    return mapping.fields[resolve_time_field(domain)].source_column


def _as_iso_date(value: object) -> date:
    """Read a driver's date/datetime/text back as a ``date``."""
    return date.fromisoformat(str(value)[:10])


def _load_ods_rows(
    engine: Engine,
    table: str,
    domain: str,
    start: date,
    end: date,
    affected_keys: set[tuple],
    *,
    time_column: str | None = None,
    symbol_column: str = "symbol",
) -> list[dict]:
    """Read ods rows for the window plus the affected keys.

    Args:
        engine: Warehouse engine.
        table: Ods table (source column names).
        domain: Registered domain identifier.
        start: Window start (inclusive).
        end: Window end (inclusive).
        affected_keys: Business keys to read (empty reads the window).
        time_column: Source's date column; defaults to the contract
            field name (only correct for sources that name it the same).
        symbol_column: Source's symbol column, for the same reason.

    Returns:
        The raw ods rows.
    """
    from sqlalchemy import text

    from opendata.pipeline.query import resolve_time_field

    field = time_column or resolve_time_field(domain)
    params: dict[str, object] = {"start": start, "end": end}
    sql = f"SELECT * FROM `{table}` WHERE `{field}` >= :start AND `{field}` <= :end"  # noqa: S608
    keys = sorted(affected_keys)
    if keys:
        placeholders = []
        for index, (symbol, trade_date) in enumerate(keys):
            params[f"symbol_{index}"] = symbol
            params[f"day_{index}"] = trade_date
            placeholders.append(
                f"(`{symbol_column}` = :symbol_{index} AND `{field}` = :day_{index})"
            )
        sql = f"SELECT * FROM `{table}` WHERE ({' OR '.join(placeholders)})"  # noqa: S608
    with engine.connect() as connection:
        result = connection.execute(text(sql), params)
        columns = list(result.keys())
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _write_dwd(engine: Engine, frame: pd.DataFrame) -> int:
    """Write a merged frame into the stock-daily dwd table."""
    from opendata.pipeline.dwd_merge import DwdWriter

    return DwdWriter(engine).write(frame, table="dwd_stock_daily", key=("symbol", "trade_date"))
