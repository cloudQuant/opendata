"""REST query layer (design §10.1, milestone A4.9).

Turns a validated request into a paginated SELECT over the warehouse,
with the parameter-safety rules from the design:

* identifiers are validated before quoting - a table name can never
  break out of its backticks;
* ``symbols`` become bind parameters (``IN (:symbol_0, ...)``), never
  string interpolation; an injection attempt stays a literal;
* ``fields`` are whitelisted against the table's real columns;
* ``layer`` / ``source`` / ``adjust`` are enum-validated upstream;
* a default time window is always applied, so an unfiltered request
  cannot scan every partition;
* ``adjust`` synthesizes qfq/hfq series from Bar + AdjustFactor (D10)
  instead of reading pre-adjusted columns - volume and amount are
  copied unchanged, and a bar without its factor row fails closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import Bar

#: Word characters only (Unicode letters included), never a backtick.
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)

#: Query layers (design §8.3: REST defaults to dwd).
LAYERS = ("dwd", "ods")
#: Adjust methods (D10; default keeps the raw series).
ADJUST_METHODS = ("none", "qfq", "hfq")
#: Default window when the caller gives no range (avoids full scans).
DEFAULT_WINDOW_DAYS = 365
#: Hard page-size ceiling of the interactive query.
MAX_PAGE_SIZE = 1_000
#: Default page size.
DEFAULT_PAGE_SIZE = 200
#: Rows one export request may pull (design §10.1 "异步导出" cap).
EXPORT_MAX_ROWS = 200_000
#: Rows an export reads per round trip.
EXPORT_BATCH_ROWS = 10_000


@dataclass(frozen=True)
class DataQuery:
    """A validated data query.

    Attributes:
        domain: Registered domain identifier.
        layer: ``dwd`` (default) or ``ods``.
        source: Source identifier; ``auto`` for the merged view.
        symbols: Symbol filter (empty means no filter).
        start: Window start (defaulted when absent).
        end: Window end (defaulted when absent).
        fields: Requested contract fields (empty means all).
        page: 1-based page number.
        page_size: Rows per page (capped at ``MAX_PAGE_SIZE``).
        adjust: ``none`` / ``qfq`` / ``hfq``.
        period: Optional period filter (financial domains).
        report_date: Optional report-date filter (financial domains).
    """

    domain: str
    layer: str = "dwd"
    source: str = "auto"
    symbols: tuple[str, ...] = ()
    start: date | None = None
    end: date | None = None
    fields: tuple[str, ...] = ()
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    adjust: str = "none"
    period: str | None = None
    report_date: date | None = None


def resolve_time_field(domain: str) -> str:
    """Return the date column used for the window filter.

    The same rule as the freshness check: the contract model's first
    ``date`` field (``trade_date`` for bars, ``as_of`` for index
    membership, ...).

    Args:
        domain: Registered domain identifier.

    Returns:
        The date column name.

    Raises:
        LookupError: If the domain is unknown.
        ValueError: If the contract model has no date field.
    """
    from opendata.pipeline.freshness import freshness_field

    return freshness_field(domain)


def window_bounds(query: DataQuery, *, today: date) -> tuple[date, date]:
    """Resolve the effective window, applying the default when absent.

    Args:
        query: The validated query.
        today: Current date (injected for deterministic tests).

    Returns:
        The inclusive ``(start, end)`` bounds.

    Raises:
        ValueError: If ``start`` is after ``end`` (fail closed).
    """
    end = query.end or today
    start = query.start or (end - timedelta(days=DEFAULT_WINDOW_DAYS))
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    return start, end


def validate_fields(
    requested: Sequence[str],
    *,
    available: Sequence[str],
    always_include: Sequence[str] = (),
) -> list[str]:
    """Whitelist the requested fields against the table's columns.

    The business key (``always_include``) is always part of the
    selection - a projection without the key columns would be
    uninterpretable - so ``fields`` filters the value columns only.

    Args:
        requested: Requested field names (empty means every column).
        available: Columns the table actually has, in table order.
        always_include: Columns that stay selected regardless.

    Returns:
        The selected columns in table order.

    Raises:
        ValueError: If a requested field is not a table column (the
            endpoint maps this to a 400).
    """
    if not requested:
        return list(available)
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"unknown fields {unknown}; available: {list(available)}")
    keep = set(requested) | set(always_include)
    return [column for column in available if column in keep]


def build_data_select(
    query: DataQuery,
    *,
    table: str,
    columns: Sequence[str],
    key: Sequence[str],
    today: date | None = None,
    max_rows: int = MAX_PAGE_SIZE,
) -> tuple[str, dict[str, object]]:
    """Build the paginated SELECT and its bind parameters.

    Args:
        query: The validated query.
        table: Target table (validated before quoting).
        columns: Columns the table has, in table order.
        key: Business-key columns used for deterministic ordering.
        today: Current date (injected for deterministic tests).
        max_rows: Page-size ceiling; the interactive query keeps the
            default, the export raises it deliberately.

    Returns:
        The SQL text and its parameters.

    Raises:
        ValueError: If the layer is unknown, the table name is unsafe,
            the page is below 1, or the field selection is unknown.
    """
    if query.layer not in LAYERS:
        raise ValueError(f"unknown layer {query.layer!r}; expected one of {LAYERS}")
    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid SQL identifier {table!r}")
    if query.page < 1:
        raise ValueError(f"page must be >= 1, got {query.page}")
    start, end = window_bounds(query, today=today or date.today())
    selected = validate_fields(query.fields, available=columns, always_include=key)
    time_field = resolve_time_field(query.domain)
    page_size = min(query.page_size, max_rows)
    params: dict[str, object] = {
        "start": start,
        "end": end,
        "limit": page_size,
        "offset": (query.page - 1) * page_size,
    }
    where = [f"`{time_field}` >= :start", f"`{time_field}` <= :end"]
    if query.symbols:
        names = []
        for index, symbol in enumerate(query.symbols):
            name = f"symbol_{index}"
            params[name] = symbol
            names.append(f":{name}")
        where.append(f"`symbol` IN ({', '.join(names)})")
    order = ", ".join(f"`{column}`" for column in key) or f"`{time_field}`"
    select_list = ", ".join(f"`{column}`" for column in selected)
    sql = (
        f"SELECT {select_list} FROM `{table}` "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {order} LIMIT :limit OFFSET :offset"
    )
    return sql, params


def apply_adjust_to_rows(
    domain: str,
    rows: Sequence[dict[str, object]],
    *,
    method: str,
    factors: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    """Synthesize an adjusted series from raw rows and factors (D10).

    Args:
        domain: Registered domain identifier.
        rows: Raw rows as read from the warehouse.
        method: ``none`` / ``qfq`` / ``hfq``.
        factors: Cumulative factor rows (``symbol``, ``trade_date``,
            ``qfq_factor``, ``hfq_factor``).

    Returns:
        The rows with adjusted prices (``none`` returns them as-is).

    Raises:
        ValueError: If the method is unknown or a bar has no factor
            row (fail closed: a partially adjusted series is worse
            than an error).
    """
    if method not in ADJUST_METHODS:
        raise ValueError(f"unknown adjust method {method!r}; expected one of {ADJUST_METHODS}")
    if method == "none" or not rows:
        return [dict(row) for row in rows]
    from opendata.data.adjust import apply_adjust
    from opendata.data.models import AdjustFactor, Bar

    factor_index: dict[tuple, dict[str, object]] = {}
    for entry in factors:
        factor_index[(entry.get("symbol"), entry.get("trade_date"))] = entry
    bars: list[Bar] = []
    for row in rows:
        biz_key = (row.get("symbol"), row.get("trade_date"))
        factor = factor_index.get(biz_key)
        if factor is None:
            raise ValueError(
                f"no adjust factor for {biz_key!r}; cannot synthesize a {method} series"
            )
        bars.append(
            Bar(
                symbol=str(row["symbol"]),
                trade_date=row["trade_date"],  # type: ignore[arg-type]  # typed by the caller
                open=float(row["open"]),  # type: ignore[arg-type]
                high=float(row["high"]),  # type: ignore[arg-type]
                low=float(row["low"]),  # type: ignore[arg-type]
                close=float(row["close"]),  # type: ignore[arg-type]
                volume=float(row["volume"]),  # type: ignore[arg-type]
                amount=float(row["amount"]),  # type: ignore[arg-type]
            )
        )
    adjusted = apply_adjust(
        bars,
        [
            AdjustFactor(
                symbol=str(factor["symbol"]),
                trade_date=factor["trade_date"],  # type: ignore[arg-type]
                qfq_factor=float(factor.get("qfq_factor", 1.0)),  # type: ignore[arg-type]
                hfq_factor=float(factor.get("hfq_factor", 1.0)),  # type: ignore[arg-type]
            )
            for factor in factors
        ],
        method,
    )
    by_key: dict[tuple[object, object], Bar] = {
        (bar.symbol, bar.trade_date): bar for bar in adjusted
    }
    output: list[dict[str, object]] = []
    for row in rows:
        # The adjusted bars carry real ``date`` objects (pydantic
        # coerced them); the query rows still hold the ISO strings the
        # JSON serializer produced, so the keys are normalized here.
        trade_date = row.get("trade_date")
        if isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)
        bar = by_key[(row.get("symbol"), trade_date)]
        adjusted_row = dict(row)
        adjusted_row.update(
            {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
        )
        output.append(adjusted_row)
    return output
