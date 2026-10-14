"""Stock-adjust dwd builder (AC-11 adjust, design D10).

Reads the ods layers - the unadjusted daily close series and the
corporate-action events - cumulates them into per-day qfq/hfq factors
(:mod:`opendata.pipeline.factors`) and upserts the result into
``dwd_stock_adjust``. Idempotent by construction: the upsert is keyed
on (symbol, trade_date), so re-running after a dump re-import heals the
previous rows instead of duplicating them.

The builder is deliberately a warehouse-to-warehouse service: factors
are a derived layer, so their truth lives in the ods data they are
computed from, and recomputing is the recovery path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import bindparam, text

from opendata.pipeline.dwd_merge import DwdWriter
from opendata.pipeline.factors import (
    FactorEvent,
    coalesce_same_day_events,
    cumulate_factors,
    validate_event,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from opendata.data.models.adjustment import AdjustFactor
    from opendata.services.corporate_events import CorporateEventVersionStore

#: Default target of the derived factor layer.
DWD_FACTOR_TABLE = "dwd_stock_adjust"
_AFFINE_FACTOR_SOURCE = "corporate-action-affine-v1"
_RATIO_FACTOR_SOURCE = "corporate-action-ratio-v1"


@dataclass(frozen=True)
class FactorWriteScope:
    """Rows committed for one symbol and their complete factor date range."""

    symbol: str
    row_count: int
    window_start: date
    window_end: date


@dataclass(frozen=True)
class FactorBuildResult:
    """What one build did.

    Attributes:
        rows_written: Factor rows upserted.
        symbols: Distinct symbols covered.
        rows_planned: Factor rows selected for an upsert, including dry runs.
        events_outside_basis: Source events outside each symbol's close range.
        legacy_rows_preserved: Existing legacy multiplication factors retained verbatim.
        write_scopes: Per-symbol committed factor row counts and full date ranges.
    """

    rows_written: int
    symbols: int
    rows_planned: int = 0
    events_outside_basis: int = 0
    legacy_rows_preserved: int = 0
    write_scopes: tuple[FactorWriteScope, ...] = ()


class FactorBuilder:
    """Cumulate ods events and closes into the stock-adjust dwd table."""

    def __init__(
        self,
        engine: Engine,
        *,
        daily_table: str,
        action_table: str,
        target_table: str = DWD_FACTOR_TABLE,
        event_store: CorporateEventVersionStore | None = None,
    ) -> None:
        """Bind the builder to its ods inputs and dwd target.

        Args:
            engine: Warehouse engine.
            daily_table: Ods table of the unadjusted daily closes
                (``ods_<domain>_<source>``).
            action_table: Ods table of the corporate-action events.
            target_table: Dwd table to write.
            event_store: Optional versioned event source. When provided, its
                unambiguous current events replace the legacy ODS action read.
        """
        self.engine = engine
        self.daily_table = daily_table
        self.action_table = action_table
        self.target_table = target_table
        self.event_store = event_store

    def build(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        dry_run: bool = False,
        affected_keys: Sequence[tuple[object, ...]] | None = None,
    ) -> FactorBuildResult:
        """Rebuild the factor table from the ods layers.

        Args:
            start: Optional date to start the written factor window.
            end: Optional window end.
            dry_run: Compute and validate without constructing a writer or writing rows.
            affected_keys: Optional stock-action keys ``(symbol, ex_date)``.
                When supplied, the builder recomputes every factor row for
                those symbols, since a changed action can alter both sides
                of the complete factor series.

        Returns:
            Rows upserted and symbols covered.

        Raises:
            ValueError: An event has no previous close or a degenerate
                ratio (fail closed - the caller decides what that means
                for the run).
        """
        _validate_window(start, end)
        affected_symbols = _affected_event_symbols(affected_keys)
        if affected_symbols == set():
            return FactorBuildResult(rows_written=0, symbols=0)
        symbol_scope = None if affected_symbols is None else sorted(affected_symbols)
        # Full source history establishes stable per-symbol anchors. The
        # requested window limits writes only; it never changes the factors.
        # An affected-key rebuild limits the source reads to complete histories
        # for those symbols, so unrelated events cannot block the rebuild.
        closes = self._read_closes(None, None, symbols=symbol_scope)
        raw_events = (
            self.event_store.load_current_events(symbols=symbol_scope)
            if self.event_store is not None
            else self._read_events(None, None, symbols=symbol_scope)
        )
        events, events_outside_basis = _events_within_close_basis(raw_events, closes)
        factors = cumulate_factors(events, closes)
        selected = [
            factor
            for factor in factors
            if (start is None or factor.trade_date >= start)
            and (end is None or factor.trade_date <= end)
            and (affected_symbols is None or factor.symbol in affected_symbols)
        ]
        if not selected:
            return FactorBuildResult(
                rows_written=0,
                symbols=0,
                events_outside_basis=events_outside_basis,
            )
        existing = self._read_existing_factors(
            start,
            end,
            sorted({factor.symbol for factor in selected}),
        )
        planned: list[dict[str, object]] = []
        legacy_rows_preserved = 0
        for factor in selected:
            row = factor.model_dump()
            old = existing.get((factor.symbol, factor.trade_date))
            is_current_computation = (
                self.event_store is not None
                and old is not None
                and old[2] == _AFFINE_FACTOR_SOURCE
                and old[3] == _RATIO_FACTOR_SOURCE
            )
            if old is None or is_current_computation:
                # Both fields are required: _with_dwd_trace() rewrites source
                # for preserved legacy rows too, while legacy_source retains
                # the legacy value provenance. Unknown provenance fails closed.
                row["legacy_source"] = _RATIO_FACTOR_SOURCE
            else:
                old_qfq, old_hfq, old_source, old_legacy_source = old
                if (
                    not math.isfinite(old_qfq)
                    or not math.isfinite(old_hfq)
                    or old_qfq <= 0
                    or old_hfq <= 0
                ):
                    raise ValueError(
                        f"existing legacy factors for {(factor.symbol, factor.trade_date)} "
                        "must be finite and positive"
                    )
                row["qfq_factor"] = old_qfq
                row["hfq_factor"] = old_hfq
                row["legacy_source"] = old_legacy_source or old_source
                legacy_rows_preserved += 1
            planned.append(row)
        frame = _with_dwd_trace(pd.DataFrame(planned))
        rows_planned = len(frame)
        symbols = int(frame["symbol"].nunique())
        if dry_run:
            return FactorBuildResult(
                rows_written=0,
                symbols=symbols,
                rows_planned=rows_planned,
                events_outside_basis=events_outside_basis,
                legacy_rows_preserved=legacy_rows_preserved,
            )
        writer = DwdWriter(self.engine)
        rows_written = writer.write(frame, table=self.target_table, key=("symbol", "trade_date"))
        if rows_written != rows_planned:
            raise RuntimeError(
                f"factor writer reported {rows_written} rows for {rows_planned} planned rows"
            )
        return FactorBuildResult(
            rows_written=rows_written,
            symbols=symbols,
            rows_planned=rows_planned,
            events_outside_basis=events_outside_basis,
            legacy_rows_preserved=legacy_rows_preserved,
            write_scopes=_factor_write_scopes(selected),
        )

    def _read_closes(
        self,
        start: date | None,
        end: date | None,
        *,
        symbols: Sequence[str] | None = None,
    ) -> list[tuple[str, date, float]]:
        """Read the unadjusted close series of the daily ods table.

        Args:
            start: Window start (None for every row).
            end: Window end.
            symbols: Optional normalized symbol scope; each symbol includes
                all exchange-suffixed source rows.

        Returns:
            ``(symbol, trade_date, close)`` rows; symbols are normalized
            by stripping the exchange suffix so event and daily tables
            join on the same identifier.
        """
        where, params = _window_clause(start, end, "trade_date")
        symbol_where, symbol_params = _symbol_clause("thscode", symbols)
        clauses = ["`adjusted` = 'none'"]
        if where:
            clauses.append(where)
        if symbol_where:
            clauses.append(symbol_where)
        params.update(symbol_params)
        sql = (
            "SELECT `thscode`, `trade_date`, `close_price` "  # noqa: S608  # nosec B608  # tbl=ods_table(); clause col literal
            f"FROM `{self.daily_table}` WHERE {' AND '.join(clauses)}"
        )
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), params)
            return [
                (
                    _normalize_symbol(row[0]),
                    _coerce_date(row[1], "trade_date"),
                    float(row[2]),
                )
                for row in result.fetchall()
            ]

    def _read_events(
        self,
        start: date | None,
        end: date | None,
        *,
        symbols: Sequence[str] | None = None,
    ) -> list[FactorEvent]:
        """Read the corporate-action events of the action ods table.

        Args:
            start: Window start (None for every event).
            end: Window end.
            symbols: Optional normalized symbol scope; each symbol includes
                all exchange-suffixed source rows.

        Returns:
            All source rows, including zero-valued rows, so duplicates can
            be checked before no-op rows are filtered within the close basis.
        """
        where, params = _window_clause(start, end, "ex_date")
        symbol_where, symbol_params = _symbol_clause("thscode", symbols)
        clauses = [clause for clause in (where, symbol_where) if clause]
        params.update(symbol_params)
        suffix = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT `thscode`, `ex_date`, `dividend_per_share`, `per_share_bonus`, "  # noqa: S608  # nosec B608  # tbl=ods_table(); clause col literal
            "`allotment_ratio`, `allotment_price`, `event_key` "
            f"FROM `{self.action_table}`{suffix}"
        )
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), params)
            events: list[FactorEvent] = []
            for row in result.fetchall():
                event = FactorEvent(
                    symbol=_normalize_symbol(row[0]),
                    ex_date=_coerce_date(row[1], "ex_date"),
                    cash_dividend=float(row[2] or 0.0),
                    bonus=float(row[3] or 0.0),
                    allotment_ratio=float(row[4] or 0.0),
                    allotment_price=float(row[5] or 0.0),
                    event_key=None if row[6] is None else str(row[6]),
                )
                events.append(event)
            return events

    def _read_existing_factors(
        self,
        start: date | None,
        end: date | None,
        symbols: list[str],
    ) -> dict[tuple[str, date], tuple[float, float, str, str | None]]:
        """Read target rows that will be rewritten to preserve legacy factors."""
        if not symbols:
            return {}
        where, params = _window_clause(start, end, "trade_date")
        clauses = ["`symbol` IN :symbols"]
        if where:
            clauses.append(where)
        sql = (
            f"SELECT `symbol`, `trade_date`, `qfq_factor`, `hfq_factor`, `source`, `legacy_source` "  # noqa: S608  # nosec B608  # tbl=DWD_FACTOR_TABLE default; :binds
            f"FROM `{self.target_table}` WHERE "
            f"{' AND '.join(clauses)}"
        )
        statement = text(sql).bindparams(bindparam("symbols", expanding=True))
        with self.engine.connect() as connection:
            result = connection.execute(statement, {**params, "symbols": symbols})
            existing: dict[tuple[str, date], tuple[float, float, str, str | None]] = {}
            for row in result.fetchall():
                key = (_normalize_symbol(row[0]), _coerce_date(row[1], "trade_date"))
                if key in existing:
                    raise ValueError(f"duplicate existing adjustment row for {key}")
                existing[key] = (
                    float(row[2]),
                    float(row[3]),
                    str(row[4]),
                    None if row[5] is None else str(row[5]),
                )
            return existing


def _validate_window(start: date | None, end: date | None) -> None:
    """Reject malformed date bounds before any engine access."""
    for name, value in (("start", start), ("end", end)):
        if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
            raise ValueError(f"{name} must be a date, not a datetime")
    if start is not None and end is not None and start > end:
        raise ValueError("start must be on or before end")


def _coerce_date(value: object, field: str) -> date:
    """Normalize DATE values returned by SQL engines and SQLite fixtures."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"invalid {field} date {value!r}") from exc
    raise ValueError(f"invalid {field} date value {value!r}")


def _events_within_close_basis(
    events: list[FactorEvent],
    closes: list[tuple[str, date, float]],
) -> tuple[list[FactorEvent], int]:
    """Select events in each symbol's observable ``(first, last]`` basis.

    Ambiguous duplicate action dates are rejected inside the usable basis,
    including zero-valued rows, before those no-op rows are removed. Explicitly
    identified same-day events on a common pre-event share basis are coalesced.
    Outside-basis events are counted and do not require unavailable previous
    closes.
    """
    days_by_symbol: dict[str, list[date]] = {}
    for symbol, day, _close in closes:
        days_by_symbol.setdefault(symbol, []).append(day)
    bounds = {symbol: (min(days), max(days)) for symbol, days in days_by_symbol.items() if days}
    included: list[FactorEvent] = []
    outside = 0
    for event in events:
        symbol_bounds = bounds.get(event.symbol)
        if symbol_bounds is None or not (symbol_bounds[0] < event.ex_date <= symbol_bounds[1]):
            outside += 1
            continue
        validate_event(event)
        included.append(event)
    coalesced = coalesce_same_day_events(included)
    non_noop = [
        event
        for event in coalesced
        if event.cash_dividend != 0.0 or event.bonus != 0.0 or event.allotment_ratio != 0.0
    ]
    return non_noop, outside


def _affected_event_symbols(
    affected_keys: Sequence[tuple[object, ...]] | None,
) -> set[str] | None:
    """Validate action keys and return symbols whose factor series to rebuild."""
    if affected_keys is None:
        return None
    symbols: set[str] = set()
    for key in affected_keys:
        if not isinstance(key, (tuple, list)) or len(key) != 2:
            raise ValueError("affected corporate-event keys must be (symbol, ex_date) pairs")
        symbol, ex_date = key
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("affected corporate-event key symbol must be non-empty text")
        if not isinstance(ex_date, date) or isinstance(ex_date, datetime):
            raise ValueError("affected corporate-event key ex_date must be a date")
        symbols.add(_normalize_symbol(symbol))
    return symbols


def _symbol_clause(column: str, symbols: Sequence[str] | None) -> tuple[str, dict[str, str]]:
    """Build bound exact/exchange-suffix matches for normalized symbols."""
    if symbols is None:
        return "", {}
    normalized = sorted({_normalize_symbol(symbol) for symbol in symbols})
    if not normalized:
        return "1 = 0", {}
    if any("%" in symbol or "_" in symbol for symbol in normalized):
        raise ValueError("symbols used in bounded queries cannot contain LIKE wildcards")
    clauses: list[str] = []
    params: dict[str, str] = {}
    for index, symbol in enumerate(normalized):
        symbol_param = f"symbol_{index}"
        prefix_param = f"symbol_prefix_{index}"
        clauses.append(f"(`{column}` = :{symbol_param} OR `{column}` LIKE :{prefix_param})")
        params[symbol_param] = symbol
        params[prefix_param] = f"{symbol}.%"
    return f"({' OR '.join(clauses)})", params


def _factor_write_scopes(factors: Sequence[AdjustFactor]) -> tuple[FactorWriteScope, ...]:
    """Summarize the committed factor rows by symbol and full date range."""
    dates_by_symbol: dict[str, list[date]] = {}
    for factor in factors:
        symbol = factor.symbol
        trade_date = factor.trade_date
        dates_by_symbol.setdefault(symbol, []).append(trade_date)
    return tuple(
        FactorWriteScope(
            symbol=symbol,
            row_count=len(days),
            window_start=min(days),
            window_end=max(days),
        )
        for symbol, days in sorted(dates_by_symbol.items())
    )


def _normalize_symbol(thscode: str) -> str:
    """Strip the exchange suffix of a fuyao symbol.

    The ths ods tables carry ``600519.SH``-style identifiers; the dwd
    layer and the contract use the bare code, so the join key is
    normalized here exactly once, at the boundary.

    Args:
        thscode: The raw symbol.

    Returns:
        The bare code.
    """
    return str(thscode).split(".")[0]


def _window_clause(start: date | None, end: date | None, column: str) -> tuple[str, dict]:
    """Build a bound window clause.

    Args:
        start: Window start.
        end: Window end.
        column: Date column to filter.

    Returns:
        The bound conditions (no ``WHERE``/``AND`` prefix - the callers
        own those) and the parameters.
    """
    clauses = []
    params: dict[str, object] = {}
    if start is not None:
        clauses.append(f"`{column}` >= :start")
        params["start"] = start
    if end is not None:
        clauses.append(f"`{column}` <= :end")
        params["end"] = end
    return " AND ".join(clauses), params


def _with_dwd_trace(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the dwd lineage columns the writer and queries expect.

    Args:
        frame: The factor rows.

    Returns:
        The frame with ``source``/``_merged_at``/``_diff_flag``/``_as_of``
        set (factors are derived from the ths ods data, so the lineage
        names that source).
    """
    from datetime import datetime, timezone

    frame = frame.copy()
    frame["source"] = _AFFINE_FACTOR_SOURCE
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    frame["_merged_at"] = now
    frame["_diff_flag"] = 0
    frame["_as_of"] = now.date()
    return frame
