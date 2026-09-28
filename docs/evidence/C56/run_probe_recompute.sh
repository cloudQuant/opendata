#!/usr/bin/env bash
# C56 probe recompute face: the probe's AC-11|02 measure/judge changed this round (dispatch
# table + four source readings + three archive readings), so every counterfact has to bite
# again and the whole 41-probe pass has to be re-read against the ledger.
#
# Read-only against the repository: the probe reads files and runs pytest node planes in a
# temp dir; nothing here writes to opendata/, tests/ or another round's archive.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C56
PROBE=scripts/quality/acceptance_item_probe.py

header() {
  echo "round: C56"
  echo "ARCHIVE_ROUND=C56"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
}

{
  echo "probe-self-test.txt —— 反事实面：每条 break 都要把干净读数打回 gap，且声明的补齐读数能到 proven"
  echo "===================================================================="
  header
  echo "command: python -u scripts/quality/acceptance_item_probe.py --self-test"
  echo "note: AC-11|02 本轮新增 12 条 break，并修了一条 unreachable judge（声明的补齐读数把"
  echo "      run_ok_rows 抄成当场量到的 0，判据永远到不了 proven —— 由 --self-test 抓到）。"
  echo ""
  echo "===== 正文（未裁剪）====="
  python -u "$PROBE" --self-test 2>&1
  echo "SELF_TEST_EXIT=$?"
} | tee "$DIR/probe-self-test.txt"

{
  echo "probe-all.txt —— 41 个探针全量对账（与台账逐格比 state，并把每格的读数原样写下）"
  echo "===================================================================="
  header
  echo "command: python -u scripts/quality/acceptance_item_probe.py --all"
  echo "note: AC-11|02 应当仍是 gap，且 reason 里三条留档 run 读数（PASS/FAIL/ERROR 行数）"
  echo "      与 qfq-official-akshare.txt 逐字一致。"
  echo ""
  echo "===== 正文（未裁剪）====="
  python -u "$PROBE" --all 2>&1
  echo "PROBE_ALL_EXIT=$?"
} | tee "$DIR/probe-all.txt"

{
  echo "probe-gate-check.txt —— 门禁成员 acceptance-probe-check 的自检面（配方在不在、面有没有漂）"
  echo "===================================================================="
  header
  echo "command: python -u scripts/quality/acceptance_item_probe.py --gate-check"
  echo ""
  echo "===== 正文（未裁剪）====="
  python -u "$PROBE" --gate-check 2>&1
  echo "GATE_CHECK_EXIT=$?"
} | tee "$DIR/probe-gate-check.txt"

tail -3 "$DIR/probe-self-test.txt"
tail -2 "$DIR/probe-all.txt"
tail -2 "$DIR/probe-gate-check.txt"
