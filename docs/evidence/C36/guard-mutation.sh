#!/bin/bash
# C36 mutation proof for the import-closure guard (tests/test_ported_import_closure.py).
#
# A guard that has never been seen red is a guard nobody proved exists. This script
# temporarily removes the dependency whose absence was the CI failure (`curl_cffi`) and
# shows the guard goes red, then removes a dependency that is *transitively* reachable
# (`idna`) and shows the guard stays green -- so the reading is "reachable or not",
# not "is the literal string in pyproject.toml".
#
# pyproject.toml is saved and restored byte-for-byte by this script. It never touches git.
set -u
cd /Users/yunjinqi/Documents/new_projects/opendata || exit 99
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.
PYPROJECT=pyproject.toml
SAVED=/tmp/pyproject-C36-mutation-pristine.toml
TESTS=tests/test_ported_import_closure.py

cp "$PYPROJECT" "$SAVED"
echo "saved_pristine_rc=$?"
cmp "$PYPROJECT" "$SAVED" && echo "pristine copy verified byte-for-byte"

echo
echo "=== [0] who declares what, before any mutation ==="
grep -n "curl_cffi\|urllib3" "$PYPROJECT"
python - <<'PY'
import importlib.metadata

for name in ("curl-cffi", "urllib3"):
    try:
        required_by = [
            d.metadata["Name"]
            for d in importlib.metadata.distributions()
            if any(
                (r.split(";")[0].strip().split("[")[0].split("=")[0].split(">")[0]
                 .split("<")[0].split("!")[0].split("~")[0].strip().lower().replace("_", "-"))
                == name
                for r in (d.requires or [])
            )
        ]
    except Exception as exc:  # pragma: no cover - diagnostic only
        required_by = [f"<error {exc}>"]
    print(f"  {name}: installed, required-by other distributions -> {sorted(required_by) or 'nothing'}")
PY

run_guard() {  # run_guard <label>  (real guard exit code)
    echo "--- $1 ---"
    # --no-cov: pytest.ini's addopts enforces the whole-suite coverage floor, which makes
    # every single-file run exit 1 for a reason that has nothing to do with this guard.
    pytest "$TESTS" -q --no-header --no-cov
    echo "GUARD_EXIT=$?"
}

echo
echo "=== [1] baseline: guard green on the committed declaration set ==="
run_guard "unmutated pyproject.toml"

echo
echo "=== [2] mutation A: delete the curl_cffi declaration (the CI defect re-opened) ==="
python - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
kept = [line for line in lines if '"curl_cffi' not in line]
assert len(kept) == len(lines) - 1, f"expected to drop exactly one line, dropped {len(lines) - len(kept)}"
path.write_text("".join(kept), encoding="utf-8")
print("removed line:", [line.strip() for line in lines if '"curl_cffi' in line])
PY
run_guard "curl_cffi declaration removed"
python -c "import tomllib;d=tomllib.loads(open('pyproject.toml').read());print('curl-cffi still declared in [project].dependencies?', any('curl' in x for x in d['project']['dependencies']))"

echo
echo "=== [3] restore, then mutation B: delete urllib3 (reachable through requests) ==="
cp "$SAVED" "$PYPROJECT"
cmp "$PYPROJECT" "$SAVED" && echo "restored byte-for-byte"
python - <<'PY'
from pathlib import Path

path = Path("pyproject.toml")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
kept = [line for line in lines if '"urllib3' not in line]
assert len(kept) == len(lines) - 1, f"expected to drop exactly one line, dropped {len(lines) - len(kept)}"
path.write_text("".join(kept), encoding="utf-8")
print("removed line:", [line.strip() for line in lines if '"urllib3' in line])
PY
run_guard "urllib3 declaration removed (control: requests requires it transitively)"

echo
echo "=== [4] final restore ==="
cp "$SAVED" "$PYPROJECT"
cmp "$PYPROJECT" "$SAVED" && echo "pyproject.toml restored byte-for-byte"
run_guard "unmutated pyproject.toml again"
git diff --stat "$PYPROJECT"
