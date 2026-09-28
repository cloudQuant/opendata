#!/usr/bin/env bash
# C57 probe face: the two AC-8 cells this round made decidable, plus the
# counterfact self-test that proves every registered break still bites.
#
# ``--item`` re-reads one cell (fast: AC-8|05/|06 measure source + this round's
# live archive, no node plane); ``--self-test`` re-runs all 43 probes and, for
# each one, checks that the declared repair reads proven and every counterfact
# reads gap. Read-only: nothing here writes to opendata/, tests/ or another
# round's archive.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C57
PROBE=scripts/quality/acceptance_item_probe.py
PASS="${1:-items}"

if [ "$PASS" = "items" ]; then
  OUT="$DIR/probe-items-ac8.txt"
  {
    echo "probe-items-ac8.txt —— AC-8|05 / AC-8|06 两格复算读数（本轮新增判据直接作用的两格）"
    echo "===================================================================="
    echo "round: C57"
    echo "ARCHIVE_ROUND=C57"
    echo "date: $(date '+%Y-%m-%d %H:%M %z')"
    echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
    echo "python: $(python -V 2>&1)"
    echo "command: python -u scripts/quality/acceptance_item_probe.py --item 'AC-8|05' && ... --item 'AC-8|06'"
    echo "note: 两格都期望 gap，且 gap 的理由必须点名「等一次经确认的执行」那两面"
    echo "      （生产仓库的 REORGANIZE / e2e 跨年真跑），不是把判据写宽。"
    echo
    echo "===== 正文（未裁剪）====="
    echo
    python -u $PROBE --item 'AC-8|05' 2>&1
    rc1=$?
    echo
    python -u $PROBE --item 'AC-8|06' 2>&1
    rc2=$?
    echo
    echo "PROBE_ITEM_EXIT ac8_05=$rc1 ac8_06=$rc2"
  } | tee "$OUT"
else
  OUT="$DIR/probe-self-test.txt"
  {
    echo "probe-self-test.txt —— 全量反事实自测（43 格：repair 必判 proven，每条 break 必翻红）"
    echo "===================================================================="
    echo "round: C57"
    echo "ARCHIVE_ROUND=C57"
    echo "date: $(date '+%Y-%m-%d %H:%M %z')"
    echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
    echo "python: $(python -V 2>&1)"
    echo "command: python -u scripts/quality/acceptance_item_probe.py --self-test"
    echo "note: 本轮新增 AC-8|05（9 条反事实）与 AC-8|06（6 条）。判据没有为过关放宽过。"
    echo
    echo "===== 正文（未裁剪）====="
    echo
    python -u $PROBE --self-test 2>&1
    rc=$?
    echo
    echo "PROBE_SELF_TEST_EXIT=$rc"
  } | tee "$OUT"
fi

exit 0
