#!/bin/bash
# Reproduction script for zero-dep-surface-after.txt (round C36).
#
# Runs the hardened zero-dependency scanner against the *v1* baseline it must reject,
# freezes v2, and proves (a) the three frozen upstream residuals are the same three and
# (b) the new file census matches an independent `find` count, and (c) a one-file shrink
# on the scan surface goes red instead of passing quietly.
#
# The baseline document is saved and restored byte-for-byte by this script; the v1
# document it must reject is archived next to this script, so it re-runs without git.
set -u
cd /Users/yunjinqi/Documents/new_projects/opendata || exit 99
export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.
BASELINE=docs/quality/zero-dep-baseline.json
SCANNER=scripts/codemod/verify_no_akshare.py
PRISTINE=/tmp/zero-dep-baseline-v2-pristine.json

echo "=== [0] interpreter / tooling ==="
python --version
echo "python_executable: $(command -v python)"
ruff --version
mypy --version

echo
echo "=== [1] restore the v1 baseline that HEAD carries ==="
cp docs/evidence/C36/zero-dep-baseline-v1.json "$BASELINE"
echo "restore_rc=$?"
python - <<'PY'
import json

d = json.load(open("docs/quality/zero-dep-baseline.json"))
print("v1 top-level fields:", sorted(d))
print("v1 has a file census?  ", "files" in d)
print("v1 pins a scanner ver? ", "scanner_version" in d)
print("v1 pins interpreters?  ", "python_minors" in d)
print("v1 scope               :", d["scope"])
print("v1 count               :", d["count"])
PY

echo
echo "=== [2] HEAD's scanner walking the same tree (the device before this round) ==="
python - <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "head_zero_dep", Path("docs/evidence/C36/zero_dep_scanner_HEAD.py")
)
mod = importlib.util.module_from_spec(spec)
sys.modules["head_zero_dep"] = mod
spec.loader.exec_module(mod)
# The archived copy sits two directories deeper than the real script, so its
# REPO_ROOT = parents[2] resolves to docs/. Everything else is HEAD's own code.
mod.REPO_ROOT = Path.cwd()
print("HEAD DEFAULT_TARGETS:", mod.DEFAULT_TARGETS)
total = 0
for target in mod.DEFAULT_TARGETS:
    base = mod.REPO_ROOT / target
    if base.is_dir():
        files = [p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts]
    else:
        files = []
    total += len(files)
    print(f"  {target}: is_dir={base.is_dir()} walked_files={len(files)}")
print(f"HEAD total files walked: {total}")
print("HEAD raises on a missing/empty target?  ", hasattr(mod, "ScanSurfaceError"))
PY

echo
echo "=== [3] self-test of the hardened scanner (guards must bite both ways) ==="
python "$SCANNER" --self-test
echo "SELFTEST_EXIT=$?"

echo
echo "=== [4] lint / type views of the changed files ==="
ruff check --no-cache "$SCANNER"
echo "RUFF_EXIT=$?"
ruff format --check "$SCANNER"
echo "RUFF_FORMAT_EXIT=$?"
mypy "$SCANNER" 2>&1 | tail -2
echo "MYPY_RC=${PIPESTATUS[0]}"

echo
echo "=== [5] check against the v1 baseline: must fail, and must name the field ==="
python "$SCANNER"
echo "V1_CHECK_EXIT=$?"

echo
echo "=== [6] freeze v2 ==="
python "$SCANNER" --force-update
echo "FREEZE_EXIT=$?"
cat "$BASELINE"
cp "$BASELINE" "$PRISTINE"

echo
echo "=== [7] check against v2 ==="
python "$SCANNER"
echo "V2_CHECK_EXIT=$?"

echo
echo "=== [8] the frozen residuals are the same three; only the device grew ==="
python - <<'PY'
import json

old = json.load(open("docs/evidence/C36/zero-dep-baseline-v1.json"))
new = json.load(open("docs/quality/zero-dep-baseline.json"))


def identity(doc):
    return sorted((f["file"], f["module"], f["kind"]) for f in doc["findings"])


print("v1 count:", old["count"], " v2 count:", new["count"])
print("v1 identities:", json.dumps(identity(old), ensure_ascii=False))
print("v2 identities:", json.dumps(identity(new), ensure_ascii=False))
print("same residual set:", identity(old) == identity(new))
print("dropped from scope:", sorted(set(old["scope"]) - set(new["scope"])))
for a, b in zip(old["findings"], new["findings"], strict=True):
    mark = "" if a["line"] == b["line"] else "  (line drift; identity is line-insensitive)"
    print(f"  {a['file']}: {a['line']} -> {b['line']}{mark}")
PY

echo
echo "=== [9] the census is not self-reported fiction: recount with find(1) ==="
python - <<'PY'
import json

doc = json.load(open("docs/quality/zero-dep-baseline.json"))
for target, count in sorted(doc["files"].items()):
    print(f"  {target}: {count}")
print("  total:", sum(doc["files"].values()))
PY
for pkg in opendata opendata_http opendata_fuyao opendata_client; do
  n=$(find "$pkg" -name '*.py' -not -path '*/__pycache__/*' | wc -l | tr -d ' ')
  echo "  find $pkg -> $n"
done
echo "  find opendata_providers -> $(test -d opendata_providers && find opendata_providers -name '*.py' | wc -l | tr -d ' ' || echo 'no such directory (correctly dropped from scope)')"

echo
echo "=== [10] census divergence must go red in both directions ==="
tamper() {  # tamper <value>  -> rewrite the archived opendata_http count
python - "$1" <<'PY'
import json
import sys
from pathlib import Path

path = Path("docs/quality/zero-dep-baseline.json")
doc = json.loads(path.read_text(encoding="utf-8"))
value = int(sys.argv[1])
before = doc["files"]["opendata_http"]
doc["files"]["opendata_http"] = value
path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"census tampered: opendata_http archived {before} -> {value} (reality is {before})")
PY
}

echo "--- [10a] archived count lower than the walk: the tree grew past the archive ---"
tamper 312
python "$SCANNER"
echo "STALE_CHECK_EXIT=$?"
cp "$PRISTINE" "$BASELINE"

echo "--- [10b] archived count higher than the walk: the surface shrank ---"
tamper 314
python "$SCANNER"
echo "SHRINK_CHECK_EXIT=$?"
cp "$PRISTINE" "$BASELINE"
echo "restored_pristine_rc=$?"
cmp "$BASELINE" "$PRISTINE" && echo "baseline restored byte-for-byte"

echo
echo "=== [11] final reading with the pristine v2 baseline ==="
python "$SCANNER"
echo "FINAL_CHECK_EXIT=$?"
git diff --stat "$BASELINE"
