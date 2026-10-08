"""Refresh DataTable metadata after the pipeline's data and notify steps."""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import inspect, select, text

from opendata.data.domains import dwd_table, ods_table, require_domain
from opendata.data.mapping import require_domain_mapping
from opendata.models.data_table import DataTable
from opendata.pipeline.query import resolve_time_field

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from opendata.pipeline.runner import PipelineContext

_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)


async def refresh_pipeline_metadata(
    warehouse_engine: Engine,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    domain: str,
    sources: Sequence[str],
) -> dict[str, dict[str, object]]:
    """Upsert ODS and DWD row/date statistics in the main database.

    Aggregation runs in the warehouse with ``COUNT/MIN/MAX`` and transfers
    only one summary per table. The ODS aggregate scans the selected source
    tables, so its warehouse cost grows with those tables' row counts; no
    rows are materialized in application memory. All resulting DataTable
    changes commit together in one main-database transaction.
    """
    require_domain(domain)
    time_field = resolve_time_field(domain)
    table_specs: list[tuple[str, str]] = []
    for source in dict.fromkeys(sources):
        mapping = require_domain_mapping(source, domain)
        if time_field not in mapping.fields:
            raise LookupError(f"mapping {source!r}/{domain!r} has no date field {time_field!r}")
        table_specs.append((ods_table(domain, source), mapping.fields[time_field].source_column))
    table_specs.append((dwd_table(domain), time_field))

    summaries = await asyncio.to_thread(
        _collect_warehouse_summaries,
        warehouse_engine,
        table_specs,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result: dict[str, dict[str, object]] = {}
    async with session_maker() as session:
        next_sqlite_id: int | None = None
        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            max_id = await session.scalar(
                select(DataTable.id).order_by(DataTable.id.desc()).limit(1)
            )
            next_sqlite_id = int(max_id or 0) + 1
        for table, _date_column, count, first_day, last_day in summaries:
            record = (
                await session.execute(select(DataTable).where(DataTable.table_name == table))
            ).scalar_one_or_none()
            if record is None:
                record = DataTable(
                    # BigInteger is not SQLite's rowid alias; the explicit
                    # id is needed only by local SQLite test/control stores.
                    id=next_sqlite_id if next_sqlite_id is not None else None,
                    table_name=table,
                )
                if next_sqlite_id is not None:
                    next_sqlite_id += 1
                session.add(record)
            record.table_comment = f"Pipeline metadata for {domain}"
            record.category = "pipeline"
            record.row_count = count
            record.last_update_time = now
            record.last_update_status = "success"
            record.data_start_date = first_day
            record.data_end_date = last_day
            result[table] = {
                "row_count": count,
                "data_start_date": first_day.isoformat() if first_day else None,
                "data_end_date": last_day.isoformat() if last_day else None,
                "last_update_status": "success",
            }
        await session.commit()
    return result


def _collect_warehouse_summaries(
    warehouse_engine: Engine,
    table_specs: Sequence[tuple[str, str]],
) -> list[tuple[str, str, int, date | None, date | None]]:
    """Read exact table aggregates off the event loop and bound row transfer."""
    summaries: list[tuple[str, str, int, date | None, date | None]] = []
    with warehouse_engine.connect() as connection:
        inspector = inspect(connection)
        for table, date_column in table_specs:
            if not _IDENTIFIER_RE.fullmatch(table) or not _IDENTIFIER_RE.fullmatch(date_column):
                raise ValueError("pipeline metadata contains an invalid SQL identifier")
            if not inspector.has_table(table):
                raise LookupError(f"required warehouse table {table!r} is missing")
            row = connection.execute(
                text(
                    f"SELECT COUNT(*), MIN(`{date_column}`), MAX(`{date_column}`) "  # noqa: S608
                    f"FROM `{table}`"  # nosec B608
                )
            ).one()
            summaries.append((table, date_column, int(row[0]), _as_date(row[1]), _as_date(row[2])))
    return summaries


def build_pipeline_metadata_hook(
    warehouse_engine: Engine,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    domain: str,
    sources: Sequence[str],
) -> Callable[[PipelineContext], Awaitable[object]]:
    """Bind the async metadata refresh to the runner's hook signature."""
    source_tuple = tuple(sources)

    async def refresh(_context: PipelineContext) -> dict[str, dict[str, object]]:
        return await refresh_pipeline_metadata(
            warehouse_engine,
            session_maker,
            domain=domain,
            sources=source_tuple,
        )

    return refresh


def _as_date(value: object) -> date | None:
    """Convert a SQL date/datetime/text aggregate to a date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
