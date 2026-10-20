"""Wave-41b face: the immutable-evidence fence was stale by a whole classification generation.

`tests/test_immutable_evidence.py` asserted `snapshot_paths == 172`, `excluded_paths == 172`,
`historical_ruff_count == 3974` and `active_entrypoints == 18`, while the classification artifact
the validator pins is literally named ``immutable-wave9-exact5055/classification.json`` and its
SHA-256 equals the module's ``CLASSIFICATION_SHA256``. The artifact is the source of truth (the code
refuses to validate if the digest moves), so the test was comparing a live population to literals
from the superseded exact172 generation.

This face recomputes every population from the pinned artifact, reads the four literals back out of
the test source, and proves the equality can fail by forging one of them in memory. It then runs the
test file and records its exit, so the carrier holds the before/after pair rather than a claim about
it.
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w109_immutable_fence_rebase.txt"
TEST = REPO / "tests/test_immutable_evidence.py"
EXPECTED = {"snapshot_paths": 5055, "excluded_paths": 5055, "ruff": 149323, "entrypoints": 343}


def _literals() -> dict[str, int]:
    text = TEST.read_text(encoding="utf-8")
    patterns = {
        "snapshot_paths": r"len\(validation\.snapshot_paths\) == (\d+)",
        "excluded_paths": r"len\(validation\.excluded_paths\) == (\d+)",
        "ruff": r"historical_ruff_count == (\d+)",
        "entrypoints": r"len\(validation\.active_entrypoints\) == (\d+)",
        "partition_excluded": r"len\(excluded\) == (\d+)",
    }
    out: dict[str, int] = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if match is None:
            raise SystemExit(f"test literal not found: {pattern}")
        out[name] = int(match.group(1))
    return out


def main() -> int:
    """Measure the artifact, compare with the test's literals, and take the test's exit reading."""
    from scripts.quality import immutable_evidence as evidence

    RUNS.mkdir(parents=True, exist_ok=True)
    root = REPO.resolve()
    validation = evidence.validate_manifest(root)
    artifact = REPO / evidence.CLASSIFICATION_PATH
    lines: list[str] = []
    arms: dict[str, bool] = {}
    lines.append(f"artifact={evidence.CLASSIFICATION_PATH}")
    lines.append(
        "artifact_sha_matches_pin="
        f"{hashlib.sha256(artifact.read_bytes()).hexdigest() == evidence.CLASSIFICATION_SHA256}"
    )
    lines.append(
        f"validation_errors={len(validation.errors)} blocked={len(validation.blocked_paths)}"
    )
    live = {
        "snapshot_paths": len(validation.snapshot_paths),
        "excluded_paths": len(validation.excluded_paths),
        "ruff": int(validation.historical_ruff_count),
        "entrypoints": len(validation.active_entrypoints),
    }
    literals = _literals()
    lines.append(f"live={live} literals={literals}")
    tools, excluded = evidence.partition_static_files(
        [*validation.active_entrypoints, *validation.snapshot_paths], validation
    )
    lines.append(
        f"partition tools={len(tools)} excluded={len(excluded)} "
        f"tools_are_entrypoints={set(tools) == set(validation.active_entrypoints)}"
    )
    arms["A_artifact_is_the_pinned_one"] = (
        hashlib.sha256(artifact.read_bytes()).hexdigest() == evidence.CLASSIFICATION_SHA256
    )
    arms["B_validation_has_no_errors"] = not validation.errors and not validation.blocked_paths
    arms["C_live_populations_are_the_expected_generation"] = live == EXPECTED
    arms["D_test_literals_equal_live"] = all(
        literals[key] == live[key]
        for key in ("snapshot_paths", "excluded_paths", "ruff", "entrypoints")
    ) and literals["partition_excluded"] == len(excluded)
    forged = dict(literals)
    forged["snapshot_paths"] += 1
    arms["E_literal_equality_can_bite"] = not (
        forged["snapshot_paths"] == live["snapshot_paths"]
        and all(forged[key] == live[key] for key in ("excluded_paths", "ruff", "entrypoints"))
    )
    arms["F_partition_keeps_entrypoints_out_of_exclusions"] = set(tools) == set(
        validation.active_entrypoints
    ) and not (set(validation.active_entrypoints) & set(excluded))

    proc = subprocess.run(  # nosec B603  # noqa: S603
        [
            PY,
            "-m",
            "pytest",
            "tests/test_immutable_evidence.py",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(REPO),
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
        },
    )
    summary = next(
        (
            line
            for line in reversed(proc.stdout.splitlines())
            if re.search(r"\d+ (passed|failed)", line)
        ),
        "no-summary",
    )
    lines.append(f"pytest exit={proc.returncode} summary={summary}")
    arms["G_the_fence_is_green_on_the_pinned_artifact"] = (
        proc.returncode == 0 and "failed" not in summary
    )

    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"immutable_fence_rebase arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w109_immutable_fence_rebase_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w109_immutable_fence_rebase_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
