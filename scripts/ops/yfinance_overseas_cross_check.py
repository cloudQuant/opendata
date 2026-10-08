"""yfinance overseas daily bars against the official sina series (AC-10).

``stock_daily_overseas`` was registered in C1 with ``verified=false`` because
the adapter trusted the SDK's own framing: "auto_adjust" gives adjusted
prices, and the default is on. Measuring it against a second vendor showed
two口径 defects (dividend adjustment folded into the stored prices, and a
*global* backward split adjustment hiding inside the raw columns), which C8
fixes. This script is the proof for ``verified=true``.

The legs are chosen around the defect, not at random: two ordinary windows
(US and HK, where a dividend was paid inside the range) plus pairs of windows
around real splits - one straddling the ex-date, one entirely *before* it. The
second kind is the trap: the upstream still de-visions those bars by a ratio
that no row in the response mentions, so an adapter that only looks at the
window it asked for cannot restore the traded price.

Contract identity: Yahoo keys overseas symbols with an exchange suffix and
drops leading zeros (``0005.HK``) while sina's channel wants a zero-padded
five-digit code (``00005``), so each leg states both.

Pass criteria: overlap of at least ``MIN_OVERLAP`` of our trading days, plus a
relative deviation below ``CLOSE_TOLERANCE`` on close (the field the two
adjustment defects show up in), ``PRICE_TOLERANCE`` on the intraday fields and
``VOLUME_TOLERANCE`` on volume. A day above a bound fails the run unless it is
registered in ``KNOWN_DEVIATIONS`` with its attribution, and a registered day
that later falls back inside the bound fails as well - exceptions cannot
accumulate.

Usage (py313 env; every leg calls two vendors live, so the run needs network):
    python scripts/ops/yfinance_overseas_cross_check.py
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pandas as pd

    from opendata.data.models import OverseasBar
    from opendata.data.protocol import Fetcher


class Leg(NamedTuple):
    """One comparison: the Yahoo ticker, sina's symbol, and the window."""

    label: str
    yf_symbol: str
    sina_channel: str
    sina_symbol: str
    start: date
    end: date
    #: ``split`` legs straddle or precede an ex-date, ``plain`` legs do not.
    kind: str = "plain"
    fields: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def _us_sina(symbol: str) -> pd.DataFrame:
    from opendata.data.providers.akshare._vendor.stock.stock_us_sina import stock_us_daily

    return cast("pd.DataFrame", stock_us_daily(symbol=symbol, adjust=""))


def _hk_sina(symbol: str) -> pd.DataFrame:
    from opendata.data.providers.akshare._vendor.stock.stock_hk_sina import stock_hk_daily

    return cast("pd.DataFrame", stock_hk_daily(symbol=symbol, adjust=""))


SINA_CHANNELS: Mapping[str, Callable[[str], pd.DataFrame]] = {
    "stock_us_daily": _us_sina,
    "stock_hk_daily": _hk_sina,
}

#: Ordinary windows: KO and 0700 both paid a dividend inside the range, which
#: is what ``auto_adjust=True`` used to bake into the stored prices.
PLAIN_LEGS: tuple[Leg, ...] = (
    Leg("US 2026-08", "AAPL", "stock_us_daily", "AAPL", date(2026, 8, 3), date(2026, 9, 22)),
    Leg("US 2026-08 div", "KO", "stock_us_daily", "KO", date(2026, 8, 3), date(2026, 9, 22)),
    Leg("US 2026-08", "MSFT", "stock_us_daily", "MSFT", date(2026, 8, 3), date(2026, 9, 22)),
    Leg("HK 2026-08", "0700.HK", "stock_hk_daily", "00700", date(2026, 8, 3), date(2026, 9, 22)),
    Leg("HK 2026-08", "0005.HK", "stock_hk_daily", "00005", date(2026, 8, 3), date(2026, 9, 22)),
)

#: The 4:1 (AAPL 2020-08-31) and 5:1 (CVNA 2026-05-08) events, each measured on
#: both sides: the straddle proves the in-window restoration and the
#: pre-split-only window proves the factor comes from the full history.
SPLIT_LEGS: tuple[Leg, ...] = (
    Leg(
        "AAPL 4:1 straddle",
        "AAPL",
        "stock_us_daily",
        "AAPL",
        date(2020, 8, 20),
        date(2020, 9, 4),
        kind="split",
    ),
    Leg(
        "AAPL 4:1 before",
        "AAPL",
        "stock_us_daily",
        "AAPL",
        date(2020, 6, 1),
        date(2020, 7, 14),
        kind="split",
    ),
    Leg(
        "CVNA 5:1 straddle",
        "CVNA",
        "stock_us_daily",
        "CVNA",
        date(2026, 4, 27),
        date(2026, 5, 15),
        kind="split",
    ),
    Leg(
        "CVNA 5:1 before",
        "CVNA",
        "stock_us_daily",
        "CVNA",
        date(2026, 4, 6),
        date(2026, 4, 24),
        kind="split",
    ),
)

PRICE_FIELDS = ("open", "high", "low", "close")
#: Everything this script measures, in run order.
LEG_ROWS: tuple[Leg, ...] = PLAIN_LEGS + SPLIT_LEGS

#: 收盘是本轮口径的证明字段（复权/拆分错误都会体现在它上面）：除下述两笔
#: 已登记的单日修订外，实测 8 条腿全部 ≤ 6e-08（浮点表示余量），故判据取
#: 1e-06 而非更宽。
CLOSE_TOLERANCE = 1e-6
#: 盘中三价：sina 的美股 open/high/low 带 0.001 美分的尾数（实测 KO 89.405 对
#: yfinance 89.41，即 5.8e-05），是非可成交价的表示差异，判据取 1e-04。
#: 港股与全部历史窗口的盘中价实测 ≤ 1.5e-06。
PRICE_TOLERANCE = 1e-4
#: 成交量两侧同为「股」，留出交易所修正余量（实测最大 3.4e-03，AAPL
#: 2020-09-01 sina 侧 152,470,142 对 yfinance 151,948,100）。
VOLUME_TOLERANCE = 5e-3
#: 单日跨供应商修订，逐条登记而不是放宽阈值；命中登记项仍偏离超界则判失败，
#: 登记项回到阈值内同样判失败（陈旧例外必须删除）。
KNOWN_DEVIATIONS: Mapping[tuple[str, date, str], str] = {
    ("0700.HK", date(2026, 9, 4), "high"): (
        "yfinance 447.60 / sina 446.60：+1.00 港元（2.24e-03），该腿其余 36 日 high ≤ 2.9e-08，"
        "非乘性口径差，属单日盘中最高价修订"
    ),
    ("AAPL", date(2020, 7, 9), "close"): (
        "yfinance 383.01 / sina 382.73：+0.28 美元（7.32e-04），同腿 31 日开盘/最高/最低"
        "全部 ≤ 4.6e-08（已按 ×4 还原），故非拆分因子问题，属单日收盘修订"
    ),
}
#: 重叠交易日少于本比例即判失败：缺日说明窗口或标的对不上。
MIN_OVERLAP = 0.95


def _tolerance(field: str) -> float:
    """Bound for one compared field."""
    if field == "close":
        return CLOSE_TOLERANCE
    if field in PRICE_FIELDS:
        return PRICE_TOLERANCE
    return VOLUME_TOLERANCE


def _yf_fetcher() -> Fetcher[Any, Any]:
    """Resolve the routed yfinance fetcher exactly as a caller would."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry().resolve_domain("stock_daily_overseas", source="yfinance")


def _ours_frame(fetcher: Fetcher[Any, Any], symbol: str, start: date, end: date) -> pd.DataFrame:
    """Read one window through the routing path and flatten it to a frame."""
    import pandas as pd

    rows = cast(
        "Sequence[OverseasBar]",
        fetcher.fetch(symbol=symbol, start_date=start, end_date=end),
    )
    frame: pd.DataFrame = pd.DataFrame([row.model_dump() for row in rows])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def _sina_frame(channel: str, sina_symbol: str, start: date, end: date) -> pd.DataFrame:
    """Fetch one sina reference window and key it on a plain date column."""
    import pandas as pd

    frame = SINA_CHANNELS[channel](sina_symbol)
    if "date" not in frame.columns:
        frame = frame.reset_index()
    out = frame.copy()
    out["trade_date"] = pd.to_datetime(out["date"]).dt.date
    windowed = out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]
    return cast("pd.DataFrame", windowed.reset_index(drop=True))


def _compare(ours: pd.DataFrame, theirs: pd.DataFrame, fields: tuple[str, ...]) -> dict[str, Any]:
    """Join on trade date and measure every compared field.

    Args:
        ours: Yahoo bars restored by the adapter.
        theirs: sina's as-traded rows for the same window.
        fields: Columns compared, shared by both sides.

    Returns:
        ``compared``/``overlap``, per-field max deviation and ratio spread,
        the offending rows (with each registered exception's attribution),
        and the two failure flags.
    """
    merged = ours.merge(theirs, on="trade_date", how="inner", suffixes=("_yf", "_sina"))
    report: dict[str, Any] = {
        "compared": len(merged),
        "overlap": len(merged) / max(1, len(ours)),
        "fields": {},
        "diffs": [],
        "notes": [],
        "price_failed": False,
        "volume_failed": False,
    }
    if merged.empty:
        report.update(compared=0, overlap=0.0, diffs=["no common dates"], price_failed=True)
        return report
    symbol = str(merged["symbol"].iloc[0])
    if len(merged) < max(1, int(len(ours) * MIN_OVERLAP)):
        report["price_failed"] = True
        report["diffs"].append(f"only {len(merged)}/{len(ours)} trading days overlap")
    for field in fields:
        tolerance = _tolerance(field)
        worst = 0.0
        ratios: list[float] = []
        for trade_date, ours_value, theirs_value in zip(
            merged["trade_date"],
            merged[f"{field}_yf"],
            merged[f"{field}_sina"],
            strict=True,
        ):
            reference = float(theirs_value)
            if not reference:
                continue
            ratio = float(ours_value) / reference
            deviation = abs(ratio - 1.0)
            ratios.append(ratio)
            worst = max(worst, deviation)
            registered = KNOWN_DEVIATIONS.get((symbol, trade_date, field))
            over_bound = deviation > tolerance
            stale_exception = registered is not None and not over_bound
            if registered is not None and not stale_exception:
                report["notes"].append(
                    f"- {symbol} {trade_date} {field}（实测 {deviation:.2e} > 阈值 "
                    f"{tolerance:g}）：{registered}"
                )
            if (over_bound and registered is None) or stale_exception:
                if len(report["diffs"]) < 8:
                    reason = "stale registered deviation" if stale_exception else "unregistered"
                    report["diffs"].append(
                        f"{trade_date} {field} ({reason}): yfinance {float(ours_value):.6f} "
                        f"vs sina {reference:.6f} (dev={deviation:.2e}, bound={tolerance:g})"
                    )
                if field in PRICE_FIELDS:
                    report["price_failed"] = True
                else:
                    report["volume_failed"] = True
        report["fields"][field] = {
            "max": worst,
            "bound": tolerance,
            "ratio_min": min(ratios, default=float("nan")),
            "ratio_max": max(ratios, default=float("nan")),
        }
    return report


def _report_legs(
    legs: tuple[Leg, ...],
    fetcher: Fetcher[Any, Any],
    lines: list[str],
) -> tuple[int, list[str], list[str]]:
    """Run the legs, append table rows, and return failures, diffs and notes."""
    failures = 0
    details: list[str] = []
    notes: list[str] = []
    for leg in legs:
        try:
            ours = _ours_frame(fetcher, leg.yf_symbol, leg.start, leg.end)
            theirs = _sina_frame(leg.sina_channel, leg.sina_symbol, leg.start, leg.end)
        except Exception as exc:  # an evidence script reports, it does not raise
            failures += 1
            details.append(f"- {leg.yf_symbol} {leg.start}..{leg.end}: FAIL fetch ({exc})")
            lines.append(
                f"| {leg.label} | {leg.yf_symbol} | {leg.sina_symbol} | {leg.start} .. {leg.end} "
                "| - | - | - | - | - | FAIL (fetch) |"
            )
            print(f"!! {leg.yf_symbol}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        report = _compare(ours, theirs, leg.fields)
        failures += 1 if (report["price_failed"] or report["volume_failed"]) else 0
        fields = report["fields"]
        close = fields.get("close", {}).get("max", float("nan"))
        intraday = max(
            (fields[field]["max"] for field in ("open", "high", "low") if field in fields),
            default=float("nan"),
        )
        volume = fields.get("volume", {})
        ratio = f"{volume['ratio_min']:.6f} .. {volume['ratio_max']:.6f}" if volume else "n/a"
        verdict = "FAIL" if (report["price_failed"] or report["volume_failed"]) else "PASS"
        lines.append(
            f"| {leg.label} | {leg.yf_symbol} | {leg.sina_symbol} | {leg.start} .. {leg.end} "
            f"| {report['compared']} | {report['overlap']:.3f} | {close:.2e} | {intraday:.2e} "
            f"| {ratio} | {verdict} |"
        )
        details.extend(
            f"- {leg.yf_symbol} {leg.start}..{leg.end} {diff}" for diff in report["diffs"]
        )
        notes.extend(report["notes"])
        print(
            f".. {leg.label} {leg.yf_symbol}: {report['compared']} days, max close dev {close:.2e}",
            file=sys.stderr,
            flush=True,
        )
    return failures, details, notes


def _counterfactual(leg: Leg) -> dict[str, float]:
    """Measure what the SDK's own default would have stored for one leg.

    Two variants, because the adapter had two defects: ``auto_adjust=True``
    (the SDK default) additionally folds dividends in, and *raw mode with a
    window-local split factor* is the C8 pre-fix behaviour - it only undoes
    splits that appear inside the window.

    Args:
        leg: The leg to re-read straight from the SDK.

    Returns:
        Max relative deviation of close under each variant, plus the dividend
        total inside the window.
    """
    import yfinance

    from opendata.data.providers.yfinance.models.stock_daily import (
        SPLITS_COLUMN,
        as_traded_bars,
    )

    theirs = _sina_frame(leg.sina_channel, leg.sina_symbol, leg.start, leg.end)
    theirs = theirs.set_index("trade_date")
    kwargs = {"start": leg.start.isoformat(), "end": (leg.end + timedelta(days=1)).isoformat()}
    adjusted = cast(
        "pd.DataFrame",
        yfinance.Ticker(leg.yf_symbol).history(auto_adjust=True, interval="1d", **kwargs),
    )
    raw = cast(
        "pd.DataFrame",
        yfinance.Ticker(leg.yf_symbol).history(auto_adjust=False, interval="1d", **kwargs),
    )
    window_local = as_traded_bars(raw, raw[SPLITS_COLUMN])
    out: dict[str, float] = {"dividends": float(raw["Dividends"].sum())}
    for label, frame in (("auto_adjust", adjusted), ("window_local", window_local)):
        deviations = []
        for trade_date, value in frame["Close"].items():
            reference = theirs["close"].get(cast("pd.Timestamp", trade_date).date())
            if reference:
                deviations.append(abs(float(value) / float(reference) - 1.0))
        out[label] = max(deviations, default=float("nan"))
    return out


def main(argv: list[str] | None = None) -> int:
    """Run every leg, archive the report, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/evidence/C8/yfinance-overseas-cross-check.txt")
    parser.add_argument(
        "--counterfactual",
        action="store_true",
        help="measure the pre-fix readings instead of the routed adapter",
    )
    args = parser.parse_args(argv)

    if args.counterfactual:
        lines = [
            "What the pre-fix adapter and the SDK default would have stored",
            "",
            f"script: {REPO_RELATIVE} --counterfactual",
            "auto_adjust = the SDK default (dividend adjustment folded into OHLC)",
            "window_local = C8 pre-fix: raw mode, split factor taken from the window only",
            "deviation = max |value / sina as-traded close - 1| over the window",
            "",
            "| leg | symbol | window | dividends in window | max dev auto_adjust |"
            " max dev window-local |",
            "|---|---|---|---|---|---|",
        ]
        for leg in LEG_ROWS:
            try:
                measured = _counterfactual(leg)
            except Exception as exc:  # an evidence script reports, it does not raise
                lines.append(
                    f"| {leg.label} | {leg.yf_symbol} | {leg.start} .. {leg.end} "
                    f"| - | FAIL ({exc}) | - |"
                )
                continue
            lines.append(
                f"| {leg.label} | {leg.yf_symbol} | {leg.start} .. {leg.end} "
                f"| {measured['dividends']:.2f} "
                f"| {measured['auto_adjust']:.2e} | {measured['window_local']:.2e} |"
            )
        report_path = ROOT / args.out
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        print(f"\nreport written to {report_path}")
        return 0

    header = [
        "AC-10 stock_daily_overseas (yfinance) vs the official sina series",
        "",
        f"script: {REPO_RELATIVE}",
        "ours: get_registry().resolve_domain('stock_daily_overseas', source='yfinance').fetch(...)",
        "          (the routing path, i.e. the adapter's restored output)",
        "official: sina stock_us_daily / stock_hk_daily with adjust='' (as-traded)",
        "          -- different vendor, different adjustment chain",
        f"hard criteria: overlap >= {MIN_OVERLAP:g} of our trading days; relative deviation <=",
        f"          {CLOSE_TOLERANCE:g} on close, {PRICE_TOLERANCE:g} on open/high/low,",
        f"          {VOLUME_TOLERANCE:g} on volume.",
        "          A day above a bound fails unless it is registered in KNOWN_DEVIATIONS",
        "          (and a registered day that falls back inside the bound fails, too).",
        "",
        "| leg | yfinance | sina | window | days | overlap | max dev close |"
        " max dev O/H/L | volume ratio yf/sina | result |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    table_shape = header[-3:]
    fetcher = _yf_fetcher()
    plain_failures, plain_details, plain_notes = _report_legs(PLAIN_LEGS, fetcher, header)
    header += ["", "Split windows (the ex-date is inside, or after, the range):", ""] + table_shape
    split_failures, split_details, split_notes = _report_legs(SPLIT_LEGS, fetcher, header)

    lines = header
    lines.append("")
    lines.extend(plain_details + split_details)
    registered_notes = plain_notes + split_notes
    if registered_notes:
        lines += ["", "## 已登记的单日差异（跨供应商修订，非口径差）", "", *registered_notes]
    lines.append("")
    total = plain_failures + split_failures
    lines.append(f"result: {'FAIL' if total else 'PASS'} ({total} failing legs)")
    report_path = ROOT / args.out
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport written to {report_path}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
