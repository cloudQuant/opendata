"""Face: did a2-check's ``ok bandit`` reading come from bandit's findings or from a parse failure?

``a2_check._bandit`` parsed ``stdout + stderr`` as one string. bandit writes its ``nosec``/``Test in
comment`` warnings to stderr, and this repo's A2 set emits them, so the combined string is not valid
JSON and the handler's ``except JSONDecodeError: return True`` branch answered "pass". Two arms
separate what happened from what it proved:

* **live arm** -- the real tool on a real A2 file. ``results`` is empty here, so the verdict was
  already correct; the point is that it was NOT read. The arm prints which branch produced it.
* **shaped arm** -- the same bytes with one finding inside ``results`` and the exit code pinned to
  0 (the state a ``--exitzero`` / severity-threshold config would produce). Old body: pass. New
  body: fail. This is the arm that makes the fix bite.

The shaped arm's precondition is measured, not asserted: ``config_scoping()`` reads ``bandit.yaml``
and prints whether either of those two knobs is present. So the live arm's verdict was already
right, and the face's claim is narrow -- the old leg reached a correct answer without reading it.

Re-run: ``python3.11 docs/evidence/C77/a2-bandit-escape-face.py``. Read-only over the repo; the
shaped arm feeds strings, it does not invoke bandit. Face: ``a2-bandit-escape-face.txt``.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scripts.quality import a2_check as guard  # noqa: E402

LIVE_TARGET = "scripts/quality/acceptance_item_probe.py"
WARNING_HINT = "Test in comment"


def legacy_bandit_verdict(code: int, stdout: str, stderr: str) -> tuple[bool, str]:
    """The pre-C77 body, byte for byte: parse stdout+stderr, and pass when that throws."""
    output = (stdout or "") + (stderr or "")
    if code != 0:
        return False, output.strip()
    try:
        payload = json.loads(output or "{}")
    except json.JSONDecodeError:
        return True, "ESCAPE: stdout+stderr is not JSON, so no finding was read"
    results = payload.get("results", [])
    if not isinstance(results, list):
        return True, "ESCAPE: results is not a list"
    return (False, f"{len(results)} finding(s)") if results else (True, "results is empty")


def live_arm() -> dict[str, object]:
    """Run the real bandit on a real A2 file and record which branch each body takes."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [
            sys.executable,
            "-m",
            "bandit",
            "-c",
            "bandit.yaml",
            "-f",
            "json",
            "-q",
            LIVE_TARGET,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    parsed_results = None
    parses_alone = False
    try:
        parsed_results = len(json.loads(stdout or "{}").get("results", []))
        parses_alone = True
    except json.JSONDecodeError:
        pass
    concat_parses = True
    try:
        json.loads(stdout + stderr)
    except json.JSONDecodeError:
        concat_parses = False
    old, old_branch = legacy_bandit_verdict(proc.returncode, stdout, stderr)
    new, new_detail = guard._bandit([LIVE_TARGET])
    return {
        "target": LIVE_TARGET,
        "rc": proc.returncode,
        "stdout_bytes": len(stdout.encode()),
        "stderr_bytes": len(stderr.encode()),
        "stderr_carries_warnings": WARNING_HINT in stderr,
        "stdout_parses_alone": parses_alone,
        "parsed_results_count": parsed_results,
        "concat_parses": concat_parses,
        "legacy_ok": old,
        "legacy_branch": old_branch or "read the parsed results",
        "current_ok": new,
        "current_detail": (new_detail or "read an empty results list")[:200],
    }


def shaped_arm() -> dict[str, object]:
    """Force the state the escape hides: a finding in stdout, exit code 0, warning on stderr."""
    stdout = json.dumps(
        {
            "errors": [],
            "results": [
                {
                    "filename": LIVE_TARGET,
                    "line_number": 12,
                    "test_id": "B307",
                    "issue_text": "use of eval",
                }
            ],
        }
    )
    stderr = f"[manager]\tWARNING\t{WARNING_HINT}: argv is not a test name or id, ignoring\n"
    old_ok, old_branch = legacy_bandit_verdict(0, stdout, stderr)
    real_run_streams = guard._run_streams

    def stub(args: list[str]) -> tuple[int, str, str]:
        del args
        return 0, stdout, stderr

    try:
        guard._run_streams = stub
        new_ok, new_detail = guard._bandit([LIVE_TARGET])
    finally:
        guard._run_streams = real_run_streams
    return {
        "legacy_ok": old_ok,
        "legacy_branch": old_branch,
        "current_ok": new_ok,
        "current_detail": new_detail[:160],
        "fix_bites": old_ok is True and new_ok is False,
    }


def config_scoping() -> dict[str, object]:
    """Whether today's bandit config can actually produce the shaped arm's rc=0-with-findings."""
    text = (REPO / "bandit.yaml").read_text(encoding="utf-8")
    knobs = {name: (name in text) for name in ("exit_zero", "level_severity")}
    return {
        "bandit_yaml_bytes": len(text.encode()),
        "knobs_present": knobs,
        "skips_present": "skips:" in text,
        "shaped_state_reachable_today": any(knobs.values()),
    }


def main() -> None:
    """Print both arms and the two-sided control, and say which of them refutes what."""
    live = live_arm()
    print("=== live arm: real bandit over a real A2 file ===")
    for key, value in live.items():
        print(f"  {key}: {value}")

    shaped = shaped_arm()
    print("\n=== shaped arm: one finding in stdout with the exit code pinned to 0 ===")
    for key, value in shaped.items():
        print(f"  {key}: {value}")

    scope = config_scoping()
    print("\n=== scope of the shaped arm under this repo's config ===")
    for key, value in scope.items():
        print(f"  {key}: {value}")
    print(
        "  reading: with no exit_zero/level_severity knob, bandit exits 1 on findings, so the old"
        " leg's rc!=0 guard already caught them and the escape only mislabelled a correct verdict"
        if not scope["shaped_state_reachable_today"]
        else "  reading: this config CAN report findings at rc=0, so the escape was an active"
        " fail-open for every A2 file scanned under it"
    )

    control_empty = legacy_bandit_verdict(0, json.dumps({"results": []}), "")
    print("\n=== controls ===")
    print(f"  legacy on a genuinely empty results list (no stderr): ok={control_empty[0]}")
    if control_empty[0] is not True:
        print("REFUTE: the replica disagrees with the shipped old behaviour on clean bytes")
        raise SystemExit(1)
    print("  new body on the same bytes is covered by tests/test_a2_exclusion_scope.py")
    print(f"  SHAPED-ARM-FIX-BITES: {shaped['fix_bites']}")
    if not shaped["fix_bites"]:
        print("REFUTE: the tightened leg does not catch the state the escape used to swallow")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
