#!/usr/bin/env bash
# C35: prove the exclusion guards can actually fail, and restore everything.
#
# Every mutation reverts one of the six fixes this round made (or empties an
# exclusion list, which is the opposite error) and then runs the guard file.
# Each patched file is restored with `git checkout --` and verified against the
# sha256 recorded before the mutation, so a passing run here also proves the
# working tree is back where it started.
#
# Usage (py313 env, from the repo root):
#   bash docs/evidence/C35/mutations.sh

set -u
export PYTHONPATH=.

GUARDS=tests/test_a2_exclusion_scope.py
FAILED=0

sha() { shasum -a 256 "$1" | cut -c1-16; }

patch() { # file, old, new
  python - "$1" "$2" "$3" <<'PY'
import sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(path, encoding="utf-8").read()
if data.count(old) != 1:
    raise SystemExit(f"MUTATION-SETUP-ERROR: {data.count(old)} matches of {old!r} in {path}")
open(path, "w", encoding="utf-8").write(data.replace(old, new))
PY
}

run_guards() { # label
  echo
  echo "### $1"
  python -m pytest "$GUARDS" -p no:randomly --no-cov -q 2>&1 |
    grep -E "^(FAILED|ERROR)|passed|failed" | sed 's|^|  |'
}

mutate() { # label, file, old, new
  local before
  before=$(sha "$2")
  if ! patch "$2" "$3" "$4"; then
    echo "### $1"
    echo "  SKIPPED: the mutation could not be applied"
    FAILED=$((FAILED + 1))
    return
  fi
  run_guards "$1"
  git checkout -- "$2"
  local after
  after=$(sha "$2")
  if [ "$after" != "$before" ]; then
    echo "  RESTORE-FAILED: $2 sha256 $before -> $after"
    FAILED=$((FAILED + 1))
  else
    echo "  restored: $2 (sha256[:16] $after unchanged)"
  fi
}

echo "### 0. 基线：未变异时守卫全绿"
run_guards "baseline"

mutate "M1 a2_check 退回「任意路径段命中」（C35 缺陷本体）" scripts/quality/a2_check.py \
  '    if parts and parts[0] in EXCLUDED_ROOT_DIRS:' \
  '    if any(part in EXCLUDED_ROOT_DIRS for part in parts):'

mutate "M2 反向：排除条件永不生效（守卫必须发现根树不再被排除）" scripts/quality/a2_check.py \
  '    if parts and parts[0] in EXCLUDED_ROOT_DIRS:' \
  '    if False:  # C35 mutation: exclusion list disabled'

mutate "M3 [tool.ruff].exclude 退回修复前的写法" pyproject.toml \
  '    "opendata_http/",' \
  '    "akshare",'$'\n''    "opendata_http",'

mutate "M4 [tool.mypy].exclude 退回修复前的单行写法" pyproject.toml \
  '    "^opendata_http/",' \
  '    "akshare", "opendata_http",'

mutate "M5 bandit.yaml 重新加入裸名 akshare" bandit.yaml \
  '  - "opendata_http"' \
  '  - "akshare"'$'\n''  - "opendata_http"'

mutate "M6 Makefile 的 PY_PORTED 退回已不存在的 akshare" Makefile \
  'PY_PORTED := opendata_http' \
  'PY_PORTED := akshare'

echo
echo "### M7 判定面真的有牙：往新可见的树里写进缺陷，a2-check 必须 FAIL"
TARGET=opendata/data/providers/akshare/models/stock_daily.py
BEFORE=$(sha "$TARGET")
patch "$TARGET" 'import pandas as pd' 'import pandas as pd
import os  # C35 probe: unsorted import (ruff I001)'
printf '\n_C35_PROBE: int = "deliberate type error"\n' >> "$TARGET"
echo "--- a2_check --files <被改文件> 的输出 ---"
python scripts/quality/a2_check.py --files "$TARGET" 2>&1 | tail -14 | sed 's|^|  |'
echo "A2_EXIT=${PIPESTATUS[0]}"
git checkout -- "$TARGET"
AFTER=$(sha "$TARGET")
[ "$AFTER" = "$BEFORE" ] && echo "  restored: $TARGET (sha256[:16] $AFTER unchanged)" ||
  { echo "  RESTORE-FAILED: $AFTER != $BEFORE"; FAILED=$((FAILED + 1)); }

echo
echo "### 收尾：工作树必须与 HEAD 一致（除本轮证据目录）"
git status --porcelain | sed 's|^|  |'
echo "MUTATION_FAILURES=$FAILED"
