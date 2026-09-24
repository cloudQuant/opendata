"""Server-side adjust vs the official sina adjusted series (AC-11).

The REST layer synthesizes ``adjust=qfq|hfq`` from unadjusted bars and
``dwd_stock_adjust`` factors (design D10). This script walks the exact
query path the endpoint uses (:func:`build_data_select` +
:func:`apply_adjust_to_rows`), fetches the same symbol/window from the
upstream sina fetcher (a different source and a different factor chain
than ths), and reports the per-bar deviation.

Two numbers are reported per symbol: the absolute deviation against the
official level, and the shape deviation after re-anchoring our series by
the median ratio -- so a constant anchoring convention shows up as a
level offset rather than a per-date failure. Sina publishes prices
rounded to 2 decimals, so the tolerance is relative and generous.

Usage (py313 env, network):
    python scripts/ops/qfq_official_check.py --symbols 600519,000001 --start 2026-01-05
"""

from __future__ import annotations

import argparse
import statistics
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from sqlalchemy import Engine

DEFAULT_SYMBOLS = ("600519", "000001", "000002", "000009", "600036")
FACTOR_TABLE = "dwd_stock_adjust"
TOLERANCE = 2e-3  # sina rounds prices to 2 decimals; ~5e-4 at 10 CNY
MAX_DIFFS = 5


def sina_symbol(code: str) -> str:
    """Map a plain A-share code to its sina symbol.

    Args:
        code: Bare code such as ``600519``.

    Returns:
        The prefixed sina symbol (``sh600519``).

    Raises:
        ValueError: If the code has no sina exchange prefix rule.
    """
    if code.startswith(("60", "68", "51", "58")):
        return f"sh{code}"
    if code.startswith(("00", "30", "15", "16")):
        return f"sz{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    raise ValueError(f"cannot derive the sina symbol for {code!r}")


def _table_columns(engine: Engine, table: str) -> list[str]:
    """Column names of a warehouse table, in table order."""
    from sqlalchemy import inspect

    return [str(column["name"]) for column in inspect(engine).get_columns(table)]


def _server_side_series(
    engine: Engine, symbol: str, start: date, end: date, method: str
) -> list[dict[str, Any]]:
    """Read the adjusted rows the REST query path would return.

    Same builder and same adjustment function as
    ``GET /api/v1/data/equity/stock_daily?adjust={method}``; paging is
    drained so windows wider than one page are covered.
    """
    from sqlalchemy import text

    from opendata.data.domains import dwd_table
    from opendata.pipeline.query import (
        MAX_PAGE_SIZE,
        DataQuery,
        apply_adjust_to_rows,
        build_data_select,
    )

    table = dwd_table("stock_daily")
    columns = _table_columns(engine, table)
    key = ["symbol", "trade_date"]
    rows: list[dict[str, Any]] = []
    with engine.connect() as conn:
        page = 1
        while True:
            query = DataQuery(
                domain="stock_daily",
                layer="dwd",
                symbols=(symbol,),
                start=start,
                end=end,
                page=page,
                page_size=MAX_PAGE_SIZE,
                adjust=method,
            )
            sql, params = build_data_select(query, table=table, columns=columns, key=key)
            chunk = [dict(r) for r in conn.execute(text(sql), params).mappings().all()]
            rows.extend(chunk)
            if len(chunk) < MAX_PAGE_SIZE:
                break
            page += 1

        factor_sql = (
            f"SELECT symbol, trade_date, qfq_factor, hfq_factor FROM `{FACTOR_TABLE}` "  # noqa: S608
            "WHERE symbol = :symbol AND trade_date BETWEEN :start AND :end"
        )
        factors = [
            dict(r)
            for r in conn.execute(
                text(factor_sql),
                {"symbol": symbol, "start": start, "end": end},
            )
            .mappings()
            .all()
        ]
    return apply_adjust_to_rows("stock_daily", rows, method=method, factors=factors)


def _official_series(symbol: str, start: date, end: date, method: str) -> dict[str, float]:
    """Fetch the official sina adjusted closes for the same window."""
    from opendata_http.stock.stock_zh_a_sina import stock_zh_a_daily

    frame = stock_zh_a_daily(
        symbol=sina_symbol(symbol),
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust=method,
    )
    dates = frame["date"].astype(str).str[:10]
    closes = frame["close"].astype(float)
    return dict(zip(dates.tolist(), closes.tolist(), strict=True))


def _compare(
    adjusted: list[dict[str, Any]], official: dict[str, float], tolerance: float
) -> dict[str, Any]:
    """Compare our synthesized closes against the official series."""
    pairs: list[tuple[str, float, float]] = []
    for row in adjusted:
        trade_date = str(row["trade_date"])[:10]
        ours = float(row["close"])
        theirs = official.get(trade_date)
        if theirs:
            pairs.append((trade_date, ours, theirs))
    if not pairs:
        return {"compared": 0, "failed": True, "diffs": ["no common dates"]}

    ratios = [ours / theirs for _, ours, theirs in pairs]
    anchor = statistics.median(ratios)
    diffs: list[str] = []
    worst = 0.0
    worst_shape = 0.0
    for trade_date, ours, theirs in pairs:
        deviation = abs(ours / theirs - 1.0)
        shape = abs(ours / (theirs * anchor) - 1.0)
        worst = max(worst, deviation)
        worst_shape = max(worst_shape, shape)
        if shape > tolerance and len(diffs) < MAX_DIFFS:
            diffs.append(
                f"{trade_date}: ours {ours:.4f} vs official {theirs:.4f} (shape dev={shape:.2e})"
            )
    return {
        "compared": len(pairs),
        "anchor": anchor,
        "max_dev": worst,
        "max_shape_dev": worst_shape,
        "failed": bool(diffs) or worst_shape > tolerance,
        "diffs": diffs,
    }


def _factor_ceiling(engine: Engine) -> date | None:
    """Latest date the factor table covers (adjust is fail-closed beyond it)."""
    from sqlalchemy import text

    with engine.connect() as conn:
        latest = conn.execute(
            text(f"SELECT MAX(trade_date) FROM `{FACTOR_TABLE}`")  # noqa: S608
        ).scalar()
    return None if latest is None else date.fromisoformat(str(latest)[:10])


def main(argv: list[str] | None = None) -> int:
    """Run the comparison and archive the report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start", default="2026-01-05", type=date.fromisoformat)
    parser.add_argument("--end", default=date.today().isoformat(), type=date.fromisoformat)
    parser.add_argument("--tolerance", default=TOLERANCE, type=float)
    parser.add_argument("--methods", default="qfq,hfq")
    parser.add_argument("--out", default="docs/evidence/B4/qfq-official-check.txt")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from opendata.core.config import settings

    engine = create_engine(settings.data_database_url)
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    methods = [item.strip() for item in args.methods.split(",") if item.strip()]

    ceiling = _factor_ceiling(engine)
    clamped = ""
    if ceiling and args.end > ceiling:
        clamped = f" (clamped to the {FACTOR_TABLE} ceiling)"
        args.end = ceiling

    lines = [
        "AC-11 server-side adjust vs the official sina series",
        "",
        f"script: {REPO_RELATIVE}",
        f"window: {args.start} .. {args.end}{clamped}",
        f"symbols: {', '.join(symbols)}",
        "ours: dwd_stock_daily (unadjusted) x dwd_stock_adjust factors,",
        "  via build_data_select + apply_adjust_to_rows (the REST query path)",
        "official: sina stock_zh_a_daily(adjust=qfq|hfq) -- different source and factor chain",
        f"tolerance: relative {args.tolerance:g} on close (sina rounds prices to 2 decimals)",
        "",
    ]
    failures = 0
    for method in methods:
        lines += [
            f"## adjust={method}",
            "",
            "| symbol | bars | anchor (ours/official) | max abs dev | max shape dev | result |",
            "|---|---|---|---|---|---|",
        ]
        for symbol in symbols:
            try:
                adjusted = _server_side_series(engine, symbol, args.start, args.end, method)
                official = _official_series(symbol, args.start, args.end, method)
                report = _compare(adjusted, official, args.tolerance)
            except Exception as exc:  # evidence script reports, it does not raise
                failures += 1
                lines.append(f"| {symbol} | - | - | - | - | ERROR {type(exc).__name__}: {exc} |")
                print(f"!! {symbol} {method}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            if report["failed"]:
                failures += 1
            print(
                f".. {method} {symbol}: {report['compared']} bars, "
                f"shape dev {report.get('max_shape_dev', float('nan')):.2e}",
                file=sys.stderr,
                flush=True,
            )
            for diff in report["diffs"]:
                lines.append(f"- {symbol} {method} {diff}")
            anchor = report.get("anchor", float("nan"))
            dev = report.get("max_dev", float("nan"))
            shape = report.get("max_shape_dev", float("nan"))
            verdict = "FAIL" if report["failed"] else "PASS"
            cells = [
                symbol,
                str(report["compared"]),
                f"{anchor:.6f}",
                f"{dev:.2e}",
                f"{shape:.2e}",
                verdict,
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    verdict = "FAIL" if failures else "PASS"
    lines.append(f"result: {verdict} ({failures} failing symbol/method pairs)")
    report_path = ROOT / args.out
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport written to {report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
