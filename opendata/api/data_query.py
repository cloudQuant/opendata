"""Data query REST endpoints (design §10.1, milestone A4.9).

The read-only interface over the warehouse:

```
GET /api/v1/data/{asset_class}/{domain}     # dwd by default, ods on request
GET /api/v1/data/catalog                    # capabilities + freshness
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
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from opendata.api.dependencies import CurrentUser  # noqa: TC001  # FastAPI resolves it at runtime
from opendata.api.schemas import APIResponse
from opendata.data.domains import contract_model, dwd_table, ods_table, require_domain
from opendata.data.registry import get_registry
from opendata.pipeline.freshness import STATUS_MISSING, check_freshness, ods_freshness
from opendata.pipeline.query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DataQuery,
    apply_adjust_to_rows,
    build_data_select,
    resolve_time_field,
)
from opendata.utils.serialization import serialize_for_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

router = APIRouter()

#: Adjust factor table of the stock-adjust domain (A4.1 scope: pending).
FACTOR_TABLE = "dwd_stock_adjust"


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
    current_user: CurrentUser,
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """List domains with their capabilities and freshness (FR-20)."""
    rows = []
    for capability in get_registry().capabilities():
        table = dwd_table(capability.domain)
        freshness = await _freshness(engine, capability.domain, table, source=capability.source)
        rows.append(
            {
                "domain": capability.domain,
                "asset_class": capability.asset_class,
                "source": capability.source,
                "verified": capability.verified,
                "display_name": _display_name(capability.domain),
                "layer": "dwd",
                "latest": None if freshness is None else _iso(freshness["latest"]),
                "lag_days": None if freshness is None else freshness["lag_days"],
                "status": "missing" if freshness is None else freshness["status"],
            }
        )
    return APIResponse(success=True, message="success", data={"domains": rows})


@router.get("/domains/{domain}/freshness")
async def domain_freshness(
    domain: str,
    current_user: CurrentUser,
    source: str | None = Query(None, description="Source for the ods layer"),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Report how current one domain's data is (design §9.4)."""
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    table = ods_table(domain, source) if source else dwd_table(domain)
    freshness = await _freshness(engine, domain, table, source=source)
    if freshness is None:
        return APIResponse(
            success=True,
            message="no data",
            data={"domain": domain, "source": source, "status": STATUS_MISSING, "latest": None},
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
        },
    )


@router.get("/domains/{domain}/diff-report")
async def domain_diff_report(
    domain: str,
    current_user: CurrentUser,
    batch_id: str | None = Query(None, description="Filter one cross-check batch"),
    limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
    engine: Engine = Depends(get_warehouse_engine),
) -> APIResponse:
    """Query the cross-check differences of one domain (design §8.2)."""
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
        logger.warning("diff report unavailable for %s: %s", domain, exc)
        rows = []
    return APIResponse(
        success=True, message="success", data={"domain": domain, "rows": rows, "count": len(rows)}
    )


@router.get("/{asset_class}/{domain}")
async def query_domain_data(
    asset_class: str,
    domain: str,
    current_user: CurrentUser,
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
    try:
        require_domain(domain)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if layer == "ods" and source == "auto":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="layer=ods needs an explicit source (auto is the merged dwd view)",
        )
    registered = {
        capability.asset_class
        for capability in get_registry().capabilities()
        if capability.domain == domain
    }
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
    table = ods_table(domain, source) if layer == "ods" else dwd_table(domain)
    columns = await _table_columns(engine, table)
    if not columns:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"table {table!r} is not available"
        )
    query = DataQuery(
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
    try:
        sql, params = build_data_select(query, table=table, columns=columns, key=_key(domain))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        rows = await asyncio.to_thread(_fetch_rows, engine, sql, params)
    except Exception as exc:
        logger.error("data query failed for %s/%s: %s", asset_class, domain, exc)
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
    """Read the factor rows of the queried symbols, None when absent."""
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
            return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
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
) -> dict | None:
    """Freshness facts of a table, None when they cannot be measured.

    Until the trading calendar is populated (A4.7 wires it) the
    expectation is "today", which makes the lag a coarse staleness
    signal rather than a trading-day-exact one.
    """
    try:
        resolve_time_field(domain)
    except ValueError:
        return None
    expected = date.today()
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
        logger.debug("freshness unavailable for %s: %s", table, exc)
        return None
    if report.status == STATUS_MISSING:
        return None
    return {
        "field": report.field,
        "latest": report.latest,
        "lag_days": report.lag_days,
        "status": report.status,
    }


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
    """ISO rendering of a date-ish value."""
    return None if value is None else value.isoformat()  # type: ignore[attr-defined]


async def _selected_columns(engine: Engine, table: str, query: DataQuery, domain: str) -> list[str]:
    """Selected columns after the field whitelist (for the response)."""
    from opendata.pipeline.query import validate_fields

    columns = await _table_columns(engine, table)
    return validate_fields(query.fields, available=columns, always_include=_key(domain))
