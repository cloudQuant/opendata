#!/usr/bin/env bash
# C56: run the AC-11|02 cross-source adjust check against the akshare official series
# and archive one real machine run of it.
#
# The AC-11|02 probe reads docs/evidence/C56/qfq-official-akshare.txt and compares two faces
# with each other: the leg the dispatch table resolves *now*, and the leg the archived run
# recorded in its own header (`official: <leg> <module>:<function>`). Repointing the akshare
# entry at another module therefore reddens the item until someone runs this again -- an old
# archive cannot describe a table that no longer resolves that way.
#
# Read-only instrument: SELECTs on dwd_stock_daily / dwd_stock_adjust and the official
# fetches only. No write, no DDL. B4's sina-leg archive stays untouched.
set -u
export PATH="${HOME}/opt/anaconda3/envs/py313/bin:${PATH}"
export PYTHONPATH=.
cd "$(dirname "$0")/../../.." || exit 3

OUT="${1:-docs/evidence/C56/qfq-official-akshare.txt}"
ROUND="$(basename "$(dirname "$OUT")")"
mkdir -p "$(dirname "$OUT")"
{
  printf '%s\n' \
"qfq-official-akshare.txt —— AC-11|02 的官方对照面：服务端合成 qfq/hfq 与 akshare 官方序列的真机对照" \
"====================================================================" \
"round: ${ROUND}" \
"ARCHIVE_ROUND=${ROUND}" \
"date: $(date '+%Y-%m-%d %H:%M %z')" \
"branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）" \
"python: $(python -V 2>&1)" \
"command: bash docs/evidence/${ROUND}/run_qfq_official_akshare.sh" \
"note: 命令不写 --official，跑的就是仪器自己的默认腿；报告头 official: 行因此同时是" \
"      「默认值」与「实际执行腿」的证据，探针再拿它对当场解析到的 module:function。" \
"      ours 走 build_data_select + apply_adjust_to_rows，就是 REST 端点那条路。" \
"      判据只看 shape dev（按中位数重新锚定后的逐日偏差），恒差不算失败，容差 2e-3 不改；" \
"      每行另给 over_tolerance（超过容差的根数，不受 MAX_DIFFS=5 列表截断）与 ratio 平台分布，" \
"      用来区分「个别怪 bar」与「两条因子链把事件放在不同日期」。官方端点突发会 502/空帧，" \
"      腿最多重试 3 次并在 leg retries 列写明第几次才拿到，空帧一律算 ERROR 不算对照。" \
"" \
"===== 正文（未裁剪：仪器自写的报告全文 + 解释器的进度行）====="
  python -u scripts/ops/qfq_official_check.py \
    --symbols 600519,000001,000002,000009,600036 \
    --start 2026-01-05 \
    --out docs/evidence/C56/qfq-official-akshare-report.txt 2>&1
  printf 'QFQ_EXIT=%s\n' "$?"
} | tee "$OUT"
