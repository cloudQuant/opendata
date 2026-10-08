"""Read mapped ODS watermarks and derive bounded per-symbol windows."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text

from opendata.data.domains import ods_table
from opendata.data.mapping import require_domain_mapping
from opendata.pipeline.query import resolve_time_field
from opendata.pipeline.runner import Window

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Engine

_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)


def read_ods_watermarks(
    engine: Engine,
    domain: str,
    source: str,
    symbols: Sequence[str],
    *,
    end: date,
    batch_size: int = 200,
) -> dict[str, date]:
    """Return latest mapped ODS date per requested symbol, capped at ``end``.

    The table's own mapped source columns are used for both identity and date.
    Exact symbol values and suffixed spellings (for example ``600519.SH``)
    are considered, then normalized back through the mapping. An absent ODS
    table yields an empty watermark set; malformed mappings and query errors
    propagate so the caller cannot mistake a broken read for a first load.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    requested = tuple(dict.fromkeys(str(symbol) for symbol in symbols))
    if not requested:
        return {}

    mapping = require_domain_mapping(source, domain)
    if "symbol" not in mapping.fields:
        raise LookupError(f"mapping {source!r}/{domain!r} has no symbol field")
    date_field = resolve_time_field(domain)
    if date_field not in mapping.fields:
        raise LookupError(f"mapping {source!r}/{domain!r} has no {date_field!r} date field")
    symbol_column = mapping.fields["symbol"].source_column
    date_column = mapping.fields[date_field].source_column
    table = ods_table(domain, source)
    if any(not _IDENTIFIER_RE.fullmatch(name) for name in (table, symbol_column, date_column)):
        raise ValueError("mapped ODS identifier is invalid")
    if not inspect(engine).has_table(table):
        return {}

    output: dict[str, date] = {}
    symbol_position = mapping.key.index("symbol") if "symbol" in mapping.key else -1
    if symbol_position < 0:
        raise LookupError(f"mapping {source!r}/{domain!r} has no symbol key")

    for offset in range(0, len(requested), batch_size):
        batch = requested[offset : offset + batch_size]
        clauses: list[str] = []
        params: dict[str, object] = {"end_date": end}
        for index, symbol in enumerate(batch):
            exact = f"symbol_{index}"
            prefix = f"symbol_prefix_{index}"
            escaped = symbol.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            params[exact] = symbol
            params[prefix] = f"{escaped}.%"
            clauses.append(
                f"(`{symbol_column}` = :{exact} OR `{symbol_column}` LIKE :{prefix} ESCAPE '!')"
            )
        sql = text(
            f"SELECT `{symbol_column}`, MAX(`{date_column}`) AS `latest_date` "  # noqa: S608
            f"FROM `{table}` WHERE `{date_column}` <= :end_date "
            f"AND ({' OR '.join(clauses)}) GROUP BY `{symbol_column}`"  # nosec B608
        )
        with engine.connect() as connection:
            rows = connection.execute(sql, params).all()
        requested_set = set(batch)
        for raw_symbol, raw_day in rows:
            if raw_symbol is None or raw_day is None:
                continue
            raw_key: list[object] = [None] * len(mapping.key)
            raw_key[symbol_position] = raw_symbol
            canonical = mapping.to_contract_key(raw_key)[symbol_position]
            if not isinstance(canonical, str) or canonical not in requested_set:
                continue
            latest = _as_date(raw_day)
            if latest <= end and latest > output.get(canonical, date.min):
                output[canonical] = latest
    return output


def effective_symbol_windows(
    symbols: Sequence[str],
    base_window: Window,
    watermarks: Mapping[str, date],
    *,
    lookback_days: int = 0,
) -> dict[str, Window | None]:
    """Build a catch-up window per symbol without extending beyond ``end``.

    Symbols without ODS rows keep the established incremental window. For
    landed symbols the next missing date is fetched with the requested
    overlap. A symbol already covered through the end has no work when the
    lookback is zero.
    """
    if lookback_days < 0:
        raise ValueError(f"lookback_days must be >= 0, got {lookback_days}")
    windows: dict[str, Window | None] = {}
    for symbol in dict.fromkeys(symbols):
        watermark = watermarks.get(symbol)
        if watermark is None:
            windows[symbol] = base_window
            continue
        if watermark >= base_window.end:
            if lookback_days == 0:
                windows[symbol] = None
                continue
            first_missing = base_window.end
        else:
            first_missing = watermark + timedelta(days=1)
        try:
            start = first_missing - timedelta(days=lookback_days)
        except OverflowError:
            start = date.min
        windows[symbol] = Window(
            start=max(date.min, min(start, base_window.end)), end=base_window.end
        )
    return windows


def union_symbol_windows(
    symbols: Sequence[str],
    source_windows: Mapping[str, Mapping[str, Window | None]],
    *,
    fallback: Window,
) -> dict[str, Window | None]:
    """Union each symbol's actual windows across source legs."""
    output: dict[str, Window | None] = {}
    for symbol in dict.fromkeys(symbols):
        windows = [
            window
            for source_map in source_windows.values()
            if (window := source_map.get(symbol)) is not None
        ]
        if not windows:
            output[symbol] = None
        else:
            output[symbol] = Window(
                start=min(window.start for window in windows),
                end=max(window.end for window in windows),
            )
    return output


def enclosing_window(symbol_windows: Mapping[str, Window | None], *, fallback: Window) -> Window:
    """Return a finite min/max range for hook APIs that still need one."""
    windows = [window for window in symbol_windows.values() if window is not None]
    if not windows:
        return fallback
    return Window(
        start=min(window.start for window in windows),
        end=max(window.end for window in windows),
    )


def _as_date(value: object) -> date:
    """Convert SQL date-like scalar values into Python dates."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value).strip()
    if len(text_value) == 8 and text_value.isdigit():
        return datetime.strptime(text_value, "%Y%m%d").date()
    return date.fromisoformat(text_value[:10])
