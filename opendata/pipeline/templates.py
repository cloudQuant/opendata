"""P0 pipeline templates (design §9.3, milestone A4.7).

Wires the built-for-purpose pieces into one runnable template:

* :func:`build_stock_daily_pipeline` - the stock-daily pipeline of the
  P0 daily chain: the registry-routed fetcher per symbol, the A4.2 ods
  writer, the A4.5 cross-check (only when a second source exists - it
  needs the A3 THS feed), the A4.6 dwd merge and the schedule window;
* :func:`incremental_window` - the daily incremental window; it is
  calendar-less for now (one day, or a lookback), and the design's
  trading-calendar校准 plus the 17:00/18:00 staggering stay pending
  the real-network calibration;
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
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from opendata.data.mapping import normalize_frame, require_domain_mapping
from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from opendata.pipeline.dwd_merge import DwdMergeService
    from opendata.pipeline.runner import Hook


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


def incremental_window(as_of: date, *, lookback_days: int = 0) -> Window:
    """Build the daily incremental window.

    Args:
        as_of: The run's reference date (the scheduler passes the
            trading day; the calendar wiring is pending A4.7's
            live calibration).
        lookback_days: Extra days to re-fetch (0 = just ``as_of``).

    Returns:
        The inclusive window.

    Raises:
        ValueError: If ``lookback_days`` is negative.
    """
    if lookback_days < 0:
        raise ValueError(f"lookback_days must be >= 0, got {lookback_days}")
    return Window(start=as_of - timedelta(days=lookback_days), end=as_of)


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
            A4.5 cross-check is wired (needs A3's THS feed in
            practice), otherwise the step is skipped.
        shard_size: Symbols per shard.
        batch_id: Fixed ods batch id (tests); None generates one.
        notify: Step-5 hook override; None wires the default batch
            notifier, which records the watermark and pushes the
            ``data.update`` event (design §10.2).

    Returns:
        The wired pipeline, ready for ``run(window)``.
    """
    from uuid import uuid4

    from opendata.pipeline.cross_check_service import CrossCheckService
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
        write_dwd=lambda frame: _write_dwd(engine, frame),
        key=contract_key,
    )
    cross_check = None
    if second_source is not None:
        from opendata.pipeline.diff_report import DiffReportWriter

        mapping = require_domain_mapping(resolved_source, "stock_daily")
        cross_check = CrossCheckService(
            "stock_daily",
            sources=(resolved_source, second_source),
            mappings={
                resolved_source: mapping,
                second_source: require_domain_mapping(second_source, "stock_daily"),
            },
            readers={
                source: ods_raw_reader(engine, "stock_daily", source)
                for source in (resolved_source, second_source)
            },
            write_report=DiffReportWriter(engine).write,
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
    from opendata.pipeline.query import resolve_time_field

    mapping = require_domain_mapping(source, domain)
    source_date_column = mapping.fields[resolve_time_field(domain)].source_column
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
