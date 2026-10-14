"""C103 counterfact: prove the ``scenario_present`` guard actually judges the 9 re-based OECD rows.

Re-basing a census is only honest if the assertion it satisfies can fail. This probe builds a shadow
root in ``/tmp`` (symlinked ``opendata``/``docs`` plus the batch3/batch4 fixtures, so every other
assertion in the guard resolves against the real repo) and runs
``tests/test_provider_domains.py::test_declared_domain_contracts_match_exact_registered_provider_models``
twice:

  arm A  fixture with ONE of the 9 flipped rows put back to ``false``, and the copied module's
         ``BATCH5_SNAPSHOT_SHA256`` re-pinned to that tampered payload -- so the payload digest
         assertion passes and the ONLY thing left to fail is the scenario assertion;
  arm B  untampered fixture with the live digest -- the same shadow harness must go green, which is
         what makes arm A's red attributable to the flip rather than to the shadowing.

Usage: python docs/evidence/C103/batch5-scenario-census/probes/scenario_counterfact.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SNAPSHOT_REL = "tests/fixtures/registry_batch5_snapshot.json"
TEST_REL = "tests/test_provider_domains.py"
GUARD = "test_declared_domain_contracts_match_exact_registered_provider_models"
LITERAL = re.compile(r'(BATCH5_SNAPSHOT_SHA256 = ")([0-9a-f]{64})(")')
TAMPER_TARGET = ("oecd", "GdpReal")


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()
SHADOW = Path("/tmp/c103_scenario_counterfact")


def build_shadow() -> None:
    if SHADOW.exists():
        shutil.rmtree(SHADOW)
    (SHADOW / "tests" / "fixtures").mkdir(parents=True)
    for rel in ("opendata", "docs"):
        os.symlink(REPO / rel, SHADOW / rel)
    for name in ("registry_batch3_snapshot.json", "registry_batch4_snapshot.json"):
        os.symlink(REPO / "tests" / "fixtures" / name, SHADOW / "tests" / "fixtures" / name)


def run_guard(module_path: Path) -> tuple[int, str, dict[str, object]]:
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            f"{module_path}::{GUARD}",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    report = proc.stdout + proc.stderr
    summary_line = next(
        (
            line.strip("= ")
            for line in reversed(report.splitlines())
            if re.search(r"\b\d+ (passed|failed)\b", line)
        ),
        None,
    )
    counts: dict[str, object] = {"summary_line": summary_line}
    for token in ("failed", "passed", "error"):
        match = re.search(rf"(\d+) {token}", report)
        counts[token] = int(match.group(1)) if match else 0
    collected = re.search(r"collected (\d+) item", report)
    # The shadow rootdir has no pytest.ini, so -q drops the "collected N items" header. The
    # denominator is taken from the summary line instead, and both arms run through this same
    # parser, so the A/B delta is attributable to the one tampered field.
    counts["collected_header"] = int(collected.group(1)) if collected else None
    counts["accounted"] = counts["failed"] + counts["passed"] + counts["error"]
    counts["scenarios_assert_line_in_report"] = bool(
        re.search(r"assert \(binding\.scenario is not None\) is row\[\"scenario_present\"\]", report)
    )
    counts["oecd_gdp_real_named_in_report"] = bool(re.search(r"GdpReal", report))
    return proc.returncode, report, counts


def write_arm(snapshot_bytes: bytes, digest: str) -> Path:
    module = (REPO / TEST_REL).read_text()
    rewritten, substitutions = LITERAL.subn(rf"\g<1>{digest}\g<3>", module)
    if substitutions != 1:
        raise SystemExit(f"expected 1 BATCH5_SNAPSHOT_SHA256 literal, rewrote {substitutions}")
    module_path = SHADOW / "tests" / "test_provider_domains.py"
    module_path.write_text(rewritten)
    (SHADOW / SNAPSHOT_REL).write_bytes(snapshot_bytes)
    return module_path


def main() -> int:
    live_bytes = (REPO / SNAPSHOT_REL).read_bytes()
    live_digest = hashlib.sha256(live_bytes).hexdigest()
    live_literal = LITERAL.search((REPO / TEST_REL).read_text())
    print(f"face: live_fixture_digest={live_digest}")
    print(f"face: test_literal_matches_live={live_literal is not None and live_literal.group(2) == live_digest}")
    if live_literal is None or live_literal.group(2) != live_digest:
        print("verdict: REFUSED - the committed test literal is not the live payload digest")
        return 1

    snapshot = json.loads(live_bytes)
    rows = snapshot["provider_model_identities"]
    matches = [row for row in rows if (row["source"], row["model"]) == TAMPER_TARGET]
    print(f"face: tamper_target={TAMPER_TARGET} rows_matched={len(matches)}")
    if len(matches) != 1 or matches[0]["scenario_present"] is not True:
        print("verdict: REFUSED - the re-based target row is not a unique true row")
        return 1

    build_shadow()
    tracked_before = {
        rel: hashlib.sha256((REPO / rel).read_bytes()).hexdigest()
        for rel in (SNAPSHOT_REL, TEST_REL, "tests/test_pipeline_jobs.py")
    }
    try:
        tampered = json.loads(live_bytes)
        for row in tampered["provider_model_identities"]:
            if (row["source"], row["model"]) == TAMPER_TARGET:
                row["scenario_present"] = False
        tampered_bytes = (json.dumps(tampered, ensure_ascii=False, indent=2) + "\n").encode()
        tampered_digest = hashlib.sha256(tampered_bytes).hexdigest()
        differing_rows = [
            (before, after)
            for before, after in zip(rows, tampered["provider_model_identities"])
            if before != after
        ]
        changed_fields = sorted(
            {
                key
                for before, after in differing_rows
                for key in before
                if before.get(key) != after.get(key)
            }
        )
        print(f"face: tampered_digest={tampered_digest}")
        print(
            "face: tamper_scope="
            f"row_dicts_differing={len(differing_rows)} fields_changed={changed_fields}"
        )
        if len(differing_rows) != 1 or changed_fields != ["scenario_present"]:
            print("verdict: REFUSED - the tamper moved more than scenario_present on one row")
            return 1

        print("arm A: one re-based OECD row re-set to false, payload digest re-pinned")
        path_a = write_arm(tampered_bytes, tampered_digest)
        exit_a, report_a, counts_a = run_guard(path_a)
        print(f"face: armA_exit={exit_a} counts={counts_a}")

        print("arm B: untampered fixture, same shadow harness")
        path_b = write_arm(live_bytes, live_digest)
        exit_b, report_b, counts_b = run_guard(path_b)
        print(f"face: armB_exit={exit_b} counts={counts_b}")

        ok = (
            exit_a != 0
            and counts_a["summary_line"] is not None
            and counts_a["accounted"] == 1
            and counts_a["failed"] == 1
            and counts_a["scenarios_assert_line_in_report"]
            and exit_b == 0
            and counts_b["summary_line"] is not None
            and counts_b["accounted"] == 1
            and counts_b["passed"] == 1
            and not counts_b["scenarios_assert_line_in_report"]
        )
        failing_line = next(
            (
                line.strip()
                for line in report_a.splitlines()
                if "binding.scenario" in line or "GdpReal" in line
            ),
            None,
        )
        print(f"face: armA_failing_line={failing_line!r}")
        print(f"face: armA_names_the_gdp_real_row={counts_a['oecd_gdp_real_named_in_report']}")
        print(f"verdict: {'PASS' if ok else 'FAIL'} - "
              f"arm A fails the guard at (binding.scenario is not None) is row['scenario_present'] "
              f"with exit {exit_a}; arm B passes the same shadow harness with exit {exit_b}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(SHADOW, ignore_errors=True)
        tracked_after = {
            rel: hashlib.sha256((REPO / rel).read_bytes()).hexdigest()
            for rel in tracked_before
        }
        print(
            f"face: shadow_removed={not SHADOW.exists()} "
            f"tracked_files_unchanged={tracked_after == tracked_before}"
        )
        for rel, digest in tracked_before.items():
            if tracked_after[rel] != digest:
                print(f"face: MOVED {rel} before={digest[:16]} after={tracked_after[rel][:16]}")


if __name__ == "__main__":
    raise SystemExit(main())
