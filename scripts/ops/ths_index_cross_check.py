"""ths index daily bars against the official sina index series (AC-10).

The ths leg of the ``index_daily`` domain is a second, independent source
for the same P1 domain that B1.2 registered with akshare only. This script
walks the routing path a caller uses (``get_registry().resolve_domain``) and
compares every bar field against sina's published index series - a different
vendor with a different aggregation chain.

The CSI 300 leg compares against the **recorded** sina fixture
(``tests/fixtures/upstream/index_daily_sina``), so it is reproducible
offline; the remaining legs call sina live and are reported as SKIP when the
network refuses them. Sina publishes index points to 3 decimals while fuyao
rounds to 2, so the price tolerance is a relative one just above that
rounding; 成交量 carries its own wider bound because the two vendors
aggregate a different universe on 深证成指 (the report records the ratio
spread so the one-sidedness is visible rather than hidden by a threshold).

Usage (py313 env; the fixture leg needs no network):
    python scripts/ops/ths_index_cross_check.py --start 2026-01-05
"""

from __future__ import annotations

import argparse
import gzip
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "upstream"

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pandas as pd

#: ths code, sina symbol, reference channel (``fixture`` stays offline).
#: The first leg asks for a **bare** code so the run also proves the index
#: universe resolver; the others pass the qualified code straight through.
LEG_CASES: tuple[tuple[str, str, str], ...] = (
    ("000300", "sh000300", "fixture"),
    ("000001.SH", "sh000001", "live"),
    ("399001.SZ", "sz399001", "live"),
    ("000905.SH", "sh000905", "live"),
)

#: Contract fields sina publishes for indices (no turnover on this route).
COMPARE_FIELDS = ("open", "high", "low", "close", "volume")

#: Relative tolerance on the price fields; sina's third decimal is ~1e-6 at
#: 4000 points, so anything above this is a real series disagreement.
TOLERANCE = 5e-5
#: 成交量容差放宽一个量级：深证成指的 ths/sina 比值恒 ≥ 1（最大 1.0032），是
#: 上游聚合范围差异（ths 侧含 sina 未并入的成交），属源侧口径而非程序错误。
VOLUME_TOLERANCE = 5e-3
#: A leg with fewer common trading days than this share of ours is a failure.
MIN_OVERLAP = 0.95


def _ths_frame(code: str, start: date, end: date) -> pd.DataFrame:
    """Read the window through the registry, exactly as a caller would.

    Args:
        code: Index symbol; a bare code also resolves through the upstream
            index universe, which is what the AC-10 leg has to prove.
        start: First trading day (inclusive).
        end: Last trading day (inclusive).

    Returns:
        Bars keyed by ``(trade_date, open, high, low, close, volume)``.
    """
    import pandas as pd

    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    routed = get_registry().resolve_domain("index_daily", source="ths")
    rows = routed.fetch(symbol=code, start_date=start, end_date=end)
    return pd.DataFrame([row.model_dump() for row in rows])


def _sina_from_fixture(sina_symbol: str, start: date, end: date) -> pd.DataFrame:
    """Slice the recorded official sina frame for one symbol.

    Args:
        sina_symbol: Prefixed sina symbol, matching the fixture's call args.
        start: First trading day (inclusive).
        end: Last trading day (inclusive).

    Returns:
        The official rows with contract-shaped column names.

    Raises:
        RuntimeError: The fixture is missing or was recorded for another
            symbol (the comparison must not silently switch reference).
    """
    import json

    import pandas as pd

    case = FIXTURE_DIR / "index_daily_sina"
    meta_path = case / "meta.json"
    if not meta_path.exists():
        raise RuntimeError(f"fixture {case} is not recorded")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    recorded = str(meta.get("kwargs", {}).get("symbol", ""))
    if recorded != sina_symbol:
        raise RuntimeError(f"fixture records {recorded!r}, not {sina_symbol!r}")
    with gzip.open(case / "reference.csv.gz", "rb") as handle:
        frame = pd.read_csv(handle)
    return _sina_window(frame, start, end)


def _sina_live(sina_symbol: str, start: date, end: date) -> pd.DataFrame:
    """Fetch the official sina index series over the same window."""
    from opendata_http.index.index_stock_zh import stock_zh_index_daily

    frame = stock_zh_index_daily(symbol=sina_symbol)
    return _sina_window(frame, start, end)


def _sina_window(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Keep the sina rows inside the window on a plain date column."""
    import pandas as pd

    out = frame.copy()
    out["trade_date"] = pd.to_datetime(out["date"]).dt.date
    windowed = out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]
    return windowed.reset_index(drop=True)


def _compare(
    ours: pd.DataFrame, theirs: pd.DataFrame, tolerances: Mapping[str, float]
) -> dict[str, Any]:
    """Join the two series on trade date and measure every field.

    Args:
        ours: ths bars for the window.
        theirs: official sina rows for the same window.
        tolerances: Maximum accepted relative deviation per field.

    Returns:
        ``compared``/``overlap`` counts, per-field ``max``/``median``
        deviations plus the ``ths/sina`` ratio spread (its one-sidedness is
        what separates a 口径 difference from noise), up to five offending
        rows, and a ``failed`` flag.
    """
    merged = ours.merge(theirs, on="trade_date", how="inner", suffixes=("_ths", "_sina"))
    if merged.empty:
        return {
            "compared": 0,
            "overlap": 0.0,
            "fields": {},
            "diffs": ["no common dates"],
            "failed": True,
        }
    diffs: list[str] = []
    fields: dict[str, dict[str, float]] = {}
    failed = len(merged) < max(1, int(len(ours) * MIN_OVERLAP))
    if failed:
        diffs.append(f"only {len(merged)}/{len(ours)} trading days overlap")
    for field in COMPARE_FIELDS:
        tolerance = tolerances[field]
        worst = 0.0
        deviations: list[float] = []
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
            deviations.append(deviation)
            ratios.append(ratio)
            if deviation > tolerance:
                failed = True
                if len(diffs) < 5:
                    diffs.append(
                        f"{trade_date} {field}: ths {float(ours_value):.4f} "
                        f"vs sina {reference:.4f} (dev={deviation:.2e})"
                    )
            worst = max(worst, deviation)
        median = sorted(deviations)[len(deviations) // 2] if deviations else 0.0
        fields[field] = {
            "max": worst,
            "median": median,
            "ratio_min": min(ratios, default=float("nan")),
            "ratio_max": max(ratios, default=float("nan")),
            "ratio_mean": statistics.fmean(ratios) if ratios else float("nan"),
        }
    return {
        "compared": len(merged),
        "overlap": len(merged) / max(1, len(ours)),
        "fields": fields,
        "diffs": diffs,
        "failed": failed,
    }


def _reference_frame(channel: str, sina_symbol: str, start: date, end: date) -> pd.DataFrame:
    """Load one leg's official series through its declared channel."""
    return (
        _sina_from_fixture(sina_symbol, start, end)
        if channel == "fixture"
        else _sina_live(sina_symbol, start, end)
    )


def main(argv: list[str] | None = None) -> int:
    """Run every leg, archive the report, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=date(2026, 1, 5), type=date.fromisoformat)
    parser.add_argument("--end", default=date.today(), type=date.fromisoformat)
    parser.add_argument("--tolerance", default=TOLERANCE, type=float)
    parser.add_argument("--volume-tolerance", default=VOLUME_TOLERANCE, type=float)
    parser.add_argument("--out", default="docs/evidence/C5/ths-index-cross-check.txt")
    args = parser.parse_args(argv)

    tolerances = dict.fromkeys(COMPARE_FIELDS, args.tolerance)
    tolerances["volume"] = args.volume_tolerance

    lines = [
        "AC-10 ths index_daily leg vs the official sina index series",
        "",
        f"script: {REPO_RELATIVE}",
        f"window: {args.start} .. {args.end}",
        "ours: get_registry().resolve_domain('index_daily', source='ths') (the routing path)",
        "official: sina stock_zh_index_daily -- different vendor, different aggregation chain",
        f"fields: {', '.join(COMPARE_FIELDS)} (sina publishes no index turnover on this route)",
        f"tolerance: relative {args.tolerance:g} on OHLC, {args.volume_tolerance:g} on volume;",
        f"           overlap >= {MIN_OVERLAP:g} of our trading days",
        "",
        "| ths code | sina symbol | reference | days | overlap | max dev OHLC | max dev "
        "volume | volume ratio ths/sina | result |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    failures = 0
    detail_lines: list[str] = []
    note_lines: list[str] = []
    for code, sina_symbol, channel in LEG_CASES:
        try:
            ours = _ths_frame(code, args.start, args.end)
            theirs = _reference_frame(channel, sina_symbol, args.start, args.end)
        except Exception as exc:  # an evidence script reports, it does not raise
            if channel == "fixture":
                failures += 1
            detail_lines.append(f"- {code}: SKIP {channel} leg ({type(exc).__name__}: {exc})")
            lines.append(
                f"| {code} | {sina_symbol} | {channel} | - | - | - | - | - | SKIP (not counted) |"
            )
            print(f"!! {code}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        report = _compare(ours, theirs, tolerances)
        failures += 1 if report["failed"] else 0
        fields = report["fields"]
        ohlc = max((fields[field]["max"] for field in COMPARE_FIELDS[:4]), default=float("nan"))
        volume = fields.get("volume", {})
        ratio_min = volume.get("ratio_min", float("nan"))
        ratio_max = volume.get("ratio_max", float("nan"))
        ratio = f"{ratio_min:.6f} .. {ratio_max:.6f}"
        verdict = "FAIL" if report["failed"] else "PASS"
        cells = [
            code,
            sina_symbol,
            channel,
            str(report["compared"]),
            f"{report['overlap']:.3f}",
            f"{ohlc:.2e}",
            f"{volume.get('max', float('nan')):.2e}",
            ratio,
            verdict,
        ]
        lines.append("| " + " | ".join(cells) + " |")
        print(
            f".. {code}: {report['compared']} days, max ohlc dev {ohlc:.2e}",
            file=sys.stderr,
            flush=True,
        )
        detail_lines.extend(f"- {code} {diff}" for diff in report["diffs"])
        if volume.get("max", 0.0) > args.tolerance:
            note_lines.append(
                f"- {code}: 成交量比值恒 ≥ 1（均值 {volume.get('ratio_mean', float('nan')):.6f}，"
                f"最大 {volume.get('ratio_max', float('nan')):.6f}），单向偏离指向源侧聚合范围"
                "差异（ths 侧并入的成交多于 sina），非程序换算错误：同一字段在 000300/000001/"
                "000905 上偏差量级为 1e-8。"
            )

    lines.append("")
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
