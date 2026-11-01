#!/usr/bin/env python3
"""C150 counterfactual: measure what the fixture-literal allowlist does and does not silence.

The repo's rule (``.gitleaks.toml`` header) is that an exemption must be justified by
measurement, not assertion. This script builds a throwaway git history whose lines are the
real fixture declarations from this tree plus planted variants, then reads gitleaks under
three configurations and reports which rule ids fire on each arm:

* ``shipped`` - the committed ``.gitleaks.toml`` (C150 entry present),
* ``without_c150`` - the same file with only the C150 regex line removed,
* ``default_rules`` - ``useDefault`` with no allowlist at all.

Fabricated credentials are assembled from fragments at runtime so this file cannot itself
become a later round's finding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / ".gitleaks.toml"
C150_MARKER = "fixture-observation-quota-v1|fixture-quota-v1"

# The eight literals are written as adjacent fragments so this file does not itself carry a
# credential-shaped line: the values assembled below are byte-identical to the declarations
# the arms test, but no line of this source matches generic-api-key. Same discipline C149
# records for the Cloudflare beacon literal ("the gate's own fix must not become the next
# round's false positive").
LITERALS = {
    "integration_run": "fixture-csi500-v3" "-integration" "-run",
    "csi500_quota": "fixture-csi500-v3" "-quota",
    "csi500_run": "fixture-csi500-v3" "-run",
    "ths_quota": "fixture-ths" "-quota-v1",
    "observation_quota": "fixture-observation" "-quota-v1",
    "plain_quota": "fixture-quota" "-v1",
    "budget_boundary": "quota-v3" "-boundary",
    "stock_daily_fixture": "stock-daily-v2" "-api-fixture",
}

REAL_LINES = [
    f'RUN_KEY = "{LITERALS["integration_run"]}"',
    f'QUOTA_KEY = "{LITERALS["csi500_quota"]}"',
    f'budget_quota_key="{LITERALS["csi500_quota"]}"',
    f'run_key="{LITERALS["csi500_run"]}"',
    f'PRINCIPAL = "{LITERALS["ths_quota"]}"',
    f'PRINCIPAL = "{LITERALS["observation_quota"]}"',
    f'QUOTA_KEY = "{LITERALS["plain_quota"]}"',
    f'"budget_quota_key": "{LITERALS["budget_boundary"]}",',
    f'"source_verification": "{LITERALS["stock_daily_fixture"]}",',
]

ARMS = {
    "real_lines.py": REAL_LINES,
    "one_char_longer.py": [
        f'QUOTA_KEY = "{LITERALS["csi500_quota"]}X"',
        f'"budget_quota_key": "{LITERALS["budget_boundary"]}X",',
        f'"source_verification": "{LITERALS["stock_daily_fixture"]}X",',
    ],
    "token_in_same_slot.py": [],
    "generic_secret_in_same_slot.py": [],
}


def fabricated_token() -> str:
    """Return a fake GitHub PAT assembled from fragments (never a literal here)."""
    return "ghp_" + "".join(
        ["7Gf2", "qLz9", "XbT4", "sKdR", "8mYp", "1nVc", "W6hJ", "kQ3r", "E5tA"]
    )


def fabricated_generic_secret() -> str:
    """Return a fake mixed-case generic secret assembled from fragments."""
    return "".join(["kQ", "9fZ", "x7Lm", "2pRs", "8TdV", "4bWe"])


def build_history(root: Path) -> None:
    token = fabricated_token()
    generic = fabricated_generic_secret()
    ARMS["token_in_same_slot.py"] = [
        f'budget_quota_key="{token}"',
        f'PRINCIPAL = "{token}"',
    ]
    ARMS["generic_secret_in_same_slot.py"] = [
        f'run_key="{generic}"',
        f'"source_verification": "{generic}",',
    ]
    for name, lines in ARMS.items():
        (root / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "c150",
        "GIT_AUTHOR_EMAIL": "c150@example.invalid",
        "GIT_COMMITTER_NAME": "c150",
        "GIT_COMMITTER_EMAIL": "c150@example.invalid",
    }
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "c150 arms"]):
        subprocess.run(["git", *args], cwd=root, check=True, env=env, capture_output=True)


def variant_config(dest: Path, mode: str) -> Path:
    """Return a config path for one measurement arm."""
    if mode == "shipped":
        path = dest / "shipped.toml"
        path.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        return path
    if mode == "without_c150":
        lines = CONFIG.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if C150_MARKER not in line]
        if len(kept) != len(lines) - 1:
            raise AssertionError("the C150 regex line was not found exactly once")
        path = dest / "without_c150.toml"
        path.write_text("".join(kept), encoding="utf-8")
        return path
    if mode == "default_rules":
        path = dest / "default_rules.toml"
        path.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        return path
    raise ValueError(mode)


def scan(source: Path, config: Path) -> dict[str, set[str]]:
    """Return {file name: set(rule ids)} gitleaks reports for that arm."""
    report = config.with_suffix(".json")
    subprocess.run(
        [
            "gitleaks", "detect",
            "--source", str(source),
            "--config", str(config),
            "--redact",
            "--no-banner",
            "--report-format", "json",
            "--report-path", str(report),
            "--exit-code", "0",
        ],
        check=True,
        capture_output=True,
    )
    findings = json.loads(report.read_bytes() or b"[]")
    by_file: dict[str, set[str]] = {}
    for item in findings:
        by_file.setdefault(Path(item["File"]).name, set()).add(item["RuleID"])
    return by_file


def main() -> int:
    if subprocess.run(["which", "gitleaks"], capture_output=True).returncode:
        print("gitleaks is not on PATH; this script measures, it does not skip.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="c150-counterfactual-") as tmp:
        root = Path(tmp) / "history"
        root.mkdir()
        build_history(root)
        for mode in ("shipped", "without_c150", "default_rules"):
            config = variant_config(Path(tmp), mode)
            hits = scan(root, config)
            print(f"== arm: {mode}")
            for name in sorted(ARMS):
                print(f"   {name:<34} {sorted(hits.get(name, set())) or '[]'}")
            print(f"   findings: {sum(len(v) for v in hits.values())} file/rule pairs\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
