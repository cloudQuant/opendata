#!/usr/bin/env bash
# C55 instrument: re-run the P0 domain integration surface in a clean venv and archive both
# interpreters, untrimmed, as docs/evidence/C55/clean-env-integration-run.txt.
#
# Same two-interpreter shape as docs/evidence/C51/run_p0_integration_surface.sh, for one reason:
# C55 added tests/test_data_query_http_warehouse.py to the ``integration`` set, and the AC-16|07
# probe counts integration-marked modules that the newest archived clean run never touched. That
# ratchet is only honest if the archive is rebuilt in the same round the set grows, so this script
# exists to be run against a venv built by docs/evidence/C51/build_clean_env.sh (a build instrument,
# not a measurement) with C55's own output path. Unlike C51's copy, the interpreter actually used is
# interpolated into the emitted command lines rather than hardcoded.
#
# ARCHIVE_ROUND below is read by the AC-16|07 probe, which refuses an archive whose declared round
# does not match the docs/evidence/<round>/ directory it sits in. Copying this file to a new round
# directory means editing that line too -- and then the module census in the body still has to
# cover the marker set as it stands, which is the part a copy cannot fake.
set -u
cd "${ROOT:-/Users/yunjinqi/Documents/new_projects/opendata}" || exit 3
export PYTHONPATH=.
V="${VENV:-/tmp/c55-clean/venv}"
DEV_PY="${DEV_PY:-$HOME/opt/anaconda3/envs/py313/bin/python}"
OUT="${OUT:-/tmp/c55-clean/clean-env-integration-run.txt}"
SELECTOR='integration and not e2e'
: > "$OUT"

emit() { printf '%s\n' "$1" >> "$OUT"; }

emit "===== 0. what this file measures ====="
emit "ARCHIVE_ROUND=C55"
emit "ARCHIVE_PATH=docs/evidence/C55/clean-env-integration-run.txt"
emit 'AC-16 条目 7: 「在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过」.'
emit "C55 rebuild: the marked set grew by one module this round (tests/test_data_query_http_warehouse.py),"
emit "so the clean-environment archive has to cover the set as it stands now, not as of C51."
emit "Two interpreters run the same selection, untrimmed:"
emit "  A. the development interpreter (anaconda py313, the whole machine's package set)"
emit "  B. a venv created for this round by docs/evidence/C51/build_clean_env.sh (VENV=$V),"
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
emit "\$ time $DEV_PY -m pytest tests -m 'integration and not e2e' --no-cov"
/usr/bin/time -p "$DEV_PY" -m pytest tests -m "$SELECTOR" --no-cov >> "$OUT" 2>&1
emit "REPO_RUN_EXIT=$?"
emit ""

emit "===== B. clean venv ====="
emit "\$ time $V/bin/python -m pytest tests -m 'integration and not e2e' --no-cov"
/usr/bin/time -p "$V/bin/python" -m pytest tests -m "$SELECTOR" --no-cov >> "$OUT" 2>&1
emit "CLEAN_RUN_EXIT=$?"
emit ""

emit "===== C. the venv's own consistency check ====="
emit "\$ $V/bin/pip check"
"$V/bin/pip" check --disable-pip-version-check >> "$OUT" 2>&1
emit "PIP_CHECK_EXIT=$?"
emit ""

emit "===== D. does the archived body name the module C55 added? ====="
# Same reason as the summary below: grep must not read $OUT while it is being appended to.
COVERS="$(grep -c "tests/test_data_query_http_warehouse.py" "$OUT" 2>/dev/null || true)"
emit "warehouse_module_lines=$COVERS"
emit ""

emit "===== summary line (also greppable) ====="
# Read the finished body into a variable first: appending to $OUT while grep reads it
# can feed grep's own output back to it.
SUMMARY="$(
  grep -E "^(REPO_RUN_EXIT|CLEAN_RUN_EXIT|PIP_CHECK_EXIT|warehouse_module_lines)=" "$OUT"
  grep -E "=====" "$OUT" | grep -E "passed|failed|error" | tail -4
)"
emit "$SUMMARY"
emit "TOTAL_LINES=$(wc -l < "$OUT" | tr -d ' ')"
