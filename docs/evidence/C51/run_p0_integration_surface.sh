#!/usr/bin/env bash
# C51 instrument: run the P0 domain integration surface in two interpreters and record both,
# untrimmed, into one evidence body (docs/evidence/C51/clean-env-integration-run.txt).
#
# `-m 'integration and not e2e'` is the whole safety story: the warehouse e2e face is deselected
# by name, the app is reached through httpx ASGITransport (which does not run the lifespan), and
# the fixture engine is sqlite in memory. `--no-cov` keeps a second coverage reader out of the run.
set -u
cd "${ROOT:-/Users/yunjinqi/Documents/new_projects/opendata}" || exit 3
export PYTHONPATH=.
V="${VENV:-/tmp/c51-clean/venv}"
DEV_PY="${DEV_PY:-$HOME/opt/anaconda3/envs/py313/bin/python}"
OUT="${OUT:-/tmp/c51-clean/clean-env-integration-run.txt}"
SELECTOR='integration and not e2e'
: > "$OUT"

emit() { printf '%s\n' "$1" >> "$OUT"; }

emit "===== 0. what this file measures ====="
emit 'AC-16 条目 7: 「在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过」.'
emit "Two interpreters run the same selection, untrimmed:"
emit "  A. the development interpreter (anaconda py313, the whole machine's package set)"
emit "  B. a venv created for this round by docs/evidence/C51/build_clean_env.sh,"
emit "     whose dependency surface is exactly pyproject.toml's declared extras (.[web,dev])."
emit "Selector: -m 'integration and not e2e' -- the P0 domain integration surface, never the warehouse."
emit ""

emit "===== 0b. upstream packages in each interpreter (before any test runs) ====="
for name in repo clean; do
  if [ "$name" = repo ]; then P="$DEV_PY"; else P="$V/bin/python"; fi
  emit "--- $name: $P"
  "$P" -c '
import importlib.util
for n in ("akshare", "openbb", "openbb_platform"):
    print(f"  {n}: {"present" if importlib.util.find_spec(n) else "absent"}")
' >> "$OUT" 2>&1
done
emit ""

emit "===== A. development interpreter ====="
emit "\$ time python -m pytest tests -m 'integration and not e2e' --no-cov"
/usr/bin/time -p "$DEV_PY" -m pytest tests -m "$SELECTOR" --no-cov >> "$OUT" 2>&1
emit "REPO_RUN_EXIT=$?"
emit ""

emit "===== B. clean venv ====="
emit "\$ time /tmp/c51-clean/venv/bin/python -m pytest tests -m 'integration and not e2e' --no-cov"
/usr/bin/time -p "$V/bin/python" -m pytest tests -m "$SELECTOR" --no-cov >> "$OUT" 2>&1
emit "CLEAN_RUN_EXIT=$?"
emit ""

emit "===== C. the venv's own consistency check ====="
emit "\$ /tmp/c51-clean/venv/bin/pip check"
"$V/bin/pip" check --disable-pip-version-check >> "$OUT" 2>&1
emit "PIP_CHECK_EXIT=$?"
emit ""

emit "===== summary line (also greppable) ====="
# Read the finished body into a variable first: appending to $OUT while grep reads it
# can feed grep's own output back to it.
SUMMARY="$(
  grep -E "^(REPO_RUN_EXIT|CLEAN_RUN_EXIT|PIP_CHECK_EXIT)=" "$OUT"
  grep -E "=====" "$OUT" | grep -E "passed|failed|error" | tail -4
)"
emit "$SUMMARY"
emit "TOTAL_LINES=$(wc -l < "$OUT" | tr -d ' ')"
