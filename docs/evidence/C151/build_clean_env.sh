#!/usr/bin/env bash
# C151 instrument: build the clean environment AC-16|07 talks about.
#
# Linux adaptation of docs/evidence/C51/build_clean_env.sh. The *measurement* is the same one C51
# made: a venv created without system site-packages whose dependency surface is exactly what
# pyproject.toml declares (the .[web,dev] extras), installed from an independent source copy, and
# then asked whether akshare / openbb / openbb_platform exist in it. Only the paths and the
# interpreter are this machine's (C51 was written on a macOS maintainer box: anaconda py313 under
# /Users/...); nothing about what is being proven changed.
#
# The install is deliberately from "$SOURCE[web,dev]" and not from the repository working tree:
# the extras are the only thing that may widen the environment, so an accidental `-e .` in a tree
# that also holds top-level packages would not tell us which package satisfied an import.
set -u
BASE_PY="${BASE_PY:-/home/yun/.local/bin/python3.11}"
SOURCE="${SOURCE:-/tmp/od-c151/source}"
V="${VENV:-/tmp/od-c151/clean-a}"
LOG="${LOG:-/tmp/od-c151/clean-env-build-$(basename "$V").txt}"

{
  echo "=== step 0: this machine, this round ==="
  date -u +"%Y-%m-%dT%H:%M:%S+00:00"
  uname -srm
  echo "base interpreter: $BASE_PY"
  echo "source copy: $SOURCE"
  echo "venv: $V"
  [ -x "$V/bin/python" ] && echo "venv already exists on disk (kept, not cleared): $V"

  echo "=== step 1: create venv (no system site-packages) ==="
  "$BASE_PY" -m venv "$V" || exit 3

  echo "=== step 2: interpreter of the venv ==="
  "$V/bin/python" -VV || exit 3

  echo "=== step 3: install the declared dependency set only ==="
  "$V/bin/pip" install --disable-pip-version-check "$SOURCE[web,dev]"
  INSTALL_EXIT=$?
  echo "PIP_INSTALL_EXIT=$INSTALL_EXIT"

  echo "=== step 4: is akshare/openbb present in this env? ==="
  "$V/bin/python" - <<'PY'
import importlib.util
for name in ("akshare", "openbb", "openbb_platform"):
    spec = importlib.util.find_spec(name)
    print(f"{name}: {'present (env is not clean)' if spec is not None else 'absent (clean)'}")
PY

  echo "=== step 5: pip list, upstream names searched in the whole installed set ==="
  "$V/bin/pip" list --disable-pip-version-check 2>/dev/null | grep -i -E "akshare|openbb" \
    || echo "grep: 0 lines mentioning akshare/openbb in pip list"
  "$V/bin/pip" list --disable-pip-version-check 2>/dev/null | wc -l

  echo "=== step 6: dependency tree consistency ==="
  "$V/bin/pip" check --disable-pip-version-check
  echo "PIP_CHECK_EXIT=$?"

  echo "BUILD_DONE rc=$INSTALL_EXIT"
} > "$LOG" 2>&1
echo "build log: $LOG"
tail -25 "$LOG"
