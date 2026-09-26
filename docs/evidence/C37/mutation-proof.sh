#!/usr/bin/env bash
# C37: prove the AC-6 judge change has teeth in both directions.
#
# Three mutations, each reverted, each expected to redden exactly the guard that
# owns it:
#   M1 把 object 列退回「按 repr 文本判」  -> 容忍度两条用例必须红（否则本轮没修到东西）
#   M2 把容忍度放宽到 rtol=1e-2           -> 「真值变化仍须红」必须红（否则判据被放宽了）
#   M3 取消「至少一侧是 float 实例」的门槛  -> '000001' vs '1' 必须红（否则代码列被数值化）
# 每次变异后立刻按校验和还原；还原不成立即整轮判失败。
# 判据写在脚本里：每条 run_case 都打印 pytest 的完整原始输出，不裁剪。
set -euo pipefail

cd "$(dirname "$0")/../../.."
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

TARGET=scripts/codemod/compare_with_upstream.py
BACKUP=$(mktemp)
cp "$TARGET" "$BACKUP"
SUM_BEFORE=$(shasum -a 256 "$TARGET" | cut -d' ' -f1)
restore() { cp "$BACKUP" "$TARGET"; }
cleanup() { restore; rm -f "$BACKUP"; }
trap cleanup EXIT

run_case() { # $1=step id, $2=pytest -k selector（空串=整个文件）, $3=expected exit (0/1)
  local id="$1" selector="$2" expect="$3" code=0
  local want="绿"
  [[ "$expect" == "1" ]] && want="红"
  if [[ -z "$selector" ]]; then
    echo "--- $id: 整个 tests/test_port_fidelity.py（期望 $want）---"
    python -m pytest tests/test_port_fidelity.py --no-cov -q -p no:cacheprovider 2>&1 || code=$?
  else
    echo "--- $id: pytest -k '$selector'（期望 $want）---"
    python -m pytest tests/test_port_fidelity.py --no-cov -q -p no:cacheprovider -k "$selector" 2>&1 || code=$?
  fi
  echo "--- $id: pytest 退出码 = $code ---"
  if [[ "$expect" == "1" && "$code" -eq 0 ]]; then
    echo "$id: EXPECTED RED, GOT GREEN  <-- mutation did not bite"
    return 1
  fi
  if [[ "$expect" == "0" && "$code" -ne 0 ]]; then
    echo "$id: EXPECTED GREEN, GOT RED (exit $code)"
    return 1
  fi
  echo "$id: as expected (exit=$code, expect=$expect)"
}

echo "=== [base] 未变异：整个保真对照套件应全绿 ==="
run_case "base" "" 0

echo
echo "=== [base-guards] 未变异：本轮新增的六条判据守卫应全绿 ==="
run_case "base-guards" "object_column or looks_numeric or value_change or tolerance_is_unchanged or drift_is_reported or non_numeric_text" 0

echo
echo "=== [M1] object 列退回按 repr 文本判 ==="
python - <<'PY'
from pathlib import Path
p = Path("scripts/codemod/compare_with_upstream.py")
s = p.read_text(encoding="utf-8")
old = "        pair = _float_pair(raw_left, raw_right)\n        if pair is None:"
assert s.count(old) == 1
p.write_text(s.replace(old, "        pair = None\n        if pair is None:"), encoding="utf-8")
PY
run_case "M1" "repr_only_diff_in_object_column_is_tolerated or drift_is_reported_not_silent" 1
restore

echo
echo "=== [M2] 容忍度放宽到 rtol=1e-2 ==="
python - <<'PY'
from pathlib import Path
p = Path("scripts/codemod/compare_with_upstream.py")
s = p.read_text(encoding="utf-8")
old = "        if bool(np.isclose(left_value, right_value, rtol=RTOL)):"
assert s.count(old) == 1
p.write_text(s.replace(old, "        if bool(np.isclose(left_value, right_value, rtol=1e-2)):"), encoding="utf-8")
PY
run_case "M2" "value_change_in_object_column_still_fails" 1
restore

echo
echo "=== [M3] 取消「至少一侧是 float 实例」的门槛 ==="
python - <<'PY'
from pathlib import Path
p = Path("scripts/codemod/compare_with_upstream.py")
s = p.read_text(encoding="utf-8")
old = """    floats = (float, np.float64, np.floating)
    if not (isinstance(left, floats) or isinstance(right, floats)):
        return None
"""
assert s.count(old) == 1
p.write_text(s.replace(old, ""), encoding="utf-8")
PY
run_case "M3" "looks_numeric_is_not_numberified" 1
restore

echo
echo "=== [restored] 还原后整套件应重新全绿 ==="
run_case "restored" "" 0

SUM_AFTER=$(shasum -a 256 "$TARGET" | cut -d' ' -f1)
echo "还原校验: before=$SUM_BEFORE"
echo "          after =$SUM_AFTER"
if [[ "$SUM_AFTER" != "$SUM_BEFORE" ]]; then
  echo "FAIL: 变异未完全还原"
  exit 1
fi
echo "OK: 三次变异全部按预期咬合，源文件逐字节还原。"
