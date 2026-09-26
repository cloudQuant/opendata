"""C24: does every akshare fallback leg answer the same question as the leg behind it?

``opendata/data/authority.json`` is the order ``source="auto"`` tries legs in,
and ``GET /api/v1/data/sources`` publishes it. C23 proved each listed source
registers a capability; it could not prove the second entry could *take over*.
This script asks that question per listed akshare leg, live and read-only:

1. **可达** - does the leg answer with the parameters the patrol uses for the
   domain's verified leg? Reusing them is the point: a fallback is only a
   fallback if the same keyword arguments still ask the same question.
2. **窗口遵守** - when the caller names ``start_date``/``end_date``, does every
   returned row lie inside that range? The sina upstreams hand back a whole
   history, so this is where a fallback silently answers more than asked.
3. **逐字段一致** - on the keys both legs share, how far apart are the contract
   fields, and does the fallback carry a field or row the primary does not?

Verdicts are three-state on purpose. ``PASS`` and ``MISMATCH`` are readings;
``NO_READING`` means no reading was taken (the eastmoney push hosts refuse this
machine), which is evidence about the network and not about the data - so it
must never be counted as a passing check, and never as a failing one either.

The comparison is deliberately asymmetric: a fallback that carries *more* rows
or fields than the primary still answers the question, so extra readings are
reported but do not break agreement. A fallback that drops rows or publishes a
field the primary has as absent has stopped answering, and that is a MISMATCH.

The script pins what the last review measured (:data:`CASES`). Drift - a leg
that starts agreeing, stops answering, or moves to another verdict class -
exits non-zero, because a silently stale verdict table is the same over-claim
this series has been removing one row at a time.

Usage (py313 env; needs the fuyao credentials for the reference legs):
    python scripts/ops/akshare_fallback_cross_check.py [--domain stock_daily ...]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher, QueryParams
    from opendata.data.registry import ProviderRegistry

#: One probe request; the same shape ``patrol.ProbeParams`` uses.
Params: TypeAlias = dict[str, str | date]

#: Verdict classes. Only the first two are readings.
VERDICT_OK = "PASS"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_NO_READING = "NO_READING"

#: Relative tolerance on price-like fields. ths rounds to 2 decimals and sina
#: publishes its own rounding, so anything past this is a series disagreement
#: rather than a formatting difference (same bound the C9/C11 index checks used).
PRICE_TOLERANCE = 5e-5
#: Relative tolerance on ``volume``: vendors aggregate the tape on slightly
#: different cutoffs, so a small per-day gap is not a definition break.
VOLUME_TOLERANCE = 5e-3

#: Fields where ``0.0`` means the upstream does not publish the quantity at all
#: (sina futures bars carry ``amount=0``), so it is read as "absent" and the
#: loss-vs-extra comparison applies instead of a gap.
ZERO_MEANS_ABSENT = frozenset({"amount"})

BAR_FIELDS = ("open", "high", "low", "close", "volume", "amount")
ACTION_FIELDS = ("cash_dividend", "stock_dividend", "rights_shares", "rights_price")


@dataclass(frozen=True)
class Case:
    """One domain's comparison recipe and its pinned verdict.

    Attributes:
        domain: Domain whose akshare leg is being judged.
        key_field: Contract field naming the row (a trade date, a constituent
            code, a statement item).
        fields: Contract fields compared on shared keys.
        windowed: Whether the probe names ``start_date``/``end_date`` and both
            upstreams can honour a range (period-keyed legs cannot).
        expected: Verdict the last review measured.
        note: Why that verdict was measured, for the archive.
    """

    domain: str
    key_field: str
    fields: tuple[str, ...]
    windowed: bool
    expected: str
    note: str


CASES: tuple[Case, ...] = (
    Case(
        "stock_daily",
        "trade_date",
        BAR_FIELDS,
        True,
        VERDICT_NO_READING,
        "东财 push2his/push2 对本机拒答（RemoteDisconnected / push2delay 502）",
    ),
    Case(
        "index_daily",
        "trade_date",
        BAR_FIELDS,
        True,
        VERDICT_NO_READING,
        "同上：与 stock_daily 共用东财日线通道",
    ),
    Case(
        "fund_etf_daily",
        "trade_date",
        BAR_FIELDS,
        True,
        VERDICT_NO_READING,
        "同上：fund_etf_hist_em 走东财同一主机",
    ),
    Case(
        "futures_daily",
        "trade_date",
        BAR_FIELDS,
        True,
        VERDICT_MISMATCH,
        "sina 日线不给成交额：amount 主源 4.05e10 量级、退路恒为 0（缺字段）",
    ),
    Case(
        "option_daily",
        "trade_date",
        BAR_FIELDS,
        True,
        VERDICT_MISMATCH,
        "同价不同量：volume 两侧比值 1e3~1e4 且不稳定，不是单位差",
    ),
    Case(
        "stock_action",
        "ex_date",
        ACTION_FIELDS,
        True,
        VERDICT_OK,
        "探针窗口内 4/4 事件逐字段相同（窗口越界缺陷已由 normalize 修掉）；"
        "但转正要看加宽样本：6 只×10 年 76 个事件里 601318 的 2018-06-07 派息 "
        "ths=1.2 / akshare=1.0，故 verified 位保持 false。该缺口已归因到 sina "
        "同页那条「进度=预案」的无除息日行（派息 2.0/10），1.0+0.2=1.2，现在 "
        "_report_drops 会把它告警出来（C26）。另：这 4 个事件里没有配股事件，"
        "rights_shares/rights_price 两侧都是 0=0，本轮的配股单位修复不受这条 "
        "PASS 约束，其判据是东方财富 配股表 逐单元格对账：C26 量到 6 个有除权日的"
        "配股事件，两次跑取并集后每条都 sina 单元格=em 单元格、且适配层现值=em 口径"
        "（单次跑各有 1 个符号因网络未判读），rights_price 被 ÷10 计数 0",
    ),
    Case(
        "index_constituent",
        "symbol",
        ("weight",),
        False,
        VERDICT_OK,
        "该次调用（000300）成分集合逐只相同，权重只在 akshare 侧发布（退路多给字段）；"
        "单代码 PASS 不等于跨指数成立 —— C34 加宽到 21 个代码后，9 个半年调样家族逐只"
        "相同、4 个科创板代码按等量换入换出不符，见 docs/evidence/C34/，该腿仍 verified=false",
    ),
    Case(
        "financial_statement",
        "item",
        ("value",),
        False,
        VERDICT_MISMATCH,
        "科目词表不相交：退化等于换一个问题",
    ),
)


@dataclass(frozen=True)
class Verdict:
    """One domain's reading.

    Attributes:
        domain: Domain under test.
        status: One of :data:`VERDICT_OK`, :data:`VERDICT_MISMATCH`,
            :data:`VERDICT_NO_READING`.
        detail: The numbers the status was decided from, for the archive.
    """

    domain: str
    status: str
    detail: str


@dataclass(frozen=True)
class _Side:
    """One leg's reading: either rows, or why there are none."""

    rows: Sequence[object]
    error: str | None = None


def _cell(row: object, name: str) -> object:
    """Read one contract field from a returned row."""
    if isinstance(row, Mapping):
        return cast("object", row.get(name))
    return cast("object", getattr(row, name, None))


def _as_date(value: object) -> date | None:
    """Coerce a field to a date, None when it is not one."""
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _as_float(value: object) -> float | None:
    """Coerce a field to a float, None when absent or not numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _date_arg(params: Params, name: str) -> date | None:
    """Read one optional window bound out of a probe request."""
    value = params.get(name)
    return value if isinstance(value, date) else None


def _relative_gap(left: float, right: float) -> float:
    """Scale-aware distance between two readings."""
    scale = max(abs(left), abs(right), 1.0e-9)
    return abs(left - right) / scale


_REGISTRY: ProviderRegistry | None = None


def _registry() -> ProviderRegistry:
    """The process registry, with every provider registered once."""
    global _REGISTRY
    if _REGISTRY is None:
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        _REGISTRY = get_registry()
    return _REGISTRY


def _capability(domain: str, source: str) -> Capability:
    """The registered capability for one (domain, source) pair."""
    by_pair = {(cap.domain, cap.source): cap for cap in _registry().capabilities()}
    return by_pair[(domain, source)]


def _leg(domain: str, source: str) -> Fetcher[QueryParams, object]:
    """Resolve one leg of a domain straight from the live registry."""
    asset_class = _capability(domain, source).asset_class
    resolved = _registry().resolve(asset_class, domain, source=source)
    return cast("Fetcher[QueryParams, object]", resolved)


def _probe_params(case: Case) -> Params:
    """The parameters the patrol uses for this domain's verified leg.

    Args:
        case: The domain being judged; the ``ths`` leg supplies the probe.

    Returns:
        Keyword arguments for either leg's ``fetch``.
    """
    from opendata.pipeline import patrol

    return dict(patrol.probe_params(_capability(case.domain, "ths"), _registry()))


def _side(domain: str, source: str, **params: Any) -> _Side:  # noqa: ANN401
    """Fetch one leg, turning any failure into an attributable no-reading.

    ``**params`` is dynamic on purpose: the legs take different query
    surfaces, and ``Fetcher.fetch`` declares ``ctx`` as a keyword, which mypy
    would otherwise refuse to spread a typed mapping into. Same shape as
    ``macro_authority_cross_check._fetch_observations``.
    """
    try:
        fetcher = _leg(domain, source)
        result = fetcher.fetch(**params)
    except Exception as exc:  # any failure means this side has no reading
        return _Side((), f"{type(exc).__name__}: {str(exc)[:200]}")
    return _Side(list(cast("Sequence[object]", result)))


def _project(rows: Sequence[object], case: Case) -> dict[str, dict[str, float | None]]:
    """Index one leg's rows by key, keeping only the compared fields."""
    projected: dict[str, dict[str, float | None]] = {}
    for row in rows:
        key = str(_cell(row, case.key_field))
        values: dict[str, float | None] = {}
        for field in case.fields:
            value = _as_float(_cell(row, field))
            if value == 0.0 and field in ZERO_MEANS_ABSENT:
                value = None
            values[field] = value
        projected.setdefault(key, values)
    return projected


def _stray_rows(rows: Sequence[object], case: Case, params: Params) -> int:
    """Count fallback rows outside the window the caller asked for."""
    start = _date_arg(params, "start_date")
    end = _date_arg(params, "end_date")
    stray = 0
    for row in rows:
        day = _as_date(_cell(row, case.key_field))
        if day is None:
            continue
        if (start is not None and day < start) or (end is not None and day > end):
            stray += 1
    return stray


def _compare(case: Case, params: Params) -> Verdict:
    """Judge one domain: reachability, window adherence, per-field agreement.

    Args:
        case: The comparison recipe.
        params: The probe request shared by both legs.

    Returns:
        The domain's verdict with the deciding numbers.
    """
    primary = _side(case.domain, "ths", **params)
    fallback = _side(case.domain, "akshare", **params)

    if primary.error or fallback.error:
        parts = []
        if primary.error:
            parts.append(f"主源 ths 未答：{primary.error}")
        if fallback.error:
            parts.append(f"退路 akshare 未答：{fallback.error}")
        return Verdict(case.domain, VERDICT_NO_READING, "；".join(parts))

    ref = _project(primary.rows, case)
    cand = _project(fallback.rows, case)
    if not ref:
        return Verdict(
            case.domain,
            VERDICT_NO_READING,
            f"主源 ths 在该探针下返回 0 行，无基准可比（akshare={len(cand)} 键）",
        )
    if not cand:
        return Verdict(
            case.domain,
            VERDICT_MISMATCH,
            f"退路 akshare 返回 0 行，主源 ths 有 {len(ref)} 键：退化后拿不到任何数据",
        )

    stray = _stray_rows(fallback.rows, case, params) if case.windowed else 0
    shared = sorted(set(ref) & set(cand))
    missing_rows = len(ref) - len(shared)

    lost: dict[str, int] = {}
    gained: dict[str, int] = {}
    worst: dict[str, float] = {}
    violations: dict[str, float] = {}
    for key in shared:
        for field in case.fields:
            left = ref[key][field]
            right = cand[key][field]
            if left is None:
                if right is not None:
                    gained[field] = gained.get(field, 0) + 1
                continue
            if right is None:
                lost[field] = lost.get(field, 0) + 1
                continue
            gap = _relative_gap(left, right)
            worst[field] = max(worst.get(field, 0.0), gap)
            tolerance = VOLUME_TOLERANCE if field == "volume" else PRICE_TOLERANCE
            if gap > tolerance:
                violations[field] = max(violations.get(field, 0.0), gap)

    asked = "-"
    if case.windowed:
        start = _date_arg(params, "start_date")
        end = _date_arg(params, "end_date")
        if start is not None and end is not None:
            asked = str((end - start).days + 1)

    reasons: list[str] = []
    if stray:
        reasons.append(f"窗口未遵守：{stray}/{len(fallback.rows)} 行越界")
    if missing_rows:
        reasons.append(f"退路缺行：{missing_rows}/{len(ref)} 个 {case.key_field} 主源有而退路无")
    if lost:
        reasons.append(f"退路字段缺失：{sorted(lost.items())}")
    if violations:
        reasons.append(f"超容差：{ {f: round(v, 6) for f, v in sorted(violations.items())} }")

    status = VERDICT_MISMATCH if reasons else VERDICT_OK
    detail = (
        f"请求窗口={asked} 天 | 键数 ths={len(ref)} akshare={len(cand)} 共同={len(shared)}"
        f" | akshare 越界={stray}"
        f" | 最大相对偏差={ {f: round(v, 6) for f, v in sorted(worst.items())} or '无' }"
        f" | 退路缺失字段={sorted(lost.items()) or '无'}"
        f" | 退路多给字段={sorted(gained.items()) or '无'}"
    )
    if reasons:
        detail = f"{detail} ; 判定理由={'；'.join(reasons)}"
    return Verdict(case.domain, status, detail)


def _provenance() -> list[str]:
    """Header lines describing where and when this reading was taken."""
    import shutil
    import subprocess  # nosec B404

    git = shutil.which("git")  # resolved once: the argv never uses a PATH lookup

    def _git(*args: str) -> str:
        """Run one read-only git command, or "" when git is not installed."""
        if git is None:
            return ""
        completed = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell off
            [git, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip()

    now = datetime.now(timezone.utc).astimezone()
    dirty = len([line for line in _git("status", "--porcelain").splitlines() if line])
    return [
        f"captured_at={now.isoformat(timespec='seconds')}",
        f"branch={_git('rev-parse', '--abbrev-ref', 'HEAD')} "
        f"HEAD={_git('rev-parse', '--short', 'HEAD')}",
        f"本机时区偏移={now.strftime('%z')}",
        f"python={sys.version.split()[0]}  工作树脏文件数={dirty}",
    ]


def run(domains: Sequence[str]) -> tuple[list[Verdict], list[str]]:
    """Take every reading once.

    Args:
        domains: Domains to walk; empty means the whole pinned table.

    Returns:
        The per-domain verdicts and the readings that no longer match
        what :data:`CASES` pins.
    """
    verdicts: list[Verdict] = []
    drift: list[str] = []
    for case in CASES:
        if domains and case.domain not in domains:
            continue
        try:
            params = _probe_params(case)
        except Exception as exc:  # no probe configured for the reference leg
            verdicts.append(
                Verdict(
                    case.domain,
                    VERDICT_NO_READING,
                    f"无法组装探针 {type(exc).__name__}: {str(exc)[:160]}",
                )
            )
            drift.append(f"{case.domain}: 归档={case.expected} 实测=无法组装探针（{case.note}）")
            continue
        verdict = _compare(case, params)
        if verdict.status != case.expected:
            drift.append(
                f"{case.domain}: 归档={case.expected} 实测={verdict.status}（{case.note}）"
            )
            verdict = replace(
                verdict,
                detail=f"{verdict.detail} ; 与归档判定不同（归档={case.expected}：{case.note}）",
            )
        else:
            verdict = replace(verdict, detail=f"{verdict.detail} ; 与归档判定一致")
        verdicts.append(verdict)

    return verdicts, drift


def main() -> int:
    """Print the report and return the process exit code."""
    parser = argparse.ArgumentParser(
        description="Cross-check each akshare fallback leg against the leg it ranks behind."
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=None,
        help=f"only walk these domains (repeatable; default: all {len(CASES)})",
    )
    args = parser.parse_args()

    requested = tuple(args.domain or ())
    unknown = sorted(set(requested) - {case.domain for case in CASES})
    if unknown:
        print(f"未知 domain：{unknown}；可选：{[case.domain for case in CASES]}")
        return 2

    print("===== C24 akshare 退路逐域复核（真机、只读、不写数仓、不打印密钥）=====")
    for line in _provenance():
        print(line)
    print(f"\n===== 复现命令 =====\npython {REPO_RELATIVE}")
    if requested:
        print(f"===== --domain {' '.join(requested)} =====")
    print("\n===== 判定标准 =====")
    print(f"  价格类字段相对容差={PRICE_TOLERANCE}，volume 相对容差={VOLUME_TOLERANCE}")
    print("  PASS=同问题同答案；MISMATCH=量出来了、退路答的不是同一个问题；")
    print("  NO_READING=这一侧没有读数（网络/上游拒绝本机或返回 0 行），既不算通过也不算失败")
    print(f"  0 视为未发布的字段={sorted(ZERO_MEANS_ABSENT)}")

    verdicts, drift = run(requested)
    print("\n===== 逐域读数 =====")
    for verdict in verdicts:
        print(f"  [{verdict.status:12s}] {verdict.domain}")
        print(f"      {verdict.detail}")

    print("\n===== 与归档判据的一致性 =====")
    if drift:
        for line in drift:
            print(f"  DRIFT {line}")
        print("  判据表需要按实测改写（并同步 docs/evidence/C24 与 authority.json 的 verified 位）")
    else:
        print(f"  {len(verdicts)} 个域的判定与 CASES 归档逐条相同")

    passed = sorted(v.domain for v in verdicts if v.status == VERDICT_OK)
    mismatches = sorted(v.domain for v in verdicts if v.status == VERDICT_MISMATCH)
    unreadable = sorted(v.domain for v in verdicts if v.status == VERDICT_NO_READING)
    print(
        f"\n===== 汇总 =====\n  PASS={passed}\n  MISMATCH={mismatches}\n  NO_READING={unreadable}"
    )
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
