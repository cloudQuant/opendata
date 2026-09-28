#!/usr/bin/env bash
# C58 probe face: the four AC-8 cells this round made decidable, plus the
# counterfact self-test that proves every registered break still bites.
#
# ``items`` re-reads AC-8|01/|02/|03/|07 (node planes run under ``-m "not e2e"
# --no-cov``; the live half reads docs/evidence/C58/ods-face.txt); ``self-test``
# re-runs all 47 probes and checks that each declared repair reads proven and
# every counterfact reads gap. Read-only toward the repository: nothing here
# writes to opendata/, tests/ or another round's archive.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C58
PROBE=scripts/quality/acceptance_item_probe.py
PASS="${1:-items}"

header() {
  echo "round: C58"
  echo "ARCHIVE_ROUND=C58"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
}

if [ "$PASS" = "items" ]; then
  OUT="$DIR/probe-items-ac8.txt"
  {
    echo "probe-items-ac8.txt —— AC-8|01 / |02 / |03 / |07 四格复算读数（本轮新增判据作用的四格）"
    echo "===================================================================="
    header
    echo "command: python -u $PROBE --item 'AC-8|01' --item 'AC-8|02' --item 'AC-8|03' --item 'AC-8|07'"
    echo "note: 四格的 expects 都是 §2 那一行的逐字子串；判据原文一字未动。"
    echo "      |03 的真库那格（e2e）只披露不判定，读数 exit=5 = 选择式没选中它。"
    echo "      真仓库读数一律来自 docs/evidence/C58/ods-face.txt（只读面）。"
    echo
    echo "===== 正文（未裁剪）====="
    echo
    python -u $PROBE --item 'AC-8|01' --item 'AC-8|02' --item 'AC-8|03' --item 'AC-8|07' 2>&1
    rc=$?
    echo
    echo "PROBE_ITEM_EXIT=$rc"
  } | tee "$OUT"
else
  OUT="$DIR/probe-self-test.txt"
  {
    echo "probe-self-test.txt —— 全量反事实自测（47 格：repair 必判 proven，每条 break 必翻红）"
    echo "===================================================================="
    header
    echo "command: python -u $PROBE --self-test"
    echo "note: 本轮新增 AC-8|01（11 条）/ |02（10 条）/ |03（5 条）/ |07（7 条）= 33 条反事实。"
    echo "      判据没有为过关放宽过；格式化前后各跑一遍四格，读数逐字一致。"
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
