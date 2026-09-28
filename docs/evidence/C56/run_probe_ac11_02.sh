#!/usr/bin/env bash
# C56 single-cell recompute: README §8 promises a reading for the one item this round moved
# (AC-11|02). ``--all`` re-reads 41 cells against the ledger; this runs only the cell whose
# measure/judge changed, so the verdict line can be read without paging through the census.
#
# Read-only: the probe reads source files, the dispatch table and this round's archive; the
# pytest node plane it runs happens in a temp dir. Nothing writes to opendata/, tests/ or
# another round's archive.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C56
PROBE=scripts/quality/acceptance_item_probe.py

{
  echo "probe-ac11-02.txt —— AC-11|02 单格复算读数（本轮判据改动直接作用的那一格）"
  echo "===================================================================="
  echo "round: C56"
  echo "ARCHIVE_ROUND=C56"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
  echo "command: python -u scripts/quality/acceptance_item_probe.py --item 'AC-11|02'"
  echo "note: 期望读数是 gap，且补齐面要点名三行（run_error_rows / run_fail_rows / run_ok_rows）——"
  echo "      判据（逐日 shape dev 对 2e-3）一字未改，改的是证据。"
  echo
  echo "===== 正文（未裁剪，含 23/23 节点面与 VERDICT 行）====="
  echo
  python -u $PROBE --item 'AC-11|02' 2>&1
  rc=$?
  echo
  echo "PROBE_ITEM_EXIT=$rc"
} | tee "$DIR/probe-ac11-02.txt"

exit 0
