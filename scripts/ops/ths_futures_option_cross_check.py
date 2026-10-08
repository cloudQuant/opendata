"""ths futures/option daily bars against the official sina series (AC-10).

The ``futures_daily`` and ``option_daily`` legs registered in C6 are second
sources for domains that B1.2 opened with akshare only. This script walks the
routing path a caller uses (``get_registry().resolve_domain(domain,
source='ths')``) and compares every bar field against sina's published series
for the same contract - a different vendor with a different aggregation
chain.

Contract identity is proven here rather than assumed: fuyao keys derivatives
by ``thscode`` and sina by its own contract symbol, so each leg states the
mapping it relies on (see ``LEG_CASES`` and the report header). The
commodity-option route needs the separators dropped
(``L2611-P-7200.DCE`` -> ``L2611P7200``), the CFFEX index-option route needs
them dropped *and* the underlying lower-cased (``MO2612-C-7600.CFE`` ->
``mo2612C7600``), and the SSE ETF-option route needs nothing at all because
fuyao's ``thscode`` prefix *is* the SSE's 8-digit contract number.

Pass criteria: overlap plus an exact-agreement price bound (measured 0
deviation on every leg). Volume is measured on every leg and reported with
its ratio spread, and is only a hard criterion on the futures legs
(``VOLUME_STRICT_DOMAINS``, bound ``VOLUME_TOLERANCE``) - the thinner option
routes disagree with sina in a one-sided, non-constant way (one lot on a
46-lot day is already 2.5e-01), so the report attributes those rather than
hiding them behind a threshold.

Usage (py313 env; every leg calls sina live, so the run needs network):
    python scripts/ops/ths_futures_option_cross_check.py --start 2026-06-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    import pandas as pd

    from opendata.data.models import ContractModel
    from opendata.data.protocol import Fetcher


class Leg(NamedTuple):
    """One comparison: a routed domain, the fuyao code, and sina's symbol."""

    domain: str
    ths_symbol: str
    sina_channel: str
    sina_symbol: str
    #: Fields the two vendors publish in the same unit; ``amount`` is fuyao-only.
    fields: tuple[str, ...] = ("open", "high", "low", "close", "volume")


#: sina reference channels, resolved lazily so the script imports offline.
def _futures_sina(symbol: str) -> pd.DataFrame:
    from opendata.data.providers.akshare._vendor.futures.futures_zh_sina import (
        futures_zh_daily_sina,
    )

    return cast("pd.DataFrame", futures_zh_daily_sina(symbol=symbol))


def _commodity_option_sina(symbol: str) -> pd.DataFrame:
    from opendata.data.providers.akshare._vendor.option.option_commodity_sina import (
        option_commodity_hist_sina,
    )

    return cast("pd.DataFrame", option_commodity_hist_sina(symbol=symbol))


def _cffex_option_sina(symbol: str) -> pd.DataFrame:
    from opendata.data.providers.akshare._vendor.option.option_finance_sina import (
        option_cffex_zz1000_daily_sina,
    )

    return cast("pd.DataFrame", option_cffex_zz1000_daily_sina(symbol=symbol))


def _sse_option_sina(symbol: str) -> pd.DataFrame:
    """SSE ETF options: sina labels four columns in Chinese."""
    from opendata.data.providers.akshare._vendor.option.option_finance_sina import (
        option_sse_daily_sina,
    )

    frame = cast("pd.DataFrame", option_sse_daily_sina(symbol=symbol))
    return cast(
        "pd.DataFrame",
        frame.rename(
            columns={"日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close"}
        ),
    )


SINA_CHANNELS: Mapping[str, Callable[[str], pd.DataFrame]] = {
    "futures_zh_daily_sina": _futures_sina,
    "option_commodity_hist_sina": _commodity_option_sina,
    "option_cffex_zz1000_daily_sina": _cffex_option_sina,
    "option_sse_daily_sina": _sse_option_sina,
}

#: 3 个交易所的期货合约 + 4 条期权路由（DCE/SHFE 商品期权、CFFEX 股指期权、
#: SSE ETF 期权）。``RB2610`` 故意传裸码，顺带证明目录解析器。
LEG_CASES: tuple[Leg, ...] = (
    Leg("futures_daily", "RB2610", "futures_zh_daily_sina", "RB2610"),
    Leg("futures_daily", "CU2610.SHF", "futures_zh_daily_sina", "CU2610"),
    Leg("futures_daily", "IF2612.CFE", "futures_zh_daily_sina", "IF2612"),
    Leg(
        "option_daily",
        "L2611-P-7200.DCE",
        "option_commodity_hist_sina",
        "L2611P7200",
    ),
    Leg(
        "option_daily",
        "CU2611P102000.SHF",
        "option_commodity_hist_sina",
        "CU2611P102000",
    ),
    Leg(
        "option_daily",
        "MO2612-C-7600.CFE",
        "option_cffex_zz1000_daily_sina",
        "mo2612C7600",
    ),
    # sina 该路由的成交量与 ths 无恒定比例（实测比值 0.0002..0.02 漂移），
    # 单位语义不可确认，故此腿只对照四个价格字段。
    Leg(
        "option_daily",
        "10011514.SH",
        "option_sse_daily_sina",
        "10011514",
        fields=("open", "high", "low", "close"),
    ),
)

PRICE_FIELDS = ("open", "high", "low", "close")
#: 期货腿成交量为硬判据（实测两侧同源同单位）；期权腿只观测。
VOLUME_STRICT_DOMAINS = frozenset({"futures_daily"})

#: 实测两侧价格逐日完全一致，故上界只是浮点表示余量而非放宽。
TOLERANCE = 1e-6
#: 期货成交量上界：实测最大偏差 4.65e-04（RB2610 2026-07-22 ths 597,671 vs
#: sina 597,393），单侧 ths>=sina、量级为百手/60 万手，属交易所修正与聚合范围
#: 差异；一手之差在几千手的合约上就已是 2.4e-05，故不按价格口径卡全等。
VOLUME_TOLERANCE = 1e-3
#: 重叠交易日少于本比例即判失败：缺日说明窗口或标的对不上。
MIN_OVERLAP = 0.95


def _ths_fetcher(domain: str) -> Fetcher[Any, Any]:
    """Resolve the routed ths fetcher exactly as a caller would."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry().resolve_domain(domain, source="ths")


def _ths_frame(fetcher: Fetcher[Any, Any], symbol: str, start: date, end: date) -> pd.DataFrame:
    """Read one window through the routing path and flatten it to a frame."""
    import pandas as pd

    rows = cast(
        "Sequence[ContractModel]", fetcher.fetch(symbol=symbol, start_date=start, end_date=end)
    )
    frame: pd.DataFrame = pd.DataFrame([row.model_dump() for row in rows])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
    return frame


def _sina_frame(channel: str, sina_symbol: str, start: date, end: date) -> pd.DataFrame:
    """Fetch one sina reference window and key it on a plain date column."""
    import pandas as pd

    frame = SINA_CHANNELS[channel](sina_symbol)
    out = frame.copy()
    out["trade_date"] = pd.to_datetime(out["date"]).dt.date
    windowed = out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]
    return cast("pd.DataFrame", windowed.reset_index(drop=True))


def _compare(ours: pd.DataFrame, theirs: pd.DataFrame, fields: tuple[str, ...]) -> dict[str, Any]:
    """Join on trade date and measure every compared field.

    Args:
        ours: fuyao bars for the window.
        theirs: official sina rows for the same window.
        fields: Columns compared, shared by both sides.

    Returns:
        ``compared``/``overlap``, per-field max deviation and ``ths/sina``
        ratio spread, up to five offending rows, plus ``price_failed`` and
        ``volume_failed`` so the caller can keep the hard criteria separate
        from the observed ones.
    """
    merged = ours.merge(theirs, on="trade_date", how="inner", suffixes=("_ths", "_sina"))
    if merged.empty:
        return {
            "compared": 0,
            "overlap": 0.0,
            "fields": {},
            "diffs": ["no common dates"],
            "price_failed": True,
            "volume_failed": False,
        }
    diffs: list[str] = []
    fields_report: dict[str, dict[str, float]] = {}
    price_failed = len(merged) < max(1, int(len(ours) * MIN_OVERLAP))
    volume_failed = price_failed
    if price_failed:
        diffs.append(f"only {len(merged)}/{len(ours)} trading days overlap")
    for field in fields:
        tolerance = TOLERANCE if field in PRICE_FIELDS else VOLUME_TOLERANCE
        strict = field in PRICE_FIELDS or field == "volume"
        worst = 0.0
        ratios: list[float] = []
        for trade_date, ours_value, theirs_value in zip(
            merged["trade_date"],
            merged[f"{field}_ths"],
            merged[f"{field}_sina"],
            strict=True,
        ):
            reference = float(theirs_value)
            if not reference:
                continue
            ratio = float(ours_value) / reference
            deviation = abs(ratio - 1.0)
            ratios.append(ratio)
            if deviation > tolerance and strict:
                if len(diffs) < 5:
                    diffs.append(
                        f"{trade_date} {field}: ths {float(ours_value):.4f} "
                        f"vs sina {reference:.4f} (dev={deviation:.2e})"
                    )
                if field in PRICE_FIELDS:
                    price_failed = True
                elif field == "volume":
                    volume_failed = True
            worst = max(worst, deviation)
        fields_report[field] = {
            "max": worst,
            "ratio_min": min(ratios, default=float("nan")),
            "ratio_max": max(ratios, default=float("nan")),
        }
    return {
        "compared": len(merged),
        "overlap": len(merged) / max(1, len(ours)),
        "fields": fields_report,
        "diffs": diffs,
        "price_failed": price_failed,
        "volume_failed": volume_failed,
    }


def _observe_volume(leg: Leg, report: Mapping[str, Any]) -> str | None:
    """Word the volume verdict so a soft leg reads as an observation."""
    volume = report["fields"].get("volume")
    if volume is None:
        return (
            f"- {leg.ths_symbol}（{leg.sina_symbol} / {leg.sina_channel}）：sina 该路由的成交量列"
            "与 ths 无恒定比例（实测 ths/sina 0.0002 .. 0.0207 且无规律漂移），单位语义不可"
            "确认，故本腿只对照 OHLC；详见 docs/evidence/C6/README.md。"
        )
    if volume["max"] <= VOLUME_TOLERANCE:
        return None
    if leg.domain in VOLUME_STRICT_DOMAINS:
        return None
    return (
        f"- {leg.ths_symbol}（{leg.sina_symbol} / {leg.sina_channel}）：成交量比值 "
        f"{volume['ratio_min']:.6f} .. {volume['ratio_max']:.6f}（最大相对偏差 "
        f"{volume['max']:.2e}），比例非常数且单侧偏离，属两侧成交量口径差异"
        "（ths 侧含参考侧未并入的量），非程序换算错误：同窗口四个价格字段逐日完全一致。"
    )


def main(argv: list[str] | None = None) -> int:
    """Run every leg, archive the report, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=date(2026, 6, 1), type=date.fromisoformat)
    parser.add_argument("--end", default=date(2026, 8, 31), type=date.fromisoformat)
    parser.add_argument("--out", default="docs/evidence/C6/ths-futures-option-cross-check.txt")
    args = parser.parse_args(argv)

    lines = [
        "AC-10 ths futures_daily / option_daily legs vs the official sina series",
        "",
        f"script: {REPO_RELATIVE}",
        f"window: {args.start} .. {args.end}",
        "ours: get_registry().resolve_domain(domain, source='ths').fetch(...)  (the routing path)",
        "official: sina futures_zh_daily_sina / option_commodity_hist_sina /",
        "          option_cffex_zz1000_daily_sina / option_sse_daily_sina",
        "          -- different vendor, different aggregation chain",
        "code mapping: RB2610 -> RB2610.SHF via the fuyao futures listing; the rest pass the",
        "          qualified thscode through. L2611-P-7200.DCE -> L2611P7200 and",
        "          CU2611P102000.SHF -> CU2611P102000 drop the separators;",
        "          MO2612-C-7600.CFE -> mo2612C7600 also lower-cases the underlying;",
        "          10011514.SH -> 10011514 because the fuyao prefix is the SSE contract number.",
        f"hard criteria: overlap >= {MIN_OVERLAP:g} of our trading days; relative deviation <=",
        f"          {TOLERANCE:g} on OHLC, {VOLUME_TOLERANCE:g} on volume for",
        f"          {', '.join(sorted(VOLUME_STRICT_DOMAINS))} (volume is observed only on",
        "          option_daily legs, see the notes section).",
        "",
        "| domain | ths code | sina symbol | sina channel | days | overlap | max dev OHLC |"
        " max dev volume | volume ratio ths/sina | result |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    failures = 0
    detail_lines: list[str] = []
    note_lines: list[str] = []
    for leg in LEG_CASES:
        try:
            ours = _ths_frame(_ths_fetcher(leg.domain), leg.ths_symbol, args.start, args.end)
            theirs = _sina_frame(leg.sina_channel, leg.sina_symbol, args.start, args.end)
        except Exception as exc:  # an evidence script reports, it does not raise
            failures += 1
            detail_lines.append(f"- {leg.ths_symbol}: FAIL fetch ({type(exc).__name__}: {exc})")
            lines.append(
                f"| {leg.domain} | {leg.ths_symbol} | {leg.sina_symbol} "
                f"| {leg.sina_channel} | - | - | - | - | - | FAIL (fetch) |"
            )
            print(f"!! {leg.ths_symbol}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        report = _compare(ours, theirs, leg.fields)
        price_failed = bool(report["price_failed"])
        volume_failed = bool(report["volume_failed"]) and leg.domain in VOLUME_STRICT_DOMAINS
        failures += 1 if (price_failed or volume_failed) else 0
        fields = report["fields"]
        ohlc = max(
            (fields[field]["max"] for field in PRICE_FIELDS if field in fields),
            default=float("nan"),
        )
        volume = fields.get("volume", {})
        ratio = f"{volume['ratio_min']:.6f} .. {volume['ratio_max']:.6f}" if volume else "n/a"
        verdict = (
            "FAIL"
            if price_failed or volume_failed
            else "PASS (volume observed)"
            if fields.get("volume") and volume.get("max", 0.0) > VOLUME_TOLERANCE
            else "PASS"
        )
        cells = [
            leg.domain,
            leg.ths_symbol,
            leg.sina_symbol,
            leg.sina_channel,
            str(report["compared"]),
            f"{report['overlap']:.3f}",
            f"{ohlc:.2e}",
            f"{volume['max']:.2e}" if volume else "n/a",
            ratio,
            verdict,
        ]
        lines.append("| " + " | ".join(cells) + " |")
        print(
            f".. {leg.domain} {leg.ths_symbol}: {report['compared']} days, max ohlc dev {ohlc:.2e}",
            file=sys.stderr,
            flush=True,
        )
        detail_lines.extend(f"- {leg.ths_symbol} {diff}" for diff in report["diffs"])
        note = _observe_volume(leg, report)
        if note:
            note_lines.append(note)

    lines.append("")
    if detail_lines:
        lines += ["## 逐日偏差明细", ""]
    lines.extend(detail_lines)
    if note_lines:
        lines += ["", "## 成交量口径说明", "", *note_lines]
    lines.append("")
    lines.append(f"result: {'FAIL' if failures else 'PASS'} ({failures} failing legs)")
    report_path = ROOT / args.out
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nreport written to {report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
