"""Handoff-ledger contradiction face: a tracked test freezes a stale claim.

``tests/test_cboe_index_historical_public.py::test_integration_handoff_declares_only_the_limited_candidate_domain``
asserts that ``docs/evidence/C100/congress-cboe/cboe-integration.json``'s
``remaining_fixed_identities`` is exactly ``{"FuturesCurve", "IndexSnapshots"}``. The assertion is
tracked; the file it reads is not:
``.gitignore:249`` covers ``/docs/evidence/C100/**`` (``git check-ignore -v`` proves it and
``git ls-files --error-unmatch`` fails), so the test's premise lives only on a disk that happens
to carry
that receipt, and the same test reads green in wave 38's committed plane with zero failures.

Meanwhile the ledger's own content says these two identities have ``NO_RUNTIME_SOURCE_BINDING``.
Plane 1 says otherwise for both: ``cboe::IndexSnapshots`` was accepted in wave 37 and
``cboe::FuturesCurve`` is accepted by the chain this wave just re-signed. That is not a reason to
edit the receipt -- raw evidence under C100 belongs to the reviewer, not to me -- it is a reported
defect with two readings: the tracked
test cannot pass on a fresh clone, and it certifies a stale classification.

Armed, not narrated: the contradiction predicate must be able to read agreement, so the same
comparison is
run against a provider whose rows the ledger does not name at all.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GIT = "/usr/bin/git"
BUNDLE = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = BUNDLE / "runs"
HANDOFF = REPO / "docs/evidence/C100/congress-cboe/cboe-integration.json"
JUDGE = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
TEST_FILE = "tests/test_cboe_index_historical_public.py"
#: The assertion site the test pins, quoted from the tracked test file rather than retyped.
ASSERT_SITE = 'set(handoff["remaining_fixed_identities"]) == {"FuturesCurve", "IndexSnapshots"}'


def git(*args: str) -> tuple[int, str]:
    """Run one literal git argv and return its exit code and stdout."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout


def accepted_rows(document: dict[str, Any]) -> dict[str, str]:
    """Map ``provider::Model`` to its plane-1 status, straight from the judge's own model list."""
    return {
        f"{row['provider']}::{row['upstream_model']}": str(row["status"])
        for row in document.get("models") or []
    }


def main() -> int:
    """Print the tracked/untracked split, both claims, and the contradiction between them."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    ignore_rc, ignore_out = git("check-ignore", "-v", str(HANDOFF.relative_to(REPO)))
    track_rc, track_out = git("ls-files", "--error-unmatch", str(HANDOFF.relative_to(REPO)))
    lines.append(
        f"tracked check_ignore_exit={ignore_rc} rule={ignore_out.strip().split()[0:3]} "
        f"ls_files_exit={track_rc} ls_files_stdout={track_out.strip()!r}"
    )
    arms["A_file_is_gitignored"] = ignore_rc == 0 and "C100" in ignore_out
    arms["A_file_is_untracked"] = track_rc != 0 and track_out.strip() == ""

    ledger: dict[str, Any] = json.loads(HANDOFF.read_text(encoding="utf-8"))
    fixed = ledger.get("remaining_fixed_identities") or {}
    reason_is_no_binding = all("NO_RUNTIME_SOURCE_BINDING" in str(v) for v in fixed.values())
    lines.append(
        f"ledger names={sorted(fixed)} entries={len(fixed)} "
        f"all_no_runtime_source_binding={reason_is_no_binding}"
    )
    arms["B_ledger_names_two_rows"] = len(fixed) == 2

    judge = json.loads(JUDGE.read_text(encoding="utf-8"))
    statuses = accepted_rows(judge)
    pairs = [(f"cboe::{name}", statuses.get(f"cboe::{name}", "ABSENT")) for name in sorted(fixed)]
    contradicted = [p for p in pairs if p[1] == "OFFLINE_ACCEPTED"]
    lines.append(
        f"cross_check plane1={pairs} contradicted={len(contradicted)} "
        f"judge_accepted={judge['summary']['offline_acceptance_count']}"
    )
    arms["C_ledger_contradicts_plane1"] = len(contradicted) == len(pairs) and len(pairs) >= 2

    # The predicate has to be able to disagree, or "contradicted=2" is just the shape of the loop.
    control_name = "cboe::AvailableIndices"
    control_status = statuses.get(control_name, "ABSENT")
    control_contradicted = control_name in fixed and control_status == "OFFLINE_ACCEPTED"
    lines.append(
        f"control row={control_name} in_ledger={control_name in fixed} plane1={control_status} "
        f"reads_contradicted={control_contradicted} (must be False)"
    )
    arms["D_predicate_can_read_agreement"] = control_name not in fixed and not control_contradicted

    test_text = (REPO / TEST_FILE).read_text(encoding="utf-8")
    site_present = ASSERT_SITE in test_text
    lines.append(f"assert_site verbatim_in_tracked_test={site_present} file={TEST_FILE}")
    arms["E_assertion_is_tracked"] = site_present

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"handoff_ledger_face arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    (RUNS / "w64_handoff_ledger_contradiction.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    print("w64_handoff_ledger_face_exit=0" if ok else "w64_handoff_ledger_face_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
