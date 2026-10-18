"""Pre-commit credential scan over exactly the staged payload, three configs, planted-key armed.

Why an instrument and not a ``gitleaks`` invocation: the gate's member-5 scan runs against the
index in a form I cannot re-read afterwards, so the payload that a commit actually carries has to
be materialised and scanned inside the wave that produces it. This probe reconstructs the staged
blob for every path in ``git diff --cached`` (so it scans what would be committed, not what happens
to sit in the worktree), runs three configurations over it, and proves each counter can move.

Arms, all required:

* arm 1 scope -- every staged path lives under this bundle directory, and the file count is
  printed. Without it a stray staged file elsewhere in the tree would be silently scanned or,
  worse, silently left out. The materialised tree is hashed per path so the carrier names the
  exact bytes it scanned, and every staged path is compared against its worktree twin -- a
  non-zero count would mean the commit ships bytes that no earlier read looked at.
* arm 2 shipped config, baseline -- ``.gitleaks.toml`` as committed must read 0 findings. That is
  the commit precondition.
* arm 3 shipped config, planted -- one fabricated token (assembled at runtime, never printed)
  written into a copy of the payload must move arm 2's counter upwards. A zero that no plant can
  move is an unrun scanner, not a clean tree.
* arm 4 cross-check configs -- the same payload scanned with the repo allowlists stripped
  (``[extend] useDefault`` only) and with no config at all. The two baselines must agree (the
  strip removed allowlists and nothing else) and each must move on the planted tree.
* arm 5 suppression differential -- prints ``allowlist_suppressed = stripped_baseline -
  shipped_baseline`` as a measured counter with no threshold. A zero here means this payload
  contains nothing the shipped allowlists could hide; it is not a claim about payloads in general.
* arm 6 plant isolation -- the planted token only ever exists under the scratch tree. Re-scan the
  staged payload for any file named like a plant and require 0, so a plant can never become
  committed evidence.

Nothing is written into the repository. Reports are parsed for count and rule id only, so the
matched secret text never reaches stdout or a carrier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
GITLEAKS = "/opt/homebrew/bin/gitleaks"
SHIPPED_CONFIG = ".gitleaks.toml"
STRIPPED_CONFIG_TEXT = "[extend]\nuseDefault = true\n"
BUNDLE_REL = "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018"
PLANT_FILE_NAME = "scan_control_plant.txt"
#: Throwaway trees, removed in-run, never written inside the repo.
SCRATCH_ROOT = Path("/tmp/w31s6_scan")  # nosec B108  # noqa: S108


def _plant_token() -> str:
    """Fabricated and unused; assembled per call so no rule reads it off a literal."""
    return "ghp_" + ("0123456789" * 3 + "abcdefghij")


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603  # fixed argv, no shell, repo cwd
        argv, capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )


def _staged_paths() -> list[str]:
    proc = _run(["git", "diff", "--cached", "--name-only", "-z"])
    if proc.returncode != 0:
        raise SystemExit(f"git_diff_cached_exit={proc.returncode}")
    return [p for p in proc.stdout.split("\0") if p]


def _materialize(paths: list[str], dest: Path) -> int:
    if dest.exists():
        shutil.rmtree(dest)
    written = 0
    for rel in paths:
        proc = subprocess.run(  # noqa: S603  # nosec B603 B607  # git from PATH; rel is an arg
            ["git", "show", f":{rel}"],  # noqa: S607  # nosec B607  # same PATH lookup as _run
            capture_output=True,
            cwd=REPO_ROOT,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"git_show_exit={proc.returncode} path={rel}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(proc.stdout)
        written += 1
    return written


def _gitleaks(source: Path, report: Path, config: Path | None) -> tuple[int, int, dict[str, int]]:
    """One gitleaks run; returns (rc, finding count, per-rule counts) reading only the report."""
    argv = [
        GITLEAKS,
        "detect",
        "--no-git",
        "--source",
        str(source),
        "--report-format",
        "json",
        "--report-path",
        str(report),
        "--exit-code",
        "0",
    ]
    if config is not None:
        argv += ["--config", str(config)]
    proc = _run(argv)
    findings = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    if proc.returncode != 0:
        # rc is only a harness channel here; a non-zero means gitleaks itself failed.
        print(f"    gitleaks_error rc={proc.returncode} stderr_tail={proc.stderr.strip()[-200:]}")
    return proc.returncode, len(findings), dict(Counter(str(f.get("RuleID")) for f in findings))


def _plant(source: Path) -> None:
    (source / PLANT_FILE_NAME).write_text(f'GITHUB_TOKEN = "{_plant_token()}"\n', encoding="utf-8")


def _tree_digest(paths: list[str], dest: Path) -> str:
    """Bind the carrier to the exact bytes that were scanned, not just to a file count."""
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256((dest / rel).read_bytes()).hexdigest().encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """Scan the staged payload three ways, arm each counter, and print the face line."""
    ap = argparse.ArgumentParser(description="staged-payload credential scan")
    ap.add_argument("--keep-scratch", action="store_true", help="leave the scratch trees behind")
    args = ap.parse_args(argv)

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    paths = _staged_paths()
    outside = sorted(p for p in paths if not p.startswith(BUNDLE_REL + "/"))
    print(f"staged_files={len(paths)} staged_outside_bundle={len(outside)} outside_list={outside}")
    if not paths:
        print("credential_scan_face PASS=False reason=nothing-staged")
        return 1

    shipped = REPO_ROOT / SHIPPED_CONFIG
    stripped = SCRATCH_ROOT / "stripped.toml"
    stripped.write_text(STRIPPED_CONFIG_TEXT, encoding="utf-8")

    base_tree = SCRATCH_ROOT / "payload"
    file_count = _materialize(paths, base_tree)
    print(f"payload_materialized_from_index_files={file_count} tree={base_tree}")
    print(f"payload_index_sha256={_tree_digest(paths, base_tree)}")
    drifted = sorted(
        rel for rel in paths if (REPO_ROOT / rel).read_bytes() != (base_tree / rel).read_bytes()
    )
    print(f"worktree_differs_from_index_files={len(drifted)} list={drifted}")

    arms: dict[str, bool] = {}
    readings: list[str] = []
    exits: dict[str, int] = {}
    baseline_counts: dict[str, int] = {}
    for arm_name, config in (
        ("A_shipped", shipped),
        ("B_allowlist_stripped", stripped),
        ("C_builtin_no_config", None),
    ):
        plant_tree = SCRATCH_ROOT / f"planted_{arm_name}"
        if plant_tree.exists():
            shutil.rmtree(plant_tree)
        shutil.copytree(base_tree, plant_tree)
        _plant(plant_tree)

        rep = SCRATCH_ROOT / f"{arm_name}_base.json"
        rc, base_n, base_rules = _gitleaks(base_tree, rep, config)
        exits[f"{arm_name}_base"] = rc
        rep_p = SCRATCH_ROOT / f"{arm_name}_planted.json"
        rc_p, plant_n, plant_rules = _gitleaks(plant_tree, rep_p, config)
        exits[f"{arm_name}_planted"] = rc_p
        shutil.rmtree(plant_tree)

        readings.append(
            f"{arm_name}: baseline={base_n} baseline_rules={base_rules} "
            f"planted={plant_n} planted_rules_delta="
            f"{dict(Counter(plant_rules) - Counter(base_rules))} "
            f"moved={plant_n > base_n}"
        )
        baseline_counts[arm_name] = base_n
        arms[f"{arm_name}_clean"] = rc == 0 and rc_p == 0 and base_n == 0
        arms[f"{arm_name}_armed"] = plant_n > base_n

    for line in readings:
        print(line)
    suppressed = baseline_counts["B_allowlist_stripped"] - baseline_counts["A_shipped"]
    print(f"allowlist_suppressed_in_this_payload={suppressed} threshold=none")

    leftover = sorted(p.name for p in base_tree.rglob(PLANT_FILE_NAME))
    print(f"plant_files_inside_staged_payload={len(leftover)} names={leftover}")
    arms["scope"] = len(outside) == 0
    arms["index_is_worktree"] = len(drifted) == 0
    arms["baseline_zero"] = all(v for k, v in arms.items() if k.endswith("_clean"))
    arms["armed"] = all(v for k, v in arms.items() if k.endswith("_armed"))
    arms["config_strip_cross_check"] = (
        baseline_counts["B_allowlist_stripped"] == baseline_counts["C_builtin_no_config"]
    )
    arms["plant_isolated"] = len(leftover) == 0
    arms["exits_zero"] = all(rc == 0 for rc in exits.values())

    if not args.keep_scratch:
        shutil.rmtree(base_tree, ignore_errors=True)

    failed = [k for k, v in sorted(arms.items()) if not v]
    exit_readings = " ".join(f"gitleaks_{k}_exit={v}" for k, v in sorted(exits.items()))
    print(exit_readings)
    print(
        f"credential_scan_face staged_files={len(paths)} gitleaks_runs={len(exits)} "
        f"arms={len(arms)} arms_passed={sum(1 for v in arms.values() if v)} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    print(f"staged_payload_credential_scan_exit={0 if not failed else 1}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
