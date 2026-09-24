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

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import text

from opendata.pipeline.dwd_merge import DwdWriter
from opendata.pipeline.factors import FactorEvent, cumulate_factors

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy import Engine

#: Default target of the derived factor layer.
DWD_FACTOR_TABLE = "dwd_stock_adjust"


@dataclass(frozen=True)
class FactorBuildResult:
    """What one build did.

    Attributes:
        rows_written: Factor rows upserted.
        symbols: Distinct symbols covered.
    """

    rows_written: int
    symbols: int


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

    def build(self, *, start: date | None = None, end: date | None = None) -> FactorBuildResult:
        """Rebuild the factor table from the ods layers.

        Args:
            start: Optional window start (else every event is read).
            end: Optional window end.

        Returns:
            Rows upserted and symbols covered.

        Raises:
            ValueError: An event has no previous close or a degenerate
                ratio (fail closed - the caller decides what that means
                for the run).
        """
        closes = self._read_closes(start, end)
        events = self._read_events(start, end)
        factors = cumulate_factors(events, closes)
        if not factors:
            return FactorBuildResult(rows_written=0, symbols=0)
        frame = pd.DataFrame([factor.model_dump() for factor in factors])
        frame = _with_dwd_trace(frame)
        writer = DwdWriter(self.engine)
        rows = writer.write(frame, table=self.target_table, key=("symbol", "trade_date"))
        return FactorBuildResult(rows_written=rows, symbols=frame["symbol"].nunique())

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
        sql = (
            "SELECT `thscode`, `trade_date`, `close_price` "  # noqa: S608  # registry-derived table
            f"FROM `{self.daily_table}` WHERE `adjusted` = 'none' {where}"
        )
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), params)
            return [(_normalize_symbol(row[0]), row[1], float(row[2])) for row in result.fetchall()]

    def _read_events(self, start: date | None, end: date | None) -> list[FactorEvent]:
        """Read the corporate-action events of the action ods table.

        Args:
            start: Window start (None for every event).
            end: Window end.

        Returns:
            The events; all-zero rows are dropped (the contract forbids
            them - a row with every payout zero would cumulate to a
            no-op at best and a nonsense ratio at worst).
        """
        where, params = _window_clause(start, end, "ex_date")
        sql = (
            "SELECT `thscode`, `ex_date`, `dividend_per_share`, `per_share_bonus`, "  # noqa: S608  # registry-derived table
            "`allotment_ratio`, `allotment_price` "
            f"FROM `{self.action_table}` {where}"
        )
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), params)
            events: list[FactorEvent] = []
            for row in result.fetchall():
                event = FactorEvent(
                    symbol=_normalize_symbol(row[0]),
                    ex_date=row[1],
                    cash_dividend=float(row[2] or 0.0),
                    bonus=float(row[3] or 0.0),
                    allotment_ratio=float(row[4] or 0.0),
                    allotment_price=float(row[5] or 0.0),
                )
                if (
                    event.cash_dividend == 0.0
                    and event.bonus == 0.0
                    and event.allotment_ratio == 0.0
                ):
                    continue
                events.append(event)
            return events


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
        The SQL fragment and its parameters.
    """
    clauses = []
    params: dict[str, object] = {}
    if start is not None:
        clauses.append(f"`{column}` >= :start")
        params["start"] = start
    if end is not None:
        clauses.append(f"`{column}` <= :end")
        params["end"] = end
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


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
    frame["source"] = "ths"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    frame["_merged_at"] = now
    frame["_diff_flag"] = 0
    frame["_as_of"] = now.date()
    return frame
