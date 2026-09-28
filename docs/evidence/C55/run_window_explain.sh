#!/usr/bin/env bash
# C55: EXPLAIN the default-window query on the real warehouse and archive the plan.
#
# The AC-11|04 probe reads the newest docs/evidence/C*/window-pruning-explain.txt, so two
# pins decide whether a copy of an old archive can pass for fresh evidence:
#   * ARCHIVE_ROUND has to equal the directory the file sits in (copying means editing it), and
#   * query_module_sha in the body has to equal the digest of opendata/pipeline/query.py as it
#     is now — the plan in here belongs to the SQL text that produced it.
# Read-only instrument: EXPLAIN and information_schema selects only, never a write or a DDL.
set -u
export PATH="${HOME}/opt/anaconda3/envs/py313/bin:${PATH}"
export PYTHONPATH=.
cd "$(dirname "$0")/../../.." || exit 3

OUT="${1:-docs/evidence/C55/window-pruning-explain.txt}"
ROUND="$(basename "$(dirname "$OUT")")"
{
  printf '%s\n' \
"window-pruning-explain.txt —— AC-11|04 的执行计划面：不带日期区间的查询在真 MySQL 仓库上的 EXPLAIN" \
"====================================================================" \
"round: ${ROUND}" \
"ARCHIVE_ROUND=${ROUND}" \
"date: $(date '+%Y-%m-%d %H:%M %z')" \
"branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）" \
"python: $(python -V 2>&1)" \
"command: bash docs/evidence/${ROUND}/run_window_explain.sh" \
"note: SQL 文本与绑定参数由 build_data_select 现拼，就是 REST 端点发给 MySQL 的那一条；" \
"      正文里每张已落地的分区表一行 pruned=，计划读到全部分区的表会写 pruned=NO 并让进程非零退出。" \
"" \
"===== 正文（未裁剪，含解释器的 loguru 日志）====="
  python -u scripts/ops/window_partition_explain.py 2>&1
  printf 'EXPLAIN_EXIT=%s\n' "$?"
} | tee "$OUT"
