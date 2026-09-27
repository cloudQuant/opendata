#!/usr/bin/env bash
# C51 instrument: build the clean environment AC-16|07 talks about.
#
# The point is not "a venv" but "a dependency surface exactly as wide as pyproject.toml declares":
# if the P0 integration tests pass here, nothing in them can be leaning on a package the
# repository never claimed. Body as run: docs/evidence/C51/clean-env-build.txt.
set -u
export PATH="${PY_BIN_PATH:-$HOME/opt/anaconda3/envs/py313/bin}:$PATH"
ROOT="${ROOT:-/Users/yunjinqi/Documents/new_projects/opendata}"
V="${VENV:-/tmp/c51-clean/venv}"

echo "=== step 1: create venv (no system site-packages) ==="
python -m venv "$V" || exit 3
echo "=== step 2: interpreter of the venv ==="
"$V/bin/python" -VV || exit 3
echo "=== step 3: install the declared dependency set only ==="
cd "$ROOT" || exit 3
"$V/bin/pip" install --quiet --disable-pip-version-check -e ".[web,dev]" 2>&1 | tail -25
INSTALL_EXIT=$?
echo "PIP_INSTALL_EXIT=$INSTALL_EXIT"
echo "=== step 4: is akshare/openbb present in this env? ==="
"$V/bin/python" - <<'PY'
import importlib.util
for name in ("akshare", "openbb"):
    print(f"{name}: {'IMPORTABLE (env is not clean)' if importlib.util.find_spec(name) is not None else 'absent (clean)'}")
PY
echo "=== step 5: pip list, upstream names filtered out of the whole tree ==="
"$V/bin/pip" list --disable-pip-version-check 2>/dev/null | grep -i -E "akshare|openbb" || echo "grep: 0 lines mentioning akshare/openbb in pip list"
"$V/bin/pip" list --disable-pip-version-check 2>/dev/null | wc -l
echo "=== step 6: dependency tree consistency ==="
"$V/bin/pip" check --disable-pip-version-check 2>&1 | tail -5
echo "BUILD_DONE rc=$INSTALL_EXIT"
