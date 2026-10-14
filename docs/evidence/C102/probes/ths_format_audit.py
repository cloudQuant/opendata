"""Arm A/C of the C102 collateral-reformat audit.

HEAD's ``tests/test_ths_provider.py`` is formatted in isolation: if the committed copy is already
format-clean, a ``ruff format`` pass over the worktree had nothing to reflow except text this round
added. Prints both arms and exits non-zero when either arm disagrees.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
WORK = Path("/tmp/c102_audit")


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(
        ["git", "show", "HEAD:tests/test_ths_provider.py"],
        cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    original = WORK / "ths_head.py"
    copy = WORK / "ths_head_fmt.py"
    original.write_text(head, encoding="utf-8")
    shutil.copy(original, copy)

    format_proc = subprocess.run(["ruff", "format", str(copy)], capture_output=True, text=True)
    reflow = subprocess.run(["diff", "-u", str(original), str(copy)], capture_output=True, text=True)
    pending = subprocess.run(
        ["ruff", "format", "--diff", "tests/test_ths_provider.py"],
        cwd=ROOT, capture_output=True, text=True
    )

    print(f"armA_head_format_exit={format_proc.returncode}")
    print(f"armA_head_reflow_diff_exit={reflow.returncode} (0 = HEAD was already format-clean)")
    print(f"armA_head_reflow_diff_bytes={len(reflow.stdout)}")
    print(f"armC_worktree_pending_format_diff_exit={pending.returncode} (0 = worktree is clean)")
    print(f"armC_worktree_pending_diff_bytes={len(pending.stdout)}")
    if reflow.returncode != 0 or pending.returncode != 0 or len(reflow.stdout) != 0:
        print("verdict: AUDIT_DISAGREED")
        return 1
    print("verdict: PASS - the format pass had no collateral to reflow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
