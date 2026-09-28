#!/usr/bin/env bash
# AC-11|02 attribution face: the C56 cross-source run failed on one symbol (600519 qfq,
# shape dev 3.15e-03). This archive says *where* that deviation lives, so the gap is
# recorded against a known subject instead of against "the synthesis is probably wrong".
#
# Read-only: SELECTs against dwd_stock_daily / dwd_stock_adjust and three official fetches
# (em qfq, em none, sina none). No write, no DDL, no other round's archive touched.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
OUT=docs/evidence/C56/qfq-attribution.txt

{
  echo "qfq-attribution.txt —— AC-11|02 的归因面：那 29 根超容差的 bar 到底在哪一条链上"
  echo "===================================================================="
  echo "round: C56"
  echo "ARCHIVE_ROUND=C56"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
  echo "command: bash docs/evidence/C56/run_qfq_attribution.sh"
  echo "note: 与 qfq-official-akshare.txt 同一个窗口、同一台仓库、同一条官方腿（默认 akshare）。"
  echo "      A 段量我们自己的因子链在窗口里换过几次值；B 段把 ours/official 的比值按连续段"
  echo "      （run）展开，段内 level 已经除掉中位数 anchor，所以 level=1.00000 的段就是「两条链"
  echo "      在这一段完全重合」；C 段用同一条腿的不复权序列和 sina 不复权序列对账，判据是"
  echo "      原始 bar 有没有差异 —— 没有差异，超容差就只可能出在复权链上而不是数据上。"
  echo "      上游 502/空帧按仪器的 3 次退避处理，拿不到就打印 ERROR，不猜。"
  echo ""
  echo "===== 正文（未裁剪：解释器写到 stdout 的全部读数）====="

  python -u - <<'PY'
import importlib.util
import statistics
import sys
from collections import Counter
from datetime import date

from sqlalchemy import create_engine, text

from opendata.core.config import settings

spec = importlib.util.spec_from_file_location("qfq_official_check", "scripts/ops/qfq_official_check.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

engine = create_engine(settings.data_database_url)
SYMBOL = "600519"
START = date(2026, 1, 5)
END = module._factor_ceiling(engine)
TOL = module.TOLERANCE
print(f"symbol: {SYMBOL}   window: {START} .. {END}   tolerance: {TOL:g}")

print()
print("## A. 我们自己的因子链（dwd_stock_adjust）")
with engine.connect() as conn:
    factor_rows = conn.execute(
        text(
            "SELECT trade_date, qfq_factor, hfq_factor FROM dwd_stock_adjust "
            "WHERE symbol = :s AND trade_date BETWEEN :a AND :b ORDER BY trade_date"
        ),
        {"s": SYMBOL, "a": START, "b": END},
    ).all()
print(f"rows in window: {len(factor_rows)}")
levels = []
previous = None
for trade_date, qfq, _ in factor_rows:
    if previous is None or float(qfq) != previous:
        levels.append((str(trade_date)[:10], f"{previous}", f"{float(qfq):.12f}"))
        previous = float(qfq)
print(f"distinct qfq_factor values: {len({float(r[1]) for r in factor_rows})}")
print("qfq_factor 换值的日期（date, 前值, 后值）：")
for day, before, after in levels:
    print(f"  {day}: {before} -> {after}")
if levels:
    inside = [item for item in levels if item[0] != str(START)[:10]]
    print(f"窗口内部换值次数: {len(inside)}")

print()
print("## B. ours/official 比值的连续段（level 已除掉中位数 anchor）")
ours = module._server_side_series(engine, SYMBOL, START, END, "qfq")
official = module._official_series(SYMBOL, START, END, "qfq", module.DEFAULT_OFFICIAL_LEG)
print(f"official leg: {module.DEFAULT_OFFICIAL_LEG}   leg retries: {official.retries}")
pairs = [
    (str(row["trade_date"])[:10], float(row["close"]), official.values[d])
    for row in ours
    if (d := str(row["trade_date"])[:10]) in official.values and official.values[d]
]
print(f"common bars: {len(pairs)}   ours-only: {len(ours) - len(pairs)}")
if pairs:
    anchor = statistics.median(ours_value / off_value for _, ours_value, off_value in pairs)
    print(f"anchor (median ours/official): {anchor:.6f}")
    per_bar = [
        (day, round((ours_value / off_value) / anchor, 5)) for day, ours_value, off_value in pairs
    ]
    runs: list[list] = []
    for day, level in per_bar:
        if runs and runs[-1][2] == level:
            runs[-1][1] = day
            runs[-1][3] += 1
        else:
            runs.append([day, day, level, 1])
    print(f"{'start':<12} {'end':<12} {'bars':>4}  {'level':>8}  {'|level-1|':>9}  over-tol")
    for start_day, end_day, level, bars in runs:
        dev = abs(level - 1.0)
        print(
            f"{start_day:<12} {end_day:<12} {bars:>4}  {level:8.5f}  {dev:9.2e}  "
            f"{'YES' if dev > TOL else 'no'}"
        )
    over = [run for run in runs if abs(run[2] - 1.0) > TOL]
    print(
        f"超容差的连续段: {len(over)} 段 / {sum(run[3] for run in over)} 根 bar"
        "（对照 qfq-official-akshare.txt 的 over tolerance 计数，两处应当同为 29）"
    )
    profile = Counter(level for _, level in per_bar)
    print(
        "level 分布（按根数，全局而非按段）: "
        + " ".join(f"{level}x{n}" for level, n in profile.most_common())
    )

print()
print("## C. 原始 bar 对账（同一条 em 腿的不复权序列 + sina 不复权序列）")
with engine.connect() as conn:
    raw_rows = conn.execute(
        text(
            "SELECT trade_date, close FROM dwd_stock_daily "
            "WHERE symbol = :s AND trade_date BETWEEN :a AND :b ORDER BY trade_date"
        ),
        {"s": SYMBOL, "a": START, "b": END},
    ).all()
raw = {str(day)[:10]: float(close) for day, close in raw_rows}
print(f"ours (dwd_stock_daily, layer=dwd): {len(raw)} bars")
for leg in ("akshare", "sina"):
    try:
        series = module._official_series(SYMBOL, START, END, "", leg)
    except Exception as exc:  # evidence script reports, it does not raise
        print(f"{leg} none: ERROR {type(exc).__name__}: {exc}")
        continue
    common = {day: value for day, value in series.values.items() if day in raw}
    diffs = [
        (day, raw[day], value, abs(raw[day] / value - 1.0))
        for day, value in sorted(common.items())
        if abs(raw[day] / value - 1.0) > 1e-6
    ]
    worst = max((item[3] for item in diffs), default=0.0)
    print(
        f"{leg} none (retries={series.retries}): {len(series.values)} bars, "
        f"common with ours {len(common)}, 差异 bar {len(diffs)}, 最大相对差 {worst:.2e}"
    )
    for day, ours_value, their_value, rel in diffs[:8]:
        print(f"  {day}: ours {ours_value} vs {leg} {their_value} (rel {rel:.2e})")

print()
print("## D. 机制检验：em 的 qfq 是不是「价格减现金分红」的减法链")
# ours = raw x f，f 在事件前是窗口内唯一的常数。若官方链把这笔分红按减法处理
# （official = raw - D，D 为每股现金分红；窗口内 A 段只量到一个事件），则
#   official/raw = 1 - D/raw  ->  official/raw 与 1/raw 成一条直线，截距应当是 1。
# 直线由事件前的两端点定出，其余每一根 bar 都是对它的检验：残差落在 2 位小数的
# 量化噪声内（<=0.01 元）就说明这条链的形状是「减法」；远大于 0.01 元则这个说法
# 被当场证伪，只能记成「两条链形状对不上」。raw 用 C 段已核对过的那一份（sina 与
# ours 131/131 逐根相等），所以这里只需要 em 的一次 qfq 取数。
EVENT_DAY = "2026-06-26"
try:
    em_qfq = module._official_series(SYMBOL, START, END, "qfq", module.DEFAULT_OFFICIAL_LEG)
except Exception as exc:  # upstream 502/empty: say so, do not fit on a half series
    print(f"ERROR {type(exc).__name__}: {exc}")
    print("D 段没有取到官方 qfq 序列，这份档案不足以支撑机制结论 —— 退出码记为 3，别当完整跑读。")
    sys.exit(3)
else:
    pre = [(d, raw[d], em_qfq.values[d]) for d in sorted(em_qfq.values) if d < EVENT_DAY and raw.get(d)]
    post = [(d, raw[d], em_qfq.values[d]) for d in sorted(em_qfq.values) if d >= EVENT_DAY and raw.get(d)]
    print(f"事件（{EVENT_DAY}）前 {len(pre)} 根 / 后 {len(post)} 根")
    if len(pre) >= 2:
        (d0, r0, q0), (d1, r1, q1) = pre[0], pre[-1]
        slope = (q1 / r1 - q0 / r0) / (1.0 / r1 - 1.0 / r0)
        intercept = q1 / r1 - slope / r1
        residual = [abs((intercept + slope / r) * r - q) for _, r, q in pre]
        print(
            f"fit official/raw = {intercept:.9f} + {slope:.6f} x (1/raw)"
            f"（由 {d0} 与 {d1} 两点定线）"
        )
        print(f"  截距 - 1 = {intercept - 1.0:+.3e}   隐含每股分红 D = {-slope:.4f} 元")
        print(
            f"  其余 {len(pre) - 2} 根的残差（元）: max {max(residual):.4f}"
            f"  median {statistics.median(residual):.4f}"
            f"  >0.01 元的根数 {sum(1 for item in residual if item > 0.01)}"
        )
    worst_post = max((abs(q - r) for _, r, q in post), default=0.0)
    print(
        f"事件之后 official 与 raw 的最大绝对差: {worst_post:.4f} 元"
        "（0.00 就是一条以最新价为锚、事件后不再缩放的前复权）"
    )
    print(
        "判读：残差在分量级 = 「减法链」这个说法成立；否则不成立。事件之后逐根相等"
        "说明两条链只在事件之前分道，形状差来自分红怎么落到价格上，而不是谁的数据错。"
    )
PY
  echo "ATTRIB_PYTHON_EXIT=$?"
} 2>&1 | tee "$OUT"
