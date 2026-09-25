"""宏观替代腿的跨 vendor 对照：为 ``economy_*`` 的权威序拿一个测量依据（AC-10）.

``authority.json`` 从不列宏观域，于是 ``source=auto`` 在宏观域上退化成注册顺序
（C21 已复现：换一种文件收集顺序就有 2 条注册用例翻转）。要补一行权威序，
就得回答"同一条 (period, market) 上两条腿该先走谁"。这个问题不能凭偏好回答，
所以这里把候选腿放到**同一个请求**上真机对一次，量三件事：

1. **定义一致性**（J1）：同一参考区域、同一观测日期，两个源的数值是否落在同一口径
   （容差是"有没有把百分点当成基点、把指数当成环比率"这种量级判据，不是"谁更正确"）；
2. **发布形态**（J2）：把窗口右端故意推到未来（``PROJECTION_PROBE_END``），看哪个
   源会把没有发生的年份当观测值交回来。IMF DataMapper 与 WEO 口径一起发布
   projections（C1 记录过），若它排在权威序第一位，"取最近 N 年"这类调用就会把
   预测值当历史值用掉——这是数据平台不能靠调用方记忆来防的坑，也是两条腿的
   **排序依据**；
3. **量纲可比性**（J3）：两条腿若连量纲/参考区域都不同（欧元区 aggregate vs 英国
   国别），就不存在 value 级的裁判，权威序只能按"谁是该区域的发布方口径"声明，
   并把这个局限写进权威表与验收文档，而不是假装对照过了。

判据不放宽的三条约定：

* 每条腿的请求带**重试与抖动归因**（C20 量过 OECD 的 5xx 抖动、C21 把这条经验
  写进了巡检面）：一次失败记 BLIP，重试到上限仍失败才记取数失败，BLIP 原样进报告，
  不静默吞掉；
* **"读不出"和"读出差异"是两件事**：任一判据只要有一条腿读不出数、或两个源没有
  足够的共同观测点，就记 UNVERIFIABLE 并使退出码为 1；量出"两源在某区域系统性
  不一致"是**发现**，单独打 WARN 并原样给出偏差值——既不降级成"跳过该判据后判绿"，
  也不为了 EXIT=0 去放大容差；
* 对照按**观测日期**对齐而不是按年份：CPI 两条腿是月频，按年份配对会把一个月的值
  和另一个月的值相减并当成测量结果报出来。

两条腿都走**路由路径**（``registry.resolve_domain(..., source=...)``），与生产调用
同一个入口。数仓只读，不写一行；无密钥依赖（四个宏观源里只有 fred 需要 Key，
本轮的替代腿对照不涉及它）。

用法（py313 环境，需公网）：
    python scripts/ops/macro_authority_cross_check.py
退出码：三条判据全部可读返回 0；任何一条读不出结论返回 1。
"""

from __future__ import annotations

import sys
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import MacroSeries
    from opendata.data.protocol import Fetcher

#: 定义一致性容差（百分点）。0.3pp 是"同一现象的两次独立统计"量级，
#: 足以抓住单位/口径错配（那会是 10x 或 100x 的偏差），又不会把
#: IMF（1 位小数）与 OECD（3 位小数）的舍入差判成冲突。
DEFINITION_TOLERANCE_PP = 0.3

#: 对照窗口：两端都是固定历史区间，永不滚动作废（C16 的探针窗口口径）。
WINDOW_START = date(2015, 1, 1)
WINDOW_END = date(2024, 12, 31)

#: J2 的探测窗口右端：推到足够远的未来，才能把"源把预测当观测发布"这件事量出来。
PROJECTION_PROBE_END = date(2031, 12, 31)

#: 一个区域至少要有多少个共同观测点，这次对照才算"量到了东西"。
MIN_SHARED_POINTS = 5

#: 单次请求的尝试次数与退避（OECD 的 5xx 抖动是出了名的，C20 十次里中过一次）。
ATTEMPTS = 4
BACKOFF_SECONDS = 1.5

#: 失业率的全球年代替腿：同一参考区域，两个源。
UNEMPLOYMENT_PAIRS: tuple[tuple[str, str, str], ...] = (
    # (参考区域, imf 国家码, oecd series key)
    ("USA", "USA", "USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE"),
    ("DEU", "DEU", "DEU.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE"),
    ("JPN", "JPN", "JPN.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE"),
)

#: 两条 1M CPI 腿的探针参数（都是探针参数面审计里逐条读通过的真 key）。
ECB_CPI_EU_KEY = "ICP/M.U2.N.000000.4.ANR"
OECD_CPI_EU_KEY = "GBR.M.HICP.CPI.PC.CP09.N.G1"

#: J1 的单区域结论：量到了且一致 / 量到了但不一致 / 读不出数。
AGREED = "AGREED"
DIVERGENT = "DIVERGENT"
UNVERIFIABLE = "UNVERIFIABLE"


class Observation(NamedTuple):
    """One observation point: its date, its value, and published precision."""

    when: date
    value: float
    decimals: int


class Leg(NamedTuple):
    """One fetched leg: its observations and how many tries/blips it took."""

    observations: list[Observation]
    attempts: int
    blips: tuple[str, ...]


class AreaResult(NamedTuple):
    """Judge J1's verdict for one reference area."""

    area: str
    status: str
    shared: int
    worst: float


class UnreadableError(Exception):
    """A leg that did not answer after all retries - a finding, not a pass."""


def _observations(rows: Sequence[Any]) -> list[Observation]:
    """Flatten contract rows into date/value points with their precision.

    ``decimals`` is counted on the string the value renders as, so a
    float tail (``4.4`` arriving as ``4.4000000000000004``) would inflate
    it. It is therefore read only as "which of the two sources published
    the finer figure", never as an absolute precision claim.

    Args:
        rows: ``MacroSeries`` models returned by a fetcher.

    Returns:
        Sorted observations; rows without a value are dropped (a blank
        cell is not a measurement, and counting it would fake agreement).
    """
    points: list[Observation] = []
    for row in rows:
        value = getattr(row, "value", None)
        when: date | None = getattr(row, "date", None)
        if value is None or when is None:
            continue
        text = repr(float(value))
        decimals = len(text.split(".")[1].rstrip("0")) if "." in text else 0
        points.append(Observation(when, float(value), decimals))
    return sorted(points, key=lambda item: item.when)


def _fetch_observations(
    source: str,
    domain: str,
    **params: Any,  # noqa: ANN401  # 各 provider 的参数面不同，这里按原样透传
) -> list[Observation]:
    """Fetch one macro leg once through the routing path and read its points.

    Args:
        source: Explicit provider label (routing never guesses here).
        domain: Macro domain identifier.
        **params: The leg's own query parameters.

    Returns:
        The observations the source published for the request.
    """
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    fetcher: Fetcher[Any, MacroSeries] = get_registry().resolve_domain(domain, source=source)
    result = fetcher.fetch(**params)
    rows = list(result) if not isinstance(result, list) else result
    return _observations(rows)


def fetch_leg(label: str, source: str, domain: str, **params: object) -> Leg:
    """Fetch one leg with retries, attributing every blip it cost.

    Args:
        label: Human-readable name used in the report.
        source: Explicit provider label; routing never guesses here.
        domain: Macro domain identifier.
        **params: The leg's own query parameters.

    Returns:
        The leg (observations plus attempt/blip accounting).

    Raises:
        UnreadableError: All :data:`ATTEMPTS` tries failed.
    """
    blips: list[str] = []
    params.setdefault("start_date", WINDOW_START)
    params.setdefault("end_date", WINDOW_END)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            observations = _fetch_observations(source, domain, **params)
        except Exception as exc:  # a leg that will not answer is a finding
            blips.append(f"{label} #{attempt} {type(exc).__name__}: {str(exc)[:150]}")
            print(f"    BLIP {blips[-1]}", flush=True)
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF_SECONDS)
            continue
        return Leg(observations, attempt, tuple(blips))
    raise UnreadableError(f"{label}: {ATTEMPTS} 次尝试均失败")


def _agree(
    left: Sequence[Observation], right: Sequence[Observation]
) -> tuple[int, float, list[str]]:
    """Compare two sources observation by observation.

    Alignment is on the **observation date**, not the year: the CPI pair is
    monthly, and pairing by year alone would subtract one month's value from
    a different month's and present the gap as if it were a measurement.

    Args:
        left: Observations from the first source.
        right: Observations from the second source.

    Returns:
        ``(shared_points, max_abs_difference, mismatched_lines)``.
    """
    by_date = {item.when: item.value for item in right}
    notes: list[str] = []
    shared = 0
    worst = 0.0
    for item in left:
        other = by_date.get(item.when)
        if other is None:
            continue
        shared += 1
        gap = abs(item.value - other)
        worst = max(worst, gap)
        if gap > DEFINITION_TOLERANCE_PP:
            notes.append(f"{item.when}: {item.value} vs {other} -> {gap:.3f}")
    return shared, worst, notes


def _precision(observations: Sequence[Observation]) -> str:
    """Describe one leg's published precision and coverage end for the report."""
    if not observations:
        return "无读数"
    decimals = min(item.decimals for item in observations)
    last = max(item.when.year for item in observations)
    return f"小数位 {decimals}/末年 {last}"


def judge_unemployment_area(area: str, imf_country: str, oecd_key: str) -> AreaResult:
    """Judge J1 on one reference area: do the two sources measure one thing?

    Args:
        area: Reference area code, for the report.
        imf_country: The same area as the IMF names it.
        oecd_key: The OECD series key carrying that area.

    Returns:
        The area verdict; the lines and the per-point mismatches are printed
        here so the archive keeps them untrimmed.
    """
    try:
        imf = fetch_leg(
            f"imf/{area}", "imf", "economy_unemployment", indicator="LUR", country=imf_country
        )
        oecd = fetch_leg(f"oecd/{area}", "oecd", "economy_unemployment", series_key=oecd_key)
    except UnreadableError as exc:
        print(f"  {area}: UNVERIFIABLE 取数失败 {exc}")
        return AreaResult(area, UNVERIFIABLE, 0, 0.0)
    shared, worst, mismatches = _agree(imf.observations, oecd.observations)
    print(
        f"  {area}: 共同观测点 {shared}（按日期对齐），最大偏差 {worst:.3f}pp；"
        f"IMF {_precision(imf.observations)}，OECD {_precision(oecd.observations)}；"
        f"尝试次数 imf={imf.attempts} oecd={oecd.attempts}"
    )
    for note in mismatches[:4]:
        print(f"    超容差({DEFINITION_TOLERANCE_PP}pp) {note}")
    if len(mismatches) > 4:
        print(f"    超容差观测点合计 {len(mismatches)} 个")
    if shared < MIN_SHARED_POINTS:
        print(f"    UNVERIFIABLE 共同观测点不足 {MIN_SHARED_POINTS} 个：无交集不是证据")
        return AreaResult(area, UNVERIFIABLE, shared, worst)
    return AreaResult(area, DIVERGENT if mismatches else AGREED, shared, worst)


def judge_projection() -> tuple[bool, list[str]]:
    """Judge J2: which leg hands over future years as if they were observed?

    Returns:
        ``(readable, report_lines)`` - readable means both legs answered;
        the lines carry the ranking basis (who pollutes a window that ends
        beyond the last completed year).
    """
    lines: list[str] = []
    last_completed = date.today().year - 1
    try:
        imf = fetch_leg(
            "imf/USA 未来窗",
            "imf",
            "economy_unemployment",
            indicator="LUR",
            country="USA",
            start_date=WINDOW_START,
            end_date=PROJECTION_PROBE_END,
        )
        oecd = fetch_leg(
            "oecd/USA 未来窗",
            "oecd",
            "economy_unemployment",
            series_key="USA.UNE_RATE.PT_LF_SUB._T.Y15T64.UNE",
            start_date=WINDOW_START,
            end_date=PROJECTION_PROBE_END,
        )
    except UnreadableError as exc:
        return False, [f"  UNVERIFIABLE 取数失败 {exc}"]
    beyond: dict[str, int] = {}
    for label, leg in (("IMF", imf), ("OECD", oecd)):
        # 最后一个完整自然年之后的读数没有统计可核对，源仍给值即为预测外推。
        rows = [item for item in leg.observations if item.when.year > last_completed]
        beyond[label] = len(rows)
        years = [item.when.year for item in rows]
        lines.append(
            f"  {label}: {len(leg.observations)} 行，最大年份 "
            f"{max((i.when.year for i in leg.observations), default=0)}，"
            f"晚于最后一个完整自然年 {last_completed} 的读数 {len(rows)} 行 {years[:5]}"
        )
    lines.append(
        f"  判读：窗口右端推到 {PROJECTION_PROBE_END.year} 后，IMF 交出 {beyond['IMF']} 行、"
        f"OECD 交出 {beyond['OECD']} 行晚于 {last_completed} 的读数。"
    )
    winner = "oecd" if beyond["IMF"] > beyond["OECD"] else "imf"
    lines.append(
        f"  排序依据：把不外溢预测值的那条排在前面 ⇒ auto 首选 {winner}"
        "（覆盖面更广的一条保留为替代腿）"
    )
    return True, lines


def judge_cpi_scales() -> tuple[bool, list[str]]:
    """Judge J3: are the two 1M CPI legs even the same measure?

    Returns:
        ``(readable, report_lines)`` - readable means the probe *read* both
        legs and reported their scale, not that they agreed.
    """
    try:
        ecb = fetch_leg("ecb/economy_cpi", "ecb", "economy_cpi", series_id=ECB_CPI_EU_KEY)
        oecd = fetch_leg("oecd/economy_cpi", "oecd", "economy_cpi", series_key=OECD_CPI_EU_KEY)
    except UnreadableError as exc:
        return False, [f"  UNVERIFIABLE 取数失败 {exc}"]
    if not ecb.observations or not oecd.observations:
        return False, [
            f"  UNVERIFIABLE 有一侧没有读数：ecb={len(ecb.observations)} 行, "
            f"oecd={len(oecd.observations)} 行"
        ]
    ecb_mean = sum(item.value for item in ecb.observations) / len(ecb.observations)
    oecd_mean = sum(item.value for item in oecd.observations) / len(oecd.observations)
    shared, worst, _ = _agree(ecb.observations, oecd.observations)
    comparable = shared > 0 and abs(ecb_mean - oecd_mean) <= DEFINITION_TOLERANCE_PP
    lines = [
        f"  ECB  {ECB_CPI_EU_KEY}（欧元区 aggregate, 1M）: {len(ecb.observations)} 行，"
        f"均值 {ecb_mean:.3f}，首末值 {ecb.observations[0].value}/{ecb.observations[-1].value}",
        f"  OECD {OECD_CPI_EU_KEY}（英国国别, 1M）: {len(oecd.observations)} 行，"
        f"均值 {oecd_mean:.3f}，首末值 {oecd.observations[0].value}/"
        f"{oecd.observations[-1].value}",
        f"  共同观测点 {shared}（按日期对齐），最大偏差 {worst:.3f}",
        (
            "  判读：同区域同量级，存在 value 级裁判"
            if comparable
            else f"  判读：均值差 {abs(ecb_mean - oecd_mean):.3f}、参考区域不同"
            "（欧元区 aggregate vs 英国国别）⇒ 不存在 value 级裁判，"
            "权威序按该区域的发布方口径声明（欧元区 CPI 由 ECB 发布）"
        ),
    ]
    return True, lines


def main() -> int:
    """Run the three judges and print an auditable report."""
    print("== 宏观替代腿跨 vendor 对照（真机，只读）==")
    print(f"窗口：{WINDOW_START} ~ {WINDOW_END}；定义一致性容差 {DEFINITION_TOLERANCE_PP}pp")

    print("\n[J1] economy_unemployment / 1A / global：IMF vs OECD 定义一致性（3 个参考区域）")
    areas = [
        judge_unemployment_area(area, imf_country, oecd_key)
        for area, imf_country, oecd_key in UNEMPLOYMENT_PAIRS
    ]
    unreadable = [item.area for item in areas if item.status == UNVERIFIABLE]
    diverged = [item for item in areas if item.status == DIVERGENT]
    agreed = [item for item in areas if item.status == AGREED]

    print("\n[J2] 同一未来窗口的发布形态（排序依据）")
    j2_ok, j2_lines = judge_projection()
    print("\n".join(j2_lines))

    print("\n[J3] economy_cpi 两条 1M 腿的量纲/区域可比性")
    j3_ok, j3_lines = judge_cpi_scales()
    print("\n".join(j3_lines))

    print("\n== 判定 ==")
    results: list[tuple[str, bool]] = [
        (f"J1 三个区域的对照全部可读（共同观测点 ≥{MIN_SHARED_POINTS}）", not unreadable),
        ("J2 预测值污染面已量出，排序依据可读", j2_ok),
        ("J3 两条 1M CPI 腿的口径关系已读出（不要求数值相等）", j3_ok),
    ]
    for label, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    agreed_text = "，".join(f"{item.area} {item.worst:.3f}pp" for item in agreed)
    print(
        f"  {'PASS' if agreed else 'FAIL'} J1 定义一致：{len(agreed)}/{len(areas)} 个区域在 "
        f"{DEFINITION_TOLERANCE_PP}pp 内" + (f"（{agreed_text}）" if agreed else "")
    )
    if diverged:
        diverged_text = "，".join(f"{item.area} 最大 {item.worst:.3f}pp" for item in diverged)
        print(
            f"  WARN J1 读出来的差异（不是取数失败）：{diverged_text}，超出 "
            f"{DEFINITION_TOLERANCE_PP}pp ⇒ 这些区域上两条腿不可互换，权威序不改变这一点，"
            "已登记为未收口项"
        )
    exit_code = 0 if all(passed for _, passed in results) else 1
    print(f"EXIT={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
