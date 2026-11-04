#!/usr/bin/env bash
# C151 instrument: re-run the P0 domain integration surface in two clean venvs, untrimmed, and
# archive it as docs/evidence/C151/clean-env-integration-run.txt.
#
# Shape inherited from docs/evidence/C55/run_p0_integration_surface.sh (and C51's two-environment
# idea), with the macOS-only parts replaced by their Linux equivalents: /usr/bin/time -p exists
# here so the timing line is unchanged; ROOT/VENV paths are this machine's worktree paths.
# C51 archived "A. development interpreter" + "B. clean venv"; C64/C65 archived TWO clean
# interpreters instead, because the criterion is a clean-environment claim and a development
# interpreter proves nothing about absence. This script does the same: section A and section B are
# both venvs created for this round by docs/evidence/C151/build_clean_env.sh.
#
# ARCHIVE_ROUND below is read by the AC-16|07 probe, which refuses an archive whose declared round
# does not match the docs/evidence/<round>/ directory it sits in. The probe also reads CLEAN_RUN_EXIT
# from the body and diffs every module the `integration` marker reaches right now against the module
# names appearing after "===== B. clean venv ====="; that diff is `archive_blind`, the ratchet this
# round exists to close. C151's marked set is 10 modules and C65's archive names 9 of them, so C65
# reads as archive_blind=1 -- which a copied archive cannot repair.
#
# The tests run in a verification directory: a copy of an independent source snapshot with the
# `opendata` source package removed, so what the tests import is the installed wheel and not the
# working tree. The script proves that per run (which file `opendata` resolves to) rather than
# asserting it.
set -u
ROOT="${ROOT:-/tmp/od-cleanenv}"
BASE="${BASE:-/tmp/od-c151}"
SOURCE="${SOURCE:-$BASE/source}"
WHEELS="${WHEELS:-$BASE/wheels}"
VENVA="${VENVA:-$BASE/clean-a}"
VENVB="${VENVB:-$BASE/clean-b}"
OUT="${OUT:-$ROOT/docs/evidence/C151/clean-env-integration-run.txt}"
SELECTOR='integration and not e2e'
DEV_PY="${DEV_PY:-$BASE/clean-a/bin/python}"

# The test files the selector reaches. Produced in the repository working tree by
#   python -m pytest tests -m 'integration and not e2e' --collect-only -q
# (the archive's section C re-derives the marked-module set from the probe's own function, so a
# stale list here shows up as a marked module missing from section B rather than silently).
KEEP="tests/test_data_query_api.py
tests/test_data_query_http_warehouse.py
tests/test_dwd_merge.py
tests/test_financial_pit_api.py
tests/test_index_constituent_asof.py
tests/test_iteration01_integration_contracts.py
tests/test_ods_writer.py
tests/test_p0_providers.py
tests/test_pipeline_jobs.py
tests/test_pipeline_run_api.py
tests/test_pipeline_runner.py
tests/test_raw_response_cache.py"

: > "$OUT"
emit() { printf '%s\n' "$1" >> "$OUT"; }

emit "===== 0. what this file measures ====="
emit "ARCHIVE_ROUND=C151"
emit "ARCHIVE_PATH=docs/evidence/C151/clean-env-integration-run.txt"
emit "AC-16 条目 7: 「在未安装 akshare/openbb 的干净环境中 P0 域集成测试通过」."
emit "Date: $(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
emit "HEAD: $(git -C "$ROOT" rev-parse HEAD)"
emit "Branch: $(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
emit "Machine: $(uname -srm)"
emit "selector: $SELECTOR"
emit "Command: <venv>/bin/python -m pytest tests -m 'integration and not e2e' --no-cov -o addopts= -v"
emit "C151 rebuild: the marked set is 10 modules now (tests/test_financial_pit_api.py joined it) and"
emit "the newest archived clean run (docs/evidence/C65) names 9, which the probe reads as archive_blind=1."
emit "This round therefore rebuilt the clean environments and re-ran the surface."
emit "Two independent clean interpreters run the same selection, untrimmed:"
emit "  A. $VENVA - venv created for this round by docs/evidence/C151/build_clean_env.sh"
emit "  B. $VENVB - a second venv, created the same way from the same source snapshot"
emit "Each venv's dependency surface is exactly pyproject.toml's declared .[web,dev] extras, plus the"
emit "wheel built from this round's source snapshot, reinstalled into it with --force-reinstall --no-deps."
emit "Environment: conftest forces TESTING/APP_ENV/REDIS_URL=''/SENTRY_DSN='' and refuses the app's"
emit "default engines unless OPENDATA_ALLOW_LIVE_E2E carries the exact opt-in token, which is unset"
emit "here; no .env is present in the verification directory (the source snapshot is git-tracked files"
emit "plus non-ignored untracked ones), and no live MySQL/Redis is contacted."
emit ""

emit "===== 1. wheel built from the independent source snapshot ====="
emit "\$ $DEV_PY -m pip wheel --no-deps --wheel-dir $WHEELS $SOURCE"
rm -rf "$WHEELS"
mkdir -p "$WHEELS"
"$DEV_PY" -m pip wheel --no-deps --disable-pip-version-check \
  --wheel-dir "$WHEELS" "$SOURCE" >> "$OUT" 2>&1
emit "WHEEL_BUILD_EXIT=$?"
WHEEL="$(ls "$WHEELS"/opendata-*.whl | head -1)"
emit "Wheel=$WHEEL"
emit "Wheel_SHA256=$(sha256sum "$WHEEL" | cut -d' ' -f1)"
emit ""

emit "===== 2. that exact wheel is what each clean venv runs ====="
for V in "$VENVA" "$VENVB"; do
  emit "\$ $V/bin/python -m pip install --force-reinstall --no-deps $WHEEL"
  "$V/bin/python" -m pip install --disable-pip-version-check --force-reinstall --no-deps \
    "$WHEEL" >> "$OUT" 2>&1
  emit "WHEEL_INSTALL_EXIT_${V##*/}=$?"
done
emit ""

run_venv() { # $1 = section label (A/B), $2 = venv, $3 = verification directory
  LABEL="$1"
  V="$2"
  VERIF="$3"

  rm -rf "$VERIF"
  mkdir -p "$VERIF"
  rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='*.egg-info/' \
    --exclude='build/' --exclude='.pytest_cache/' --exclude='htmlcov/' --exclude='node_modules/' \
    "$SOURCE/" "$VERIF/"
  # The distribution under test is the installed wheel, so its source tree is not in the
  # verification directory. Everything else the tests read by path (alembic_data/versions,
  # examples/, scripts/ops/, the docs/evidence archives some guards re-read) stays put.
  rm -rf "$VERIF/opendata"
  find "$VERIF/tests" -name 'test_*.py' -print0 | while IFS= read -r -d '' p; do
    rel="${p#"$VERIF"/}"
    printf '%s\n' "$KEEP" | grep -q -x -- "$rel" || rm -f "$p"
  done

  emit "===== $LABEL. clean venv ====="
  emit "interpreter: $V/bin/python"
  emit "verification directory: $VERIF (test files kept: $(find "$VERIF/tests" -name 'test_*.py' | wc -l))"
  emit "\$ $V/bin/python -m pytest tests -m '$SELECTOR' --no-cov -o addopts= -v"
  emit "opendata resolves to (installed distribution, not the working tree):"
  (cd "$VERIF" && "$V/bin/python" -c 'import opendata; print("  " + opendata.__file__)') >> "$OUT" 2>&1
  emit "IMPORT_EXIT_${V##*/}=$?"
  emit "opendata source package in the verification directory: $(cd "$VERIF" && { ls -d opendata >/dev/null 2>&1 && echo 'FOUND (not a clean verification directory)' || echo 'absent'; })"
  for n in akshare openbb openbb_platform; do
    if (cd "$VERIF" && "$V/bin/python" -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) is None else 1)' "$n") 2>/dev/null; then
      emit "$n: absent"
    else
      emit "$n: FOUND (this environment is not clean)"
    fi
  done
  emit "pip list lines naming akshare/openbb: $(cd "$VERIF" && "$V/bin/pip" list --disable-pip-version-check 2>/dev/null | grep -i -c -E 'akshare|openbb' || true)"
  emit "\$ $V/bin/pip check"
  "$V/bin/pip" check --disable-pip-version-check >> "$OUT" 2>&1
  emit "PIP_CHECK_EXIT_${V##*/}=$?"
  emit ""
  cd "$VERIF" || exit 3
  /usr/bin/time -p "$V/bin/python" -m pytest tests -m "$SELECTOR" --no-cov -o addopts= -v >> "$OUT" 2>&1
  emit "CLEAN_${LABEL}_RUN_EXIT=$?"
  emit ""
}

run_venv A "$VENVA" "$BASE/verification-a"
run_venv B "$VENVB" "$BASE/verification-b"

cd "$ROOT" || exit 3
emit "===== C. module census: the marked set against what section B names ====="
emit "\$ python -c 'import acceptance_item_probe as p; p.integration_units_now()'  (the probe's own rule, read off tests/)"
"$DEV_PY" - <<'PY' >> "$OUT" 2>&1
import sys

sys.path.insert(0, "scripts/quality")
import acceptance_item_probe as probe

units = probe.integration_units_now()
modules = sorted({u.split("::")[0] for u in units})
print(f"marked_units={len(units)}")
print("marked_modules=" + ", ".join(modules))
PY
MARKED="$(grep '^marked_modules=' "$OUT" | tail -1 | cut -d= -f2- | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | sort -u)"
NAMED="$(awk 'f; /^===== B\. clean venv =====/{f=1}' "$OUT" | grep '^tests/' | grep '::' | cut -d: -f1 | sort -u)"
emit "marked_modules_not_named_in_section_B=[$(comm -23 <(printf '%s\n' "$MARKED") <(printf '%s\n' "$NAMED") | tr '\n' ' ')]"
emit "section_B_named_modules_count=$(printf '%s\n' "$NAMED" | grep -c . )"
emit ""

emit "===== D. the two runs' summary lines and exits ====="
# Read the finished body first: appending to $OUT while grep reads it can feed grep's own output
# back into the file.
SUMMARY="$(
  grep -E '^(WHEEL_BUILD_EXIT|WHEEL_INSTALL_EXIT|PIP_CHECK_EXIT|IMPORT_EXIT|CLEAN_A_RUN_EXIT|CLEAN_B_RUN_EXIT)[A-Za-z0-9._-]*=' "$OUT"
  grep -E '^=+ [0-9].*(passed|failed|error|skipped).*=$' "$OUT"
)"
emit "$SUMMARY"
emit "TOTAL_LINES=$(wc -l < "$OUT" | tr -d ' ')"
emit ""

A_EXIT="$(grep '^CLEAN_A_RUN_EXIT=' "$OUT" | tail -1 | cut -d= -f2)"
B_EXIT="$(grep '^CLEAN_B_RUN_EXIT=' "$OUT" | tail -1 | cut -d= -f2)"
if [ "$A_EXIT" = "0" ] && [ "$B_EXIT" = "0" ]; then
  CLEAN_RUN_EXIT=0
else
  CLEAN_RUN_EXIT=1
fi
emit "CLEAN_RUN_EXIT=$CLEAN_RUN_EXIT"
