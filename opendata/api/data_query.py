"""Data query REST endpoints (design §10.1, milestone A4.9).

The read-only interface over the warehouse:

```
GET /api/v1/data/{asset_class}/{domain}     # dwd by default, ods on request
GET /api/v1/data/catalog                    # per domain: coverage, range, sources, quality
GET /api/v1/data/domains/{domain}/freshness
GET /api/v1/data/domains/{domain}/diff-report
```

Parameter safety follows the design: ``layer`` / ``adjust`` are
enum-typed (FastAPI answers 422 on anything else), ``fields`` is
whitelisted against the real table columns (400 otherwise), symbols
travel as bind parameters, and the query layer always applies a
window. ``adjust`` synthesizes qfq/hfq from Bar + AdjustFactor (D10);
until the stock-adjust factor table lands it answers 501 with the
reason instead of returning a silently wrong series.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from opendata.api.dependencies import (  # FastAPI resolves them at runtime
    CurrentPrincipal,
    Principal,
    require_domain_access,
)
from opendata.api.schemas import APIResponse
from opendata.data.domains import contract_model, dwd_table, ods_table, require_domain
from opendata.data.registry import get_registry
from opendata.pipeline.alert_matrix import registered_legs
from opendata.pipeline.freshness import (
    STATUS_MISSING,
    check_freshness,
    freshness_field,
    ods_freshness,
)
from opendata.pipeline.query import (
    DEFAULT_PAGE_SIZE,
    EXPORT_BATCH_ROWS,
    EXPORT_MAX_ROWS,
    MAX_PAGE_SIZE,
    DataQuery,
    apply_adjust_to_rows,
    build_data_select,
    resolve_time_field,
    validate_fields,
)
from opendata.pipeline.trading_calendar import resolve_calendar
from opendata.utils.serialization import serialize_for_json

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy import Engine

router = APIRouter()

#: Adjust factor table of the stock-adjust domain (A4.1 scope: pending).
FACTOR_TABLE = "dwd_stock_adjust"

#: Cross-check detail table the catalog's quality flag counts.
DIFF_TABLE = "dq_diff_report"


@lru_cache(maxsize=1)
def get_warehouse_engine() -> Engine:
    """Process-wide sync engine of the data warehouse.

    The pipeline modules (freshness, partitions, writers) are sync;
    the endpoints hand them to ``asyncio.to_thread`` so the event loop
    stays free.
    """
    from opendata.core.config import settings

    return create_engine(settings.data_database_url, poolclass=NullPool)


@router.get("/catalog")
async def data_catalog(
    principal: CurrentPrincipal,
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """List domains with coverage, range, per-source freshness and quality (FR-20).

    One row is a *domain*, not a capability leg: ``symbol_count`` /
    ``time_range`` / ``quality`` describe the merged dwd table, while
    ``sources`` carries what each source itself last delivered. A leg
    whose source has no field mapping for the domain is reported as
    ``unmapped`` with the reason - it is not measured with another
    leg's column and it is not silently dropped.

    An API key only sees the domains its scopes cover.
    """
    expected = await _expected_data_date(engine)
    capabilities = get_registry().capabilities()
    verified: dict[tuple[str, str], bool] = {
        (cap.domain, cap.source): cap.verified for cap in capabilities
    }
    asset_class: dict[str, str] = {cap.domain: cap.asset_class for cap in capabilities}
    diff_reports = await _diff_report_counts(engine)
    rows: list[dict[str, Any]] = []
    for domain, sources in registered_legs().items():
        if not principal.allows_domain(domain):
            continue
        table = dwd_table(domain)
        freshness = await _freshness(engine, domain, table, expected=expected)
        coverage = await _coverage_facts(engine, domain, table)
        status = "missing" if freshness is None else freshness["status"]
        rows.append(
            {
                "domain": domain,
                "asset_class": asset_class.get(domain, "unrouted"),
                "display_name": _display_name(domain),
                "layer": "dwd",
                "table": table,
                "freshness_field": None if coverage is None else coverage["field"],
                "latest": None if freshness is None else _iso(freshness["latest"]),
                "lag_days": None if freshness is None else freshness["lag_days"],
                "status": status,
                "coverage": None if coverage is None else coverage["facts"],
                "quality": None
                if coverage is None
                else _quality(coverage["facts"], diff_reports, domain),
                "sources": [
                    await _source_leg(
                        engine,
                        domain,
                        source,
                        verified=verified.get((domain, source), False),
                        expected=expected,
                    )
                    for source in sources
                ],
            }
        )
    return APIResponse(
        success=True,
        message="success",
        data={
            "domains": rows,
            "expected_data_date": expected.isoformat(),
            "domains_total": len(rows),
            "source_legs_total": sum(len(row["sources"]) for row in rows),
        },
    )


@router.get("/domains/{domain}/freshness")
async def domain_freshness(
    domain: str,
    principal: CurrentPrincipal,
    source: str | None = Query(None, description="Source for the ods layer"),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Report how current one domain's data is (design §9.4).

    The payload carries ``expected_data_date`` next to ``lag_days``: a lag
    without the date it was measured against cannot be checked, and this is
    the door a caller uses to check one domain without reading the catalog.
    """
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    table = ods_table(domain, source) if source else dwd_table(domain)
    expected = await _expected_data_date(engine)
    freshness = await _freshness(engine, domain, table, source=source, expected=expected)
    if freshness is None:
        return APIResponse(
            success=True,
            message="no data",
            data={
                "domain": domain,
                "source": source,
                "status": STATUS_MISSING,
                "latest": None,
                "expected_data_date": expected.isoformat(),
            },
        )
    return APIResponse(
        success=True,
        message="success",
        data={
            "domain": domain,
            "source": source,
            "field": freshness["field"],
            "latest": _iso(freshness["latest"]),
            "lag_days": freshness["lag_days"],
            "status": freshness["status"],
            "expected_data_date": expected.isoformat(),
        },
    )


@router.get("/domains/{domain}/diff-report")
async def domain_diff_report(
    domain: str,
    principal: CurrentPrincipal,
    batch_id: str | None = Query(None, description="Filter one cross-check batch"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Query the cross-check differences of one domain (design §8.2)."""
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    sql = (
        "SELECT batch_id, source_a, source_b, biz_key, field, value_a, value_b, "
        "deviation, verdict, checked_at FROM `dq_diff_report` WHERE `domain` = :domain"
    )
    params: dict[str, object] = {"domain": domain, "limit": limit}
    if batch_id:
        sql += " AND `batch_id` = :batch_id"
        params["batch_id"] = batch_id
    sql += " ORDER BY `checked_at` DESC, `biz_key` LIMIT :limit"
    try:
        rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
    except Exception as exc:  # warehouse table not migrated yet
        logger.warning("diff report unavailable for {}: {}", domain, exc)
        rows = []
    return APIResponse(
        success=True, message="success", data={"domain": domain, "rows": rows, "count": len(rows)}
    )


@router.get("/{asset_class}/{domain}")
async def query_domain_data(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    symbols: str | None = Query(None, description="Comma separated symbols"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    source: str = Query("auto"),
    layer: Literal["dwd", "ods"] = Query("dwd"),
    adjust: Literal["none", "qfq", "hfq"] = Query("none"),
    fields: str | None = Query(None, description="Comma separated field filter"),
    period: str | None = Query(None),
    report_date: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Query one domain's data (design §10.1)."""
    query = await _validated_query(
        engine,
        principal,
        asset_class=asset_class,
        domain=domain,
        symbols=symbols,
        start=start,
        end=end,
        source=source,
        layer=layer,
        adjust=adjust,
        fields=fields,
        period=period,
        report_date=report_date,
        page=page,
        page_size=page_size,
    )
    table = ods_table(domain, source) if layer == "ods" else dwd_table(domain)
    columns = await _table_columns(engine, table)
    try:
        sql, params = build_data_select(query, table=table, columns=columns, key=_key(domain))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
    except Exception as exc:
        logger.error("data query failed for {}/{}: {}", asset_class, domain, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"query failed: {exc}",
        ) from exc
    if adjust != "none":
        try:
            rows = await _adjusted_rows(engine, domain, rows, adjust)
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(
        success=True,
        message="success",
        data={
            "domain": domain,
            "asset_class": asset_class,
            "layer": layer,
            "source": source,
            "adjust": adjust,
            "columns": await _selected_columns(engine, table, query, domain),
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "count": len(rows),
        },
    )


@router.get("/{asset_class}/{domain}/export")
async def export_domain_data(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    symbols: str | None = Query(None, description="Comma separated symbols"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    source: str = Query("auto"),
    layer: Literal["dwd", "ods"] = Query("dwd"),
    adjust: Literal["none", "qfq", "hfq"] = Query("none"),
    fields: str | None = Query(None, description="Comma separated field filter"),
    period: str | None = Query(None),
    report_date: date | None = Query(None),
    limit: int = Query(EXPORT_MAX_ROWS, ge=1, le=EXPORT_MAX_ROWS),
    engine: Engine = Depends(get_warehouse_engine),
) -> StreamingResponse:
    """Stream one domain's rows as CSV (design §10.1, AC-11).

    Same parameters and the same safety rules as the JSON query -
    field whitelist, enum layers/adjust, bound symbols, always a
    window - but read in batches so a large export does not have to fit
    in memory. Cells go through :func:`serialize_for_csv`, which
    neutralizes spreadsheet formula prefixes (``=``/``+``/``-``/``@``):
    a data source must not be able to run code in the analyst's Excel.
    """
    query = await _validated_query(
        engine,
        principal,
        asset_class=asset_class,
        domain=domain,
        symbols=symbols,
        start=start,
        end=end,
        source=source,
        layer=layer,
        adjust=adjust,
        fields=fields,
        period=period,
        report_date=report_date,
        page=1,
        page_size=min(limit, EXPORT_BATCH_ROWS),
    )
    table = ods_table(domain, source) if layer == "ods" else dwd_table(domain)
    columns = await _table_columns(engine, table)
    # The whitelist runs here, not inside the generator: once the
    # response has started streaming there is no way left to answer 400.
    try:
        selected = validate_fields(query.fields, available=columns, always_include=_key(domain))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    filename = f"{asset_class}_{domain}.csv"
    return StreamingResponse(
        _csv_stream(
            engine,
            query,
            table=table,
            columns=columns,
            key=_key(domain),
            selected=selected,
            limit=limit,
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _csv_stream(
    engine: Engine,
    query: DataQuery,
    *,
    table: str,
    columns: list[str],
    key: tuple[str, ...],
    selected: list[str],
    limit: int,
) -> AsyncIterator[str]:
    """Yield the CSV text of a query, one batch at a time.

    Args:
        engine: Warehouse engine.
        query: The validated query (its page fields are driven here).
        table: Warehouse table.
        columns: Table columns.
        key: Business-key columns (ordering and always-selected fields).
        selected: Columns to write, already whitelisted by the caller.
        limit: Row ceiling of the export.

    Yields:
        CSV fragments: a header first, then one buffer per batch.
    """
    import csv
    import io

    from opendata.utils.serialization import serialize_for_csv

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(selected)
    yield buffer.getvalue()

    written = 0
    page = 1
    while written < limit:
        batch = min(EXPORT_BATCH_ROWS, limit - written)
        page_query = replace(query, page=page, page_size=batch)
        try:
            sql, params = build_data_select(
                page_query,
                table=table,
                columns=columns,
                key=key,
                max_rows=EXPORT_BATCH_ROWS,
            )
            rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
        except ValueError as exc:
            logger.error(f"export of {table} failed: {exc}")
            return
        if not rows:
            return
        if query.adjust != "none":
            try:
                rows = await _adjusted_rows(engine, query.domain, rows, query.adjust)
            except (NotImplementedError, ValueError) as exc:
                logger.error(f"export adjust failed for {query.domain}: {exc}")
                return
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for row in rows:
            writer.writerow([serialize_for_csv(row.get(column)) for column in selected])
        yield buffer.getvalue()
        written += len(rows)
        page += 1


async def _validated_query(
    engine: Engine,
    principal: Principal,
    *,
    asset_class: str,
    domain: str,
    symbols: str | None,
    start: date | None,
    end: date | None,
    source: str,
    layer: str,
    adjust: str,
    fields: str | None,
    period: str | None,
    report_date: date | None,
    page: int,
    page_size: int,
) -> DataQuery:
    """Run every guard the data endpoints share and build the query.

    Args:
        engine: Warehouse engine.
        principal: The authenticated caller.
        asset_class: Asset class segment of the path.
        domain: Domain identifier.
        symbols: Comma separated symbols.
        start: Inclusive start date.
        end: Inclusive end date.
        source: A registered leg of the domain, or ``auto``.
        layer: ``dwd`` or ``ods``.
        adjust: ``none`` / ``qfq`` / ``hfq``.
        fields: Comma separated field filter.
        period: Period filter for financial domains.
        report_date: Report-date filter for financial domains.
        page: One-based page number.
        page_size: Rows per page.

    Returns:
        The validated query.

    Raises:
        HTTPException: 400/403/404 exactly as the JSON endpoint answers.
    """
    # Scope comes first: a key that may not read the domain gets the
    # same 403 whether or not the domain exists.
    require_domain_access(principal, domain)
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if layer == "ods" and source == "auto":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="layer=ods needs an explicit source (auto is the merged dwd view)",
        )
    caps = [
        capability for capability in get_registry().capabilities() if capability.domain == domain
    ]
    registered = {capability.asset_class for capability in caps}
    if not registered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"domain {domain!r} has no registered capability",
        )
    if asset_class not in registered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"domain {domain!r} is not registered under asset class {asset_class!r}",
        )
    # An unregistered source would otherwise be answered with the merged
    # table while the payload echoes the name the caller asked for.
    legs = {"auto", *(capability.source for capability in caps)}
    if source not in legs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"source {source!r} is not a registered leg of {domain!r}: "
            f"{', '.join(sorted(legs))}",
        )
    table = ods_table(domain, source) if layer == "ods" else dwd_table(domain)
    if not await _table_columns(engine, table):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"table {table!r} is not available"
        )
    return DataQuery(
        domain=domain,
        layer=layer,
        source=source,
        symbols=tuple(item.strip() for item in symbols.split(",") if item.strip())
        if symbols
        else (),
        start=start,
        end=end,
        fields=tuple(item.strip() for item in fields.split(",") if item.strip()) if fields else (),
        page=page,
        page_size=page_size,
        adjust=adjust,
        period=period,
        report_date=report_date,
    )


async def _adjusted_rows(engine: Engine, domain: str, rows: list[dict], method: str) -> list[dict]:
    """Synthesize an adjusted series (D10) or explain why it cannot."""
    if domain != "stock_daily":
        raise ValueError(f"adjust is only defined for bar series; {domain!r} has none")
    factors = await asyncio.to_thread(
        _factor_rows, engine, [str(row.get("symbol")) for row in rows]
    )
    if factors is None:
        raise NotImplementedError(
            "adjust factors are not available yet: the stock-adjust factor table "
            f"({FACTOR_TABLE}) lands with the adjustment domain (see A2.5/A4.1 scope)"
        )
    return apply_adjust_to_rows(domain, rows, method=method, factors=factors)


def _factor_rows(engine: Engine, symbols: list[str]) -> list[dict] | None:
    """Read the factor rows of the queried symbols, None when absent.

    The rows are serialized with the same helper as the queried bars:
    the adjust synthesis keys on (symbol, trade_date), and a raw MySQL
    DATE object would never match the ISO string the query layer
    returns, silently failing every adjustment.
    """
    if not symbols:
        return []
    names = ", ".join(f":symbol_{index}" for index in range(len(symbols)))
    params = {f"symbol_{index}": symbol for index, symbol in enumerate(symbols)}
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT `symbol`, `trade_date`, `qfq_factor`, `hfq_factor` "
                    f"FROM `{FACTOR_TABLE}` WHERE `symbol` IN ({names})"
                ),
                params,
            )
            columns = list(result.keys())
            return [_serialize_row(columns, row) for row in result.fetchall()]
    except Exception:  # table absent: the caller decides the status code
        return None


def _fetch_rows(engine: Engine, sql: str, params: dict) -> list[dict]:
    """Run a read query and serialize its rows (sync, for to_thread)."""
    with engine.connect() as connection:
        result = connection.execute(text(sql), params)
        columns = list(result.keys())
        return [_serialize_row(columns, row) for row in result.fetchall()]


async def _table_columns(engine: Engine, table: str) -> list[str]:
    """Columns of a warehouse table, empty when it does not exist."""
    return await asyncio.to_thread(_inspect_columns, engine, table)


def _inspect_columns(engine: Engine, table: str) -> list[str]:
    """Inspector helper (sync)."""
    from sqlalchemy import inspect

    try:
        with engine.connect() as connection:
            return [column["name"] for column in inspect(connection).get_columns(table)]
    except Exception:
        return []


async def _freshness(
    engine: Engine,
    domain: str,
    table: str,
    *,
    source: str | None = None,
    expected: date,
) -> dict | None:
    """Freshness facts of a table, None when they cannot be measured.

    ``expected`` comes from the trading calendar (A4.7), so the lag is
    measured against the last date that should have data rather than
    against the wall clock - a weekend is not a two-day outage.
    """
    try:
        resolve_time_field(domain)
    except ValueError:
        return None
    try:
        if source:
            report = await asyncio.to_thread(
                ods_freshness, engine, domain, source, table=table, expected=expected
            )
        else:
            report = await asyncio.to_thread(
                check_freshness,
                engine,
                domain=domain,
                source=None,
                table=table,
                expected=expected,
            )
    except Exception as exc:
        logger.debug("freshness unavailable for {}: {}", table, exc)
        return None
    if report.status == STATUS_MISSING:
        return None
    return {
        "field": report.field,
        "latest": report.latest,
        "lag_days": report.lag_days,
        "status": report.status,
    }


async def _source_leg(
    engine: Engine,
    domain: str,
    source: str,
    *,
    verified: bool,
    expected: date,
) -> dict:
    """One source's own freshness reading for the catalog.

    Args:
        engine: Warehouse engine.
        domain: Domain identifier.
        source: Source identifier.
        verified: Whether the registry marks this leg verified.
        expected: Date the data should have reached.

    Returns:
        The leg row. ``unmapped`` is a measured state (the source has no
        field mapping, so no column of its own can be read), never a
        borrowed reading from another leg and never a silent drop.
    """
    table = ods_table(domain, source)
    base = {"source": source, "verified": verified, "table": table}
    try:
        report = await asyncio.to_thread(
            ods_freshness, engine, domain, source, table=table, expected=expected
        )
    except LookupError as exc:
        return {**base, "status": "unmapped", "reason": str(exc), "latest": None, "lag_days": None}
    return {
        **base,
        "status": report.status,
        "reason": None,
        "latest": _iso(report.latest),
        "lag_days": report.lag_days,
    }


async def _coverage_facts(engine: Engine, domain: str, table: str) -> dict | None:
    """Coverage/range/quality facts of one table, None when unmeasurable."""
    try:
        field = freshness_field(domain)
    except (LookupError, ValueError):
        return None
    return await asyncio.to_thread(_read_coverage, engine, table, field)


def _read_coverage(engine: Engine, table: str, field: str) -> dict | None:
    """Run the coverage aggregate (sync).

    The subject columns come from the table's own primary key rather than
    a name heuristic: the dwd DDL declares the business key (AC-8), so
    "覆盖标的数" counts what actually identifies a row minus the date the
    range is measured over. A table whose key is only its date column has
    no subject to count, and says so with ``symbol_columns: null``.
    """
    from sqlalchemy.exc import SQLAlchemyError

    columns = _inspect_columns(engine, table)
    if field not in columns:
        return None
    subjects = [column for column in _inspect_primary_key(engine, table) if column != field]
    flagged = "SUM(`_diff_flag`)" if "_diff_flag" in columns else "NULL"
    if subjects:
        joined = ", ".join(f"`{column}`" for column in subjects)
        symbols = f"(SELECT COUNT(*) FROM (SELECT DISTINCT {joined} FROM `{table}`) AS `subj`)"
    else:
        symbols = "NULL"
    sql = (
        f"SELECT COUNT(*) AS `rows`, MIN(`{field}`) AS `start`, MAX(`{field}`) AS `end`, "
        f"{flagged} AS `diff_flagged`, {symbols} AS `symbols` FROM `{table}`"
    )
    try:
        with engine.connect() as connection:
            row = connection.execute(text(sql)).one()
    except SQLAlchemyError as exc:
        logger.debug("coverage of {} unavailable: {}", table, exc)
        return None
    return {
        "field": field,
        "symbol_columns": subjects or None,
        "facts": {
            "rows": int(row[0]),
            "start": _iso(row[1]),
            "end": _iso(row[2]),
            "diff_flagged": None if row[3] is None else int(row[3]),
            "symbols": None if row[4] is None else int(row[4]),
        },
    }


def _inspect_primary_key(engine: Engine, table: str) -> list[str]:
    """Business-key columns of a warehouse table, empty when unknown."""
    from sqlalchemy import inspect

    try:
        with engine.connect() as connection:
            constraint = inspect(connection).get_pk_constraint(table)
    except Exception:
        return []
    return [str(column) for column in constraint.get("constrained_columns") or []]


async def _diff_report_counts(engine: Engine) -> dict[str, int] | None:
    """Per-domain ``dq_diff_report`` row counts, None when the table is absent.

    None and an empty mapping are different readings: the first says the
    cross-check detail was never migrated, the second says it is there and
    no domain has any.
    """
    from sqlalchemy.exc import SQLAlchemyError

    def read() -> dict[str, int]:
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT `domain`, COUNT(*) AS `rows` FROM `{DIFF_TABLE}` GROUP BY `domain`")
            ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    try:
        return await asyncio.to_thread(read)
    except SQLAlchemyError as exc:
        logger.debug("diff report counts unavailable: {}", exc)
        return None


def _quality(facts: dict, diff_reports: dict[str, int] | None, domain: str) -> dict:
    """The catalog's 质量标记 for one domain.

    ``_diff_flag`` is the marker the dwd merge leaves on a row the two
    sources disagreed about (design §8.3), so the flag reads the merged
    table first and the cross-check detail second; ``unmeasured`` is only
    used when the marker column itself is absent - an empty table is
    ``clean`` about flags and ``missing`` about freshness, two separate
    readings on purpose.
    """
    flagged = facts.get("diff_flagged")
    reports = None if diff_reports is None else diff_reports.get(domain, 0)
    if flagged is None:
        flag = "unmeasured"
    elif flagged > 0 or (reports or 0) > 0:
        flag = "flagged"
    else:
        flag = "clean"
    return {"diff_flagged": flagged, "diff_report_rows": reports, "flag": flag}


async def _expected_data_date(engine: Engine, *, on: date | None = None) -> date:
    """Latest date that should carry data, per the trading calendar (A4.7).

    Resolved once per request instead of per table: the catalog endpoint
    measures every domain, and each measurement would otherwise re-read
    the calendar.

    Args:
        engine: Warehouse engine.
        on: Reference date; defaults to today (tests pin it so a weekend
            cannot change what the assertion means).

    Returns:
        The date the freshness lag is measured against.
    """
    calendar = await asyncio.to_thread(resolve_calendar, engine)
    return calendar.expected_data_date(on or date.today())


def _key(domain: str) -> tuple[str, ...]:
    """Business-key columns derived from a domain's contract model.

    The contract models carry no key metadata yet, so the key is the
    identifier fields plus the date field: exactly the columns that
    identify a row and that the ordering and the always-included
    selection need.
    """
    date_fields = {"trade_date", "ex_date", "report_period", "as_of", "date"}
    model = contract_model(domain)
    return tuple(
        name
        for name in model.model_fields
        if name == "symbol" or name.endswith("_symbol") or name in date_fields
    )


def _display_name(domain: str) -> str:
    """Frontend display name of a domain."""
    from opendata.data.domains import display_name

    return display_name(domain)


def _serialize_row(columns: list[str], row: Sequence[object]) -> dict:
    """Serialize one row for JSON."""
    return {column: serialize_for_json(row[index]) for index, column in enumerate(columns)}


def _iso(value: object) -> str | None:
    """ISO rendering of a date-ish value.

    Drivers disagree about aggregates: MySQL types ``MIN(col)`` as a DATE
    and hands back a ``date``, while SQLite returns the stored text. The
    catalog's range must read the same either way, and a text date is
    already ISO.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()[:10]
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        raise TypeError(f"{type(value).__name__} has no ISO rendering")
    return str(isoformat())


async def _selected_columns(engine: Engine, table: str, query: DataQuery, domain: str) -> list[str]:
    """Selected columns after the field whitelist (for the response)."""
    from opendata.pipeline.query import validate_fields

    columns = await _table_columns(engine, table)
    return validate_fields(query.fields, available=columns, always_include=_key(domain))
