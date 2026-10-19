"""The residue ladder at wave-38 close: every one of the rows the judge still refuses to accept.

Why this is a face and not a paragraph: "12 rows remain" is a count, and a count can be produced by
any two bugs that happen to add up. This probe reads the number twice, from independent objects:

* the shipped residue census (``docs/evidence/C128/.../probes/residue_axis_census.py``) run as a
   subprocess, whose per-row lines name each row's bucket, mechanism and the action it needs;
* the plane-1 judge itself, imported in-process, which publishes 350 identities and an
   ``offline_acceptance_count``; the residue is the difference.

Then it arms the two sides against each other, requires the census's own ``C_population_identity``
line to read PASS, and refuses to proceed if any census arm other than the one known defect
(``B_landed_negative``, a negative control that was consumed by the wave that landed the row it used
to falsify) is red -- a different arm going red would mean the census itself broke, not that the
ladder changed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
CARRIER = REPO / BUNDLE / "runs" / "w52_residue_ladder.txt"
CENSUS = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py"
JUDGE = REPO / "scripts/quality/iter2_model_evidence.py"
CLAIMS = REPO / "docs/evidence/C101/claims.merged.json"
INVENTORY = REPO / ("docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv")
LANDED_ROWS = ("seeking_alpha::CalendarEarnings", "cboe::IndexSnapshots")
KNOWN_RESIDUE_ROWS = ("biztoc::WorldNews", "yfinance::AvailableIndices")
TOLERATED_CENSUS_RED_ARM = "B_landed_negative"
ROW_RE = re.compile(
    r"^row (\S+) status=(\S+) bucket=(\S+) verdict=(\S+) seam=(\S+) .*"
    r"mechanism=(\S+).*action=(\S+)",
    re.MULTILINE,
)


def _census() -> tuple[int, list[str], list[dict[str, str]], str]:
    """Run the shipped census and return its exit, stdout, parsed rows and red-arms reading."""
    result = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute executables
        [sys.executable, str(CENSUS)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    out = result.stdout + result.stderr
    rows = [
        {
            "row": match.group(1),
            "status": match.group(2),
            "bucket": match.group(3),
            "verdict": match.group(4),
            "seam": match.group(5),
            "mechanism": match.group(6),
            "action": match.group(7),
        }
        for match in ROW_RE.finditer(out)
    ]
    return result.returncode, out.splitlines(), rows, out


def _judge_residue() -> dict[str, Any]:
    """Run the repo's own shipped judge in-process and read the counts from its report."""
    sys.path.insert(0, str(REPO))
    from scripts.quality import iter2_model_evidence as judge  # the shipped judge

    inventory = judge.load_inventory(INVENTORY)
    document = json.loads(CLAIMS.read_text(encoding="utf-8"))
    report = judge.build_report(
        inventory,
        document,
        claims_sha256=hashlib.sha256(CLAIMS.read_bytes()).hexdigest(),
        claims_path=str(CLAIMS.relative_to(REPO)),
    )
    return {
        "identities": report["summary"]["identity_count"],
        "accepted": report["summary"]["offline_acceptance_count"],
        "claims": report["summary"]["claim_count"],
        "status_counts": dict(report["summary"]["status_counts"]),
        "identity_sha": report["inventory"]["identity_set_sha256"][:16],
    }


def main() -> int:
    """Cross-check the census ladder against the judge's own residue and arm the disagreement."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    census_exit, census_lines, rows, census_text = _census()
    judge = _judge_residue()
    residue = int(judge["identities"]) - int(judge["accepted"])
    actions = Counter(row["action"] for row in rows)
    lines.append(
        f"judge identities={judge['identities']} claims={judge['claims']} "
        f"accepted={judge['accepted']} "
        f"residue={residue} status_counts={judge['status_counts']} "
        f"identity_set={str(judge['identity_sha'])[:16]}"
    )
    lines.append(
        f"census_exit={census_exit} rows_parsed={len(rows)} actions_by_row={dict(actions)}"
    )
    nested = re.search(r"2_actions (\{[^}]*\}) sums=(\d+)", census_text)
    nested_actions = ast.literal_eval(nested.group(1)) if nested else {}
    lines.append(
        f"census_nested_totals declared={dict(nested_actions)} "
        f"parsed_rows_sum={sum(actions.values())} "
        f"nested_sums={(nested.group(2) if nested else '?')}"
    )
    lines.extend(
        f"ladder {row['action']:22} {row['row']:38} status={row['status']:11} "
        f"verdict={row['verdict']:9} mechanism={row['mechanism']}"
        for row in rows
    )
    arm_lines = re.findall(r"^(\w+) .*PASS=(True|False)$", census_text, re.MULTILINE)
    red_arms = sorted(name for name, verdict in arm_lines if verdict == "False")
    green_arms = sorted(name for name, verdict in arm_lines if verdict == "True")
    print_missing = [row for row in rows if row["row"] in LANDED_ROWS]
    present_known = [row["row"] for row in rows if row["row"] in KNOWN_RESIDUE_ROWS]
    lines.append(
        f"two_sided landed_rows_still_listed={len(print_missing)} "
        f"known_residue_rows_present={sorted(present_known)} of={len(KNOWN_RESIDUE_ROWS)}"
    )
    lines.append(f"census_arms green={green_arms} red={red_arms}")
    arms["census_arm_c_passes"] = "C_population_identity" in green_arms
    arms["census_arm_a_passes"] = "A_landed_positive" in green_arms
    arms["rows_equal_independent_residue"] = len(rows) == residue and residue > 0
    arms["action_totals_match_rows"] = (
        bool(nested_actions)
        and sum(nested_actions.values()) == len(rows)
        and nested_actions == dict(actions)
    )
    arms["landed_rows_absent"] = print_missing == []
    arms["known_residue_present"] = sorted(present_known) == sorted(KNOWN_RESIDUE_ROWS)
    arms["census_failure_is_the_known_one"] = red_arms == [TOLERATED_CENSUS_RED_ARM]

    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append(
        f"residue_ladder arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    exit_code = 0 if not failed else 1
    lines.append("")
    lines.append(f"w52_residue_ladder_exit={exit_code}")
    body = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w52_residue_ladder.py\n"
        f"# argv: {sys.executable} "
        f"{REPO / BUNDLE / 'probes' / 'w52_residue_ladder.py'}\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
