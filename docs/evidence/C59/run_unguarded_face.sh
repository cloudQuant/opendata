#!/usr/bin/env bash
# C59 unguarded face: what the warehouse was doing from the *unit* plane before
# this round, measured from HEAD (no working-tree state involved).
#
# Read-only toward the repository and the warehouse: it imports the census from
# tests/test_e2e_opt_in_guard.py, feeds it `git show HEAD:<file>` text, greps the
# C58 gate log for the four legs' PASSED lines, and counts the e2e plane. It
# never connects to a database and writes nothing outside docs/evidence/C59/.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=1 DATA_MYSQL_HOST=127.0.0.1 DATA_MYSQL_PORT=1

cd "$(git rev-parse --show-toplevel)"
OUT=docs/evidence/C59/unguarded-before.txt

{
  echo "unguarded-before.txt —— 修前普查面：单元面里未标 e2e 的真库用例，与它们在门禁日志里的读数"
  echo "===================================================================="
  echo "round: C59"
  echo "ARCHIVE_ROUND=C59"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（HEAD 侧读数，工作树改动不参与）"
  echo "python: $(python -V 2>&1)"
  echo
  echo "面 1 —— 按 AST 普查 tests/*.py：未处于 e2e 作用域、却用 settings.*database_url* 建引擎的定义"
  echo "      census = tests/test_e2e_opt_in_guard.py::unmarked_engine_defs（与门禁里同一条判据同一个函数）"
  echo
  python - <<'PY'
import subprocess
from pathlib import Path

from tests.test_e2e_opt_in_guard import unmarked_engine_defs

files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD", "tests/"],
                       capture_output=True, text=True, check=True).stdout.split()
hits = {}
for name in files:
    if not name.endswith(".py"):
        continue
    src = subprocess.run(["git", "show", f"HEAD:{name}"], capture_output=True, text=True,
                         check=True).stdout
    found = unmarked_engine_defs(src)
    if found:
        hits[name] = found
print(f"  HEAD 侧未设防的定义：{sum(len(v) for v in hits.values())} 个，分布在 {len(hits)} 个文件")
for name, found in sorted(hits.items()):
    print(f"    {name}: {found}")
print("  工作树侧同一普查（tests/test_freshness.py）："
      f"{unmarked_engine_defs(Path('tests/test_freshness.py').read_text(encoding='utf-8'))!r}"
      "（空 = 本轮两个定义已点齐）")
PY
  echo
  echo "面 2 —— 这四条在 C58 门禁日志里的读数（tests -n 8 -m \"not e2e\" 段落，行号取自档内原文）"
  echo
  grep -n "TestCheckFreshness\|TestPartitionPlanCollector" docs/evidence/C58/gate-run1.txt | head -8
  echo
  echo "面 3 —— e2e 面的规模：修前 83 条被 -m \"not e2e\" 摘掉；本轮把真库那格挪进 e2e 后的读数"
  echo
  echo "  HEAD:tests/test_freshness.py 里 TestPartitionPlanCollector 的标记行："
  git show HEAD:tests/test_freshness.py | grep -n "pytest.mark.e2e\|class TestPartitionPlanCollector" | head -4
  echo "  工作树（修后）："
  grep -n "pytest.mark.e2e\|class TestPartitionPlanCollector" tests/test_freshness.py | head -4
  echo
  echo "  注：e2e 总数复算见本档 README §3（collect-only 面），这里不复述数字以免两处打架。"
} | tee "$OUT"

exit 0
