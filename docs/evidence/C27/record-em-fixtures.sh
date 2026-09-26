#!/usr/bin/env bash
# C27 补录 em 通道 4 个挂起用例：耐心重试，不做「跑不通就当数据错」的读法。
#
# AC-6 的 `--record` 需要 push2his / push2delay 真的答话；本网络对这两个站点是
# 阵发性的（同一台机器几分钟内 200 与 RemoteDisconnected 交替出现，见
# em-channel-measure.txt 与 refusal-shape.txt）。所以这里按「一次一个用例、用例之间
# 长间隔、多轮」的策略重试，每轮跳过已经 recorded 的用例，并把每一轮的现场输出
# 完整留在档里。判据不放宽：只有 `--record` 自己写 recorded 才算补上。
#
# 用法（py313 环境，需要在仓库根）：
#   bash docs/evidence/C27/record-em-fixtures.sh > docs/evidence/C27/record-em-fixtures.txt 2>&1
set -uo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO"

# 4 个 em 挂起用例 + 2 个本轮新增的 sina 宽窗用例（见 compare_with_upstream.CASES
# 的注释：em/sina 的因子链要在同一个跨除权日的窗口里才可比）。已有的 12 个窄窗夹具
# 一概不动，所以这里只列新名字——宽窗 sina 是新增用例，不是改写 stock_daily_sina_raw。
CASES=(
  stock_daily_raw
  stock_daily_qfq
  fund_etf_daily_em
  index_daily_em
  stock_daily_sina_raw_wide
  stock_daily_sina_qfq_wide
)
MAX_ROUNDS="${MAX_ROUNDS:-8}"
SLEEP_SECONDS="${SLEEP_SECONDS:-75}"

status_of() {
  python - "$1" <<'PY'
import json
import sys
from pathlib import Path

case = sys.argv[1]
meta = Path("tests/fixtures/upstream") / case / "meta.json"
if not meta.exists():
    print("no-meta")
else:
    print(json.loads(meta.read_text(encoding="utf-8")).get("status", "?"))
PY
}

echo "start=$(date -Iseconds)"
for round in $(seq 1 "$MAX_ROUNDS"); do
  remaining=0
  for case in "${CASES[@]}"; do
    current="$(status_of "$case")"
    if [ "$current" = "recorded" ]; then
      echo "[round $round] $case 已 recorded，跳过"
      continue
    fi
    remaining=$((remaining + 1))
    echo "[round $round] $case 尝试补录（补录前状态=$current）"
    python -u scripts/codemod/compare_with_upstream.py --record --only "$case" 2>&1 | sed "s/^/    /"
    rc=${PIPESTATUS[0]}
    after="$(status_of "$case")"
    echo "    -> $case 补录后状态=$after rc=$rc"
    sleep "$SLEEP_SECONDS"
  done
  if [ "$remaining" -eq 0 ]; then
    echo "all recorded at round $round"
    break
  fi
done

echo
echo "===== 最终状态表 ====="
for case in "${CASES[@]}"; do
  echo "$case status=$(status_of "$case")"
done
echo "RECORD_EXIT=$([ "$(status_of stock_daily_raw)" = recorded ] \
  && [ "$(status_of stock_daily_qfq)" = recorded ] \
  && [ "$(status_of fund_etf_daily_em)" = recorded ] \
  && [ "$(status_of index_daily_em)" = recorded ] && echo 0 || echo 1)"
