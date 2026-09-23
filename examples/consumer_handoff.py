"""Consumer hand-off example (design §10.4).

Simulates what ``backtrader_web`` does: fetch A-share daily bars through
``opendata_client`` and prepare a backtest run. The acceptance document
(§0, AC-14) allows exactly this as the degraded consumer validation
while the consumer repository side is unscheduled, so the script also
prints the hand-off record (calls, rows, elapsed) the acceptance asks
for.

Usage:
    export OPENDATA_BASE_URL=http://127.0.0.1:8000
    export OPENDATA_API_KEY=od-...
    python examples/consumer_handoff.py --symbol 600519 --start 2024-01-01 --end 2024-03-31

With ``backtrader`` installed the script runs a minimal moving-average
crossover on the fetched bars and prints the final portfolio value; the
data preparation itself does not need backtrader.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

# The client is a sibling package (git dependency in the consumer).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "opendata_client"))

from opendata_client import OpendataClient  # path set above

if TYPE_CHECKING:
    import pandas as pd

BAR_COLUMNS = ("trade_date", "open", "high", "low", "close", "volume")


@dataclass
class HandoffRecord:
    """What the acceptance wants recorded for one consumer hand-off.

    Attributes:
        symbol: Symbol fetched.
        calls: HTTP calls made.
        rows: Bars received.
        elapsed_seconds: Wall time of the fetch.
        first_date: Earliest trade date seen.
        last_date: Latest trade date seen.
        final_value: Backtest final value, when a backtest ran.
        bars: Frames per symbol, ready for the backtest engine.
    """

    symbol: str
    calls: int
    rows: int
    elapsed_seconds: float
    first_date: str | None = None
    last_date: str | None = None
    final_value: float | None = None
    bars: list[dict[str, object]] = field(default_factory=list)

    def summary(self) -> str:
        """Render the record as the acceptance evidence lines.

        Returns:
            A multi-line human readable record.
        """
        lines = [
            f"symbol      : {self.symbol}",
            f"calls       : {self.calls}",
            f"rows        : {self.rows}",
            f"date range  : {self.first_date} .. {self.last_date}",
            f"elapsed     : {self.elapsed_seconds:.2f}s",
        ]
        if self.final_value is not None:
            lines.append(f"backtest    : final value {self.final_value:,.2f}")
        return "\n".join(lines)


def fetch_bars(
    base_url: str,
    api_key: str,
    symbol: str,
    start: str | None,
    end: str | None,
    *,
    adjust: str = "none",
    page_size: int = 500,
) -> HandoffRecord:
    """Fetch one symbol's daily bars and prepare backtest-ready frames.

    Args:
        base_url: API root.
        api_key: Consumer API key.
        symbol: Symbol to fetch.
        start: Inclusive start date.
        end: Inclusive end date.
        adjust: ``none``, ``qfq`` or ``hfq``.
        page_size: Rows per request.

    Returns:
        The hand-off record, with ``bars`` in ascending date order.
    """
    started = time.monotonic()
    with OpendataClient(base_url, api_key=api_key) as client:
        rows = client.stock_daily_all(
            symbol, start=start, end=end, adjust=adjust, page_size=page_size
        )
        calls = 1 + (len(rows) // page_size if page_size else 0)
    elapsed = time.monotonic() - started
    bars = sorted((row for row in rows if row.get("trade_date")), key=lambda row: row["trade_date"])
    return HandoffRecord(
        symbol=symbol,
        calls=calls,
        rows=len(bars),
        elapsed_seconds=elapsed,
        first_date=str(bars[0]["trade_date"]) if bars else None,
        last_date=str(bars[-1]["trade_date"]) if bars else None,
        bars=bars,
    )


def run_backtest(record: HandoffRecord, *, fast: int = 5, slow: int = 20) -> float | None:
    """Run a minimal moving-average crossover on the fetched bars.

    Args:
        record: Hand-off record carrying the bars.
        fast: Fast moving-average period.
        slow: Slow moving-average period.

    Returns:
        The final portfolio value, or ``None`` when backtrader is not
        installed (the data preparation still succeeded).
    """
    try:
        import backtrader as bt
    except ImportError:
        print("backtrader is not installed: skipping the backtest run")
        return None
    if len(record.bars) < 2:
        print("not enough bars for a backtest run")
        return None

    class Crossover(bt.Strategy):
        """Two-period crossover over the standard bar lines."""

        params = (("fast", fast), ("slow", slow))

        def __init__(self) -> None:
            fast = bt.ind.SMA(self.data.close, period=self.p.fast)
            slow = bt.ind.SMA(self.data.close, period=self.p.slow)
            self.signal = bt.ind.CrossOver(fast, slow)

        def next(self) -> None:
            """Trade the crossover, one lot at a time."""
            if not self.position and self.signal > 0:
                self.buy()
            elif self.position and self.signal < 0:
                self.close()

    cerebro = bt.Cerebro()
    cerebro.addstrategy(Crossover)
    # The frame carries a DatetimeIndex (see _as_frame), which is what
    # PandasData treats as the datetime line by default.
    data = bt.feeds.PandasData(dataname=_as_frame(record.bars))
    cerebro.adddata(data)
    cerebro.broker.setcash(100_000.0)
    result = cerebro.run()
    return float(result[0].broker.getvalue())


def _as_frame(bars: list[dict[str, object]]) -> pd.DataFrame:
    """Convert bars to the pandas frame backtrader expects.

    Args:
        bars: Row dicts from the API.

    Returns:
        A DataFrame with a ``DatetimeIndex`` and the standard columns.
    """
    import pandas as pd

    frame = pd.DataFrame(bars)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.set_index("trade_date")[list(BAR_COLUMNS[1:])]


def main(argv: list[str] | None = None) -> int:
    """Fetch bars for one symbol and prepare (and optionally run) a backtest.

    Args:
        argv: Command line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--adjust", default="none", choices=("none", "qfq", "hfq"))
    parser.add_argument(
        "--base-url", default=os.getenv("OPENDATA_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--api-key", default=os.getenv("OPENDATA_API_KEY"))
    parser.add_argument("--fast", type=int, default=5, help="fast SMA period")
    parser.add_argument("--slow", type=int, default=20, help="slow SMA period")
    parser.add_argument("--no-backtest", action="store_true")
    args = parser.parse_args(argv)

    if not args.api_key:
        parser.error("pass --api-key or set OPENDATA_API_KEY")
    record = fetch_bars(
        args.base_url,
        args.api_key,
        args.symbol,
        args.start,
        args.end,
        adjust=args.adjust,
    )
    if not args.no_backtest:
        record.final_value = run_backtest(record, fast=args.fast, slow=args.slow)
    print(record.summary())
    return 0 if record.rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
