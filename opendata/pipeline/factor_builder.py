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
from opendata.pipeline.factors import FactorEvent, cumulate_factors, validate_event

if TYPE_CHECKING:
    from sqlalchemy import Engine

#: Default target of the derived factor layer.
DWD_FACTOR_TABLE = "dwd_stock_adjust"


@dataclass(frozen=True)
class FactorBuildResult:
    """What one build did.

    Attributes:
        rows_written: Factor rows upserted.
        symbols: Distinct symbols covered.
        rows_planned: Factor rows selected for an upsert, including dry runs.
        events_outside_basis: Source events outside each symbol's close range.
        legacy_rows_preserved: Existing legacy multiplication factors retained verbatim.
    """

    rows_written: int
    symbols: int
    rows_planned: int = 0
    events_outside_basis: int = 0
    legacy_rows_preserved: int = 0


class FactorBuilder:
    """Cumulate ods events and closes into the stock-adjust dwd table."""

    def __init__(
        self,
        engine: Engine,
        *,
        daily_table: str,
        action_table: str,
        target_table: str = DWD_FACTOR_TABLE,
    ) -> None:
        """Bind the builder to its ods inputs and dwd target.

        Args:
            engine: Warehouse engine.
            daily_table: Ods table of the unadjusted daily closes
                (``ods_<domain>_<source>``).
            action_table: Ods table of the corporate-action events.
            target_table: Dwd table to write.
        """
        self.engine = engine
        self.daily_table = daily_table
        self.action_table = action_table
        self.target_table = target_table

    def build(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        dry_run: bool = False,
    ) -> FactorBuildResult:
        """Rebuild the factor table from the ods layers.

        Args:
            start: Optional date to start the written factor window.
            end: Optional window end.
            dry_run: Compute and validate without constructing a writer or writing rows.

        Returns:
            Rows upserted and symbols covered.

        Raises:
            ValueError: An event has no previous close or a degenerate
                ratio (fail closed - the caller decides what that means
                for the run).
        """
        _validate_window(start, end)
        # Full source history establishes stable per-symbol anchors. The
        # requested window limits writes only; it never changes the factors.
        closes = self._read_closes(None, None)
        raw_events = self._read_events(None, None)
        events, events_outside_basis = _events_within_close_basis(raw_events, closes)
        factors = cumulate_factors(events, closes)
        selected = [
            factor
            for factor in factors
            if (start is None or factor.trade_date >= start)
            and (end is None or factor.trade_date <= end)
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
            if old is None:
                row["legacy_source"] = "corporate-action-ratio-v1"
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
        return FactorBuildResult(
            rows_written=rows_written,
            symbols=symbols,
            rows_planned=rows_planned,
            events_outside_basis=events_outside_basis,
            legacy_rows_preserved=legacy_rows_preserved,
        )

    def _read_closes(self, start: date | None, end: date | None) -> list[tuple[str, date, float]]:
        """Read the unadjusted close series of the daily ods table.

        Args:
            start: Window start (None for every row).
            end: Window end.

        Returns:
            ``(symbol, trade_date, close)`` rows; symbols are normalized
            by stripping the exchange suffix so event and daily tables
            join on the same identifier.
        """
        where, params = _window_clause(start, end, "trade_date")
        suffix = f" AND {where}" if where else ""
        sql = (
            "SELECT `thscode`, `trade_date`, `close_price` "  # noqa: S608  # registry-derived table
            f"FROM `{self.daily_table}` WHERE `adjusted` = 'none'{suffix}"
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

    def _read_events(self, start: date | None, end: date | None) -> list[FactorEvent]:
        """Read the corporate-action events of the action ods table.

        Args:
            start: Window start (None for every event).
            end: Window end.

        Returns:
            All source rows, including zero-valued rows, so duplicates can
            be checked before no-op rows are filtered within the close basis.
        """
        where, params = _window_clause(start, end, "ex_date")
        suffix = f" WHERE {where}" if where else ""
        sql = (
            "SELECT `thscode`, `ex_date`, `dividend_per_share`, `per_share_bonus`, "  # noqa: S608  # registry-derived table
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
            f"SELECT `symbol`, `trade_date`, `qfq_factor`, `hfq_factor`, `source`, `legacy_source` "  # noqa: S608  # registry-derived table
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

    Duplicate action dates are rejected inside the usable basis, including
    zero-valued rows, before those no-op rows are removed. Outside-basis
    events are counted and do not require unavailable previous closes.
    """
    days_by_symbol: dict[str, list[date]] = {}
    for symbol, day, _close in closes:
        days_by_symbol.setdefault(symbol, []).append(day)
    bounds = {symbol: (min(days), max(days)) for symbol, days in days_by_symbol.items() if days}
    included: list[FactorEvent] = []
    outside = 0
    seen: set[tuple[str, date]] = set()
    for event in events:
        symbol_bounds = bounds.get(event.symbol)
        if symbol_bounds is None or not (symbol_bounds[0] < event.ex_date <= symbol_bounds[1]):
            outside += 1
            continue
        key = (event.symbol, event.ex_date)
        if key in seen:
            raise ValueError(
                f"{event.symbol} @ {event.ex_date}: ambiguous multiple corporate actions"
            )
        seen.add(key)
        validate_event(event)
        if event.cash_dividend == 0.0 and event.bonus == 0.0 and event.allotment_ratio == 0.0:
            continue
        included.append(event)
    return included, outside


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
    frame["source"] = "corporate-action-affine-v1"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    frame["_merged_at"] = now
    frame["_diff_flag"] = 0
    frame["_as_of"] = now.date()
    return frame
