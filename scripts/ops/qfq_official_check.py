"""Server-side adjust vs an official adjusted series (AC-11).

The REST layer synthesizes ``adjust=qfq|hfq`` from unadjusted bars and
``dwd_stock_adjust`` factors (design D10). This script walks the exact
query path the endpoint uses (:func:`build_data_select` +
:func:`apply_adjust_to_rows`), fetches the same symbol/window from an
official fetcher chosen from ``OFFICIAL_LEGS`` (a different source and a
different factor chain than ths), and reports the per-bar deviation.

The official leg is a dispatch table entry rather than an inline import:
``--official`` names the leg, the report header records the leg *and* the
module it resolved to, and the acceptance probe reads both back against
that module's provenance header. Which chain the comparison ran against is
therefore evidence, not prose in this file.

Two numbers are reported per symbol: the absolute deviation against the
official level, and the shape deviation after re-anchoring our series by
the median ratio -- so a constant anchoring convention shows up as a
level offset rather than a per-date failure. The official feeds round
prices to 2 decimals, so the tolerance is relative and generous.

Usage (py313 env, network):
    python scripts/ops/qfq_official_check.py --symbols 600519,000001 --start 2026-01-05
    python scripts/ops/qfq_official_check.py --official sina
"""

from __future__ import annotations

import argparse
import importlib
import statistics
import sys
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

DEFAULT_SYMBOLS = ("600519", "000001", "000002", "000009", "600036")
FACTOR_TABLE = "dwd_stock_adjust"
FACTOR_FIELDS = (
    "symbol",
    "trade_date",
    "qfq_factor",
    "hfq_factor",
    "qfq_scale",
    "qfq_offset",
    "hfq_scale",
    "hfq_offset",
    "adjustment_version",
    "legacy_source",
)
TOLERANCE = 2e-3  # the official feeds round prices to 2 decimals; ~5e-4 at 10 CNY
MAX_DIFFS = 5

#: The official kline endpoint answers 502/empty under request bursts. An empty frame is
#: never a comparison, so the leg retries with backoff and says how often it had to.
LEG_ATTEMPTS = 3
LEG_BACKOFF_SECONDS = 6.0


@dataclass(frozen=True)
class OfficialLeg:
    """One official adjusted series this synthesis can be compared against.

    Attributes:
        target: ``module:function`` of the vendored fetcher, resolved at
            call time so the module itself (and its provenance header) is
            part of the recorded evidence.
        date_column: Name of the bar date column in the returned frame.
        close_column: Name of the close column in the returned frame.
        prefixed_symbol: Whether the fetcher wants an exchange-prefixed
            symbol (``sh600519``) instead of the bare code.
    """

    target: str
    date_column: str
    close_column: str
    prefixed_symbol: bool


OFFICIAL_LEGS: dict[str, OfficialLeg] = {
    "akshare": OfficialLeg(
        target=(
            "opendata.data.providers.akshare._vendor.stock_feature.stock_hist_em:stock_zh_a_hist"
        ),
        date_column="日期",
        close_column="收盘",
        prefixed_symbol=False,
    ),
    "sina": OfficialLeg(
        target=("opendata.data.providers.akshare._vendor.stock.stock_zh_a_sina:stock_zh_a_daily"),
        date_column="date",
        close_column="close",
        prefixed_symbol=True,
    ),
}

# The acceptance criterion names akshare as the official comparator, so the
# default leg is the akshare-ported fetcher, not the sina one.
DEFAULT_OFFICIAL_LEG = "akshare"


def leg_spec(leg: str) -> OfficialLeg:
    """Look one official leg up in the dispatch table.

    Args:
        leg: Candidate leg name.

    Returns:
        The ``OfficialLeg`` registered under that name.

    Raises:
        ValueError: If the leg is not in the table - a wrong leg name must
            not read downstream as a network or data failure.
    """
    try:
        return OFFICIAL_LEGS[leg]
    except KeyError as exc:
        raise ValueError(f"unknown official leg {leg!r}") from exc


def leg_module(leg: str) -> str:
    """Module path the official leg resolves to.

    Args:
        leg: One of the ``OFFICIAL_LEGS`` keys.

    Returns:
        The importable module name holding the leg's fetcher.
    """
    return leg_spec(leg).target.partition(":")[0]


def leg_fetcher(leg: str) -> Callable[..., Any]:
    """Return the callable behind one official leg.

    Args:
        leg: One of the ``OFFICIAL_LEGS`` keys.

    Returns:
        The vendored fetcher the leg's ``module:function`` resolves to.
    """
    module_name, _, function_name = leg_spec(leg).target.partition(":")
    return cast("Callable[..., Any]", getattr(importlib.import_module(module_name), function_name))


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
            f"SELECT * FROM `{FACTOR_TABLE}` "  # noqa: S608
            "WHERE symbol = :symbol AND trade_date BETWEEN :start AND :end"
        )
        factors = [
            {key: row[key] for key in FACTOR_FIELDS if key in row}
            for row in conn.execute(
                text(factor_sql),
                {"symbol": symbol, "start": start, "end": end},
            )
            .mappings()
            .all()
        ]
    return apply_adjust_to_rows("stock_daily", rows, method=method, factors=factors)


@dataclass(frozen=True)
class OfficialSeries:
    """One official adjusted close series and how hard it was to get.

    Attributes:
        values: ``{ISO date: close}``.
        retries: Extra attempts the leg needed beyond the first one; the
            report prints it so a series that only arrived after backoff is
            not read as a clean single-shot comparison.
    """

    values: dict[str, float]
    retries: int


def _fetch_official_frame(
    leg: str, code: str, start_text: str, end_text: str, method: str
) -> tuple[Any, int]:
    """Call the leg's fetcher, retrying an empty or failed answer.

    Args:
        leg: One of the ``OFFICIAL_LEGS`` keys.
        code: Symbol already in the form that leg expects.
        start_text: Window start in the ``%Y%m%d`` form both fetchers take.
        end_text: Window end in the same form.
        method: ``qfq`` or ``hfq``.

    Returns:
        ``(frame, retries)`` with ``retries`` the number of attempts beyond
        the first one.

    Raises:
        RuntimeError: Every attempt came back empty or raised; the last
            error is reported. An empty official series is never a
            comparison, so it must not fall through as one.
    """
    fetcher = leg_fetcher(leg)
    frame: Any = None
    last: BaseException | None = None
    for attempt in range(LEG_ATTEMPTS):
        try:
            frame = fetcher(symbol=code, start_date=start_text, end_date=end_text, adjust=method)
            if not frame.empty:
                return frame, attempt
            last = RuntimeError(f"official leg {leg!r} returned no rows")
        except Exception as exc:
            last = exc
        if attempt + 1 < LEG_ATTEMPTS:
            time.sleep(LEG_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(
        f"official leg {leg!r} gave no series for {code} {method} "
        f"in {LEG_ATTEMPTS} attempts (last: {type(last).__name__}: {last})"
    ) from last


def _official_series(
    symbol: str, start: date, end: date, method: str, leg: str = DEFAULT_OFFICIAL_LEG
) -> OfficialSeries:
    """Fetch the official adjusted closes for the same window via one leg."""
    spec = leg_spec(leg)
    code = sina_symbol(symbol) if spec.prefixed_symbol else symbol
    frame, retries = _fetch_official_frame(
        leg, code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), method
    )
    dates = frame[spec.date_column].astype(str).str[:10]
    closes = frame[spec.close_column].astype(float)
    return OfficialSeries(dict(zip(dates.tolist(), closes.tolist(), strict=True)), retries)


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
        return {
            "compared": 0,
            "failed": True,
            "diffs": ["no common dates"],
            "over_tolerance": 0,
            "deviating_span": "",
            "profile": [],
        }

    ratios = [ours / theirs for _, ours, theirs in pairs]
    anchor = statistics.median(ratios)
    diffs: list[str] = []
    deviating: list[str] = []
    worst = 0.0
    worst_shape = 0.0
    for trade_date, ours, theirs in pairs:
        deviation = abs(ours / theirs - 1.0)
        shape = abs(ours / (theirs * anchor) - 1.0)
        worst = max(worst, deviation)
        worst_shape = max(worst_shape, shape)
        if shape > tolerance:
            deviating.append(trade_date)
            if len(diffs) < MAX_DIFFS:
                diffs.append(
                    f"{trade_date}: ours {ours:.4f} vs official {theirs:.4f} "
                    f"(shape dev={shape:.2e})"
                )
    return {
        "compared": len(pairs),
        "anchor": anchor,
        "max_dev": worst,
        "max_shape_dev": worst_shape,
        "failed": bool(diffs) or worst_shape > tolerance,
        "diffs": diffs,
        # MAX_DIFFS caps the listing, not the population: without these two a
        # five-bar list reads as five bad bars when the whole window is offset.
        "over_tolerance": len(deviating),
        "deviating_span": ("" if not deviating else f"{deviating[0]}..{deviating[-1]}"),
        # The level of ours/official after anchoring, bucketed: one plateau means a
        # shared convention with rounding noise, two plateaus mean an event the two
        # factor chains place on different dates.
        "profile": Counter(round(ratio / anchor, 5) for ratio in ratios).most_common(6),
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
    parser.add_argument(
        "--official",
        default=DEFAULT_OFFICIAL_LEG,
        choices=sorted(OFFICIAL_LEGS),
        help="official comparison leg (its module provenance is recorded)",
    )
    parser.add_argument("--out", default="docs/evidence/C56/qfq-official-akshare.txt")
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
        f"AC-11 server-side adjust vs the official {args.official} series",
        "",
        f"script: {REPO_RELATIVE}",
        f"window: {args.start} .. {args.end}{clamped}",
        f"symbols: {', '.join(symbols)}",
        "ours: dwd_stock_daily (unadjusted) with dwd_stock_adjust coefficients,",
        "  via build_data_select + apply_adjust_to_rows (the REST query path)",
        f"official: {args.official} {leg_spec(args.official).target} "
        "(adjust=qfq|hfq) -- different source and factor chain",
        f"tolerance: relative {args.tolerance:g} on close (official feeds round to 2 decimals)",
        "",
    ]
    failures = 0
    for method in methods:
        lines += [
            f"## adjust={method}",
            "",
            "| symbol | bars | bars over tol | anchor (ours/official) | max abs dev "
            "| max shape dev | leg retries | result |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for symbol in symbols:
            try:
                adjusted = _server_side_series(engine, symbol, args.start, args.end, method)
                series = _official_series(symbol, args.start, args.end, method, args.official)
                report = _compare(adjusted, series.values, args.tolerance)
            except Exception as exc:  # evidence script reports, it does not raise
                failures += 1
                lines.append(
                    f"| {symbol} | - | - | - | - | - | - | ERROR {type(exc).__name__}: {exc} |"
                )
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
            lines.append(
                f"- {symbol} {method} over tolerance: {report['over_tolerance']} of "
                f"{report['compared']} bars"
                + (f" in {report['deviating_span']}" if report["deviating_span"] else "")
                + "; levels(ours/official / anchor): "
                + " ".join(f"{value:g}x{hits}" for value, hits in report["profile"])
            )
            anchor = report.get("anchor", float("nan"))
            dev = report.get("max_dev", float("nan"))
            shape = report.get("max_shape_dev", float("nan"))
            verdict = "FAIL" if report["failed"] else "PASS"
            cells = [
                symbol,
                str(report["compared"]),
                str(report["over_tolerance"]),
                f"{anchor:.6f}",
                f"{dev:.2e}",
                f"{shape:.2e}",
                str(series.retries),
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
