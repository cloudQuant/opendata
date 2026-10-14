"""Arm B of the C102 collateral-reformat audit.

Splits ``git diff -U0`` for the repaired test file into hunks and requires every hunk to carry at
least one token from this round's edit set. A hunk made only of reformatted pre-existing lines would
be collateral damage from the ``ruff format`` pass, and it must be named rather than assumed away.
"""

from __future__ import annotations

import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
TARGET = "tests/test_ths_provider.py"

MY_TOKENS = (
    "_offline_context",
    "mock_transport",
    "OFFLINE_ATTEMPTS",
    "MOCK_HOST",
    "RequestBudgetProfile",
    "RequestAuthorizationError",
    "GrantDecision",
    "RequestBudget",
    "RequestGrant",
    "RequestOperation",
    "FuyaoRateLimiter",
    "backoff_",
    "rate_limiter=",
    "base_url=",
    "timedelta",
    "datetime",
    "FetchContext",
    "Fetcher",
    "import ",
    "#:",
    '"""',
)


def hunks(diff_text: str) -> list[dict[str, list[str]]]:
    """Split a unified diff into per-hunk added and removed line lists."""
    blocks: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    for line in diff_text.splitlines():
        if line.startswith("@@ "):
            if current is not None:
                blocks.append(current)
            current = {"added": [], "removed": []}
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current["added"].append(line[1:])
        elif current is not None and line.startswith("-") and not line.startswith("---"):
            current["removed"].append(line[1:])
    if current is not None:
        blocks.append(current)
    return blocks


def main() -> int:
    """Audit what the working-tree diff to the ths module really changes, hunk by hunk."""
    diff_text = subprocess.run(  # nosec B603 B607  # noqa: S603
        ["git", "diff", "-U0", "--", TARGET],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,  # nosec B404
    ).stdout
    blocks = hunks(diff_text)
    orphans = [b for b in blocks if not any(tok in "\n".join(b["added"]) for tok in MY_TOKENS)]
    numstat = subprocess.run(  # nosec B603 B607  # noqa: S603
        ["git", "diff", "--numstat", "--", TARGET],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,  # nosec B404
    ).stdout.strip()
    print(
        f"face: hunks_total={len(blocks)} hunks_without_my_token={len(orphans)} numstat={numstat}"
    )
    for block in orphans:
        print("ORPHAN_HUNK")
        for line in block["added"][:8]:
            print(f"   +{line}")
    if orphans:
        print("verdict: COLLATERAL_FOUND")
        return 1
    print("verdict: PASS - every changed hunk carries a token from this round's edits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
