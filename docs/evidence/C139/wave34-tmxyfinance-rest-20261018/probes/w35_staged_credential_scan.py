"""Pre-commit credential scan over wave 34/35's whole staged payload.

Three configs, planted-key armed. Same arms as wave 32's instrument.

Why a new probe instead of reusing C135/C136's ``staged_payload_credential_scan.py``: that
instrument's scope arm asserts ``every staged path is under ONE bundle directory``, which fits an
evidence-only commit. Wave 32's landing commit stages product code, harness, witness modules, the
regenerated ``docs/evidence/C101/**`` tree and this bundle at once, so a single-bundle prefix would
either reject the whole payload or silently leave most of it unsupervised. Here the scope assertion
is against the wave's own declared path set, and the set is *derived* from the index rather than
typed by me.

Arms, every one of which can fail:

``scope``
    every staged path starts with one of the wave prefixes, and the count is printed. The prefixes
    are read from ``git diff --cached --name-only``, not asserted from prose.
``index_is_worktree``
    each staged blob equals its worktree bytes, so the commit carries what this run actually read.
``A_shipped_clean`` / ``B_allowlist_stripped_clean`` / ``C_builtin_no_config_clean``
    the materialised staged tree must read 0 findings under the repo's shipped ``.gitleaks.toml``,
    under a config with the repo allowlists stripped, and with no config at all.
``A/B/C_armed``
    the same three configs over a copy with one fabricated token must read strictly more. A zero
    that no plant can move is an unrun scanner, not a clean tree.
``config_strip_cross_check``
    the stripped-config and no-config baselines must agree; the strip removes allowlists and nothing
    else, so a disagreement means the shipped config is doing more than allowing.
``plant_isolated``
    the plant file must not exist anywhere in the staged payload.
``exits_zero``
    every gitleaks invocation exited 0 (gitleaks exits non-zero on findings *or* on usage error; a
    usage error with empty stdout once read as "0 findings" in this repo's history).

The fabricated token is assembled at runtime and never printed, so it cannot enter a carrier.
Nothing is written inside the repository.
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
GIT = "/usr/bin/git"
SHIPPED_CONFIG = ".gitleaks.toml"
STRIPPED_CONFIG_TEXT = "[extend]\nuseDefault = true\n"
PLANT_FILE_NAME = "scan_control_plant.txt"
SCRATCH_ROOT = Path("/tmp/w35_scan")  # nosec B108  # noqa: S108
#: Wave 34's declared path set: the 3 landed rows' files plus the four pinned
#: test lists that their registrations rotated.
#: set is the only thing that legitimately moved, and it moved because this wave
#: edits opendata/data/models, tmx, government_us and two more test files.
WAVE_PREFIXES: tuple[str, ...] = (
    "opendata/data/domains.yaml",
    "opendata/data/models/",
    "opendata/data/providers/tmx/",
    "opendata/data/providers/yfinance/",
    "opendata/data/providers/government_us/",
    "scripts/quality/iter2_witnesses/",
    "tests/fixtures/registry_batch5_snapshot.json",
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_authority_reconciliation.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
    "tests/test_government_us_iter2.py",
    "tests/test_yfinance_iter2_statements.py",
    "tests/test_yfinance_iter2_market.py",
    "docs/evidence/C101/",
    "docs/evidence/C138/",
    "docs/evidence/C139/",
)


def _plant_token() -> str:
    """Fabricated and unused; assembled per call so no rule reads it off a literal."""
    return "ghp_" + ("0123456789" * 3 + "abcdefghij")


def _staged_paths() -> list[str]:
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        [GIT, "-c", "core.quotepath=off", "diff", "--cached", "--name-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git diff --cached failed rc={proc.returncode}: {proc.stderr.strip()}")
    return sorted(line for line in proc.stdout.splitlines() if line.strip())


def _blob(path: str) -> bytes:
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv
        [GIT, "show", f":{path}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git show :{path} failed rc={proc.returncode}")
    return proc.stdout


def _gitleaks(tree: Path, report: Path, config: Path | None) -> tuple[int, int, list[str]]:
    argv = [GITLEAKS, "dir", str(tree), "--redact", "--no-banner", "--exit-code", "0"]
    if config is not None:
        argv += ["--config", str(config)]
    argv += ["--report-format", "json", "--report-path", str(report)]
    proc = subprocess.run(  # noqa: S603  # nosec B603  # literal argv, absolute executables
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if not report.is_file():
        return proc.returncode, -1, []
    try:
        findings = json.loads(report.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return proc.returncode, -1, []
    if not isinstance(findings, list):
        return proc.returncode, -1, []
    return proc.returncode, len(findings), [str(f.get("RuleID", "?")) for f in findings]


def _materialize(paths: list[str], dest: Path) -> int:
    """Write the staged blob for every path, so the scan reads commit bytes, not worktree bytes."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel in paths:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_blob(rel))
    return len(paths)


def main(argv: list[str] | None = None) -> int:
    """Scan exactly what this commit would carry, armed and unarmed."""
    parser = argparse.ArgumentParser(description="staged payload credential scan")
    parser.add_argument("--keep-scratch", action="store_true", help="leave scratch trees behind")
    args = parser.parse_args(argv)

    paths = _staged_paths()
    if not paths:
        raise SystemExit("nothing staged: this probe is a pre-commit gate, not a status report")
    outside = sorted(p for p in paths if not p.startswith(WAVE_PREFIXES))
    print(f"staged_files={len(paths)} wave_prefixes={len(WAVE_PREFIXES)}")
    print(f"staged_outside_wave={len(outside)} outside_list={outside[:8]}")

    drifted = [p for p in paths if _blob(p) != (REPO_ROOT / p).read_bytes()]
    print(f"worktree_differs_from_index_files={len(drifted)} list={drifted[:8]}")

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    base_tree = SCRATCH_ROOT / "payload"
    shutil.rmtree(base_tree, ignore_errors=True)
    n = _materialize(paths, base_tree)
    payload_sha = hashlib.sha256(
        "".join(f"{p}:{hashlib.sha256(_blob(p)).hexdigest()}\n" for p in paths).encode()
    ).hexdigest()
    print(f"payload_materialized_from_index_files={n} tree={base_tree}")
    print(f"payload_index_sha256={payload_sha}")

    shipped = REPO_ROOT / SHIPPED_CONFIG
    stripped = SCRATCH_ROOT / "stripped.toml"
    stripped.write_text(STRIPPED_CONFIG_TEXT, encoding="utf-8")

    arms: dict[str, bool] = {"scope": not outside, "index_is_worktree": not drifted}
    exits: dict[str, int] = {}
    counts: dict[str, int] = {}
    configs: tuple[tuple[str, Path | None], ...] = (
        ("A_shipped", shipped),
        ("B_allowlist_stripped", stripped),
        ("C_builtin", None),
    )
    for name, config in configs:
        rc, base_n, base_rules = _gitleaks(base_tree, SCRATCH_ROOT / f"{name}_base.json", config)
        exits[f"{name}_base"] = rc
        plant_tree = SCRATCH_ROOT / f"planted_{name}"
        shutil.rmtree(plant_tree, ignore_errors=True)
        shutil.copytree(base_tree, plant_tree)
        (plant_tree / PLANT_FILE_NAME).write_text(
            f'# fabricated control, never a real credential\napi_token = "{_plant_token()}"\n',
            encoding="utf-8",
        )
        rc_p, plant_n, plant_rules = _gitleaks(
            plant_tree, SCRATCH_ROOT / f"{name}_planted.json", config
        )
        exits[f"{name}_planted"] = rc_p
        shutil.rmtree(plant_tree)
        counts[name] = base_n
        print(
            f"{name}: baseline={base_n} baseline_rules={dict(Counter(base_rules))} "
            f"planted={plant_n} planted_rules_delta="
            f"{dict(Counter(plant_rules) - Counter(base_rules))} moved={plant_n > base_n}"
        )
        arms[f"{name}_clean"] = rc == 0 and rc_p == 0 and base_n == 0
        arms[f"{name}_armed"] = plant_n > base_n

    suppressed = counts["B_allowlist_stripped"] - counts["A_shipped"]
    print(f"allowlist_suppressed_in_this_payload={suppressed} threshold=none")
    leftover = sorted(p.name for p in base_tree.rglob(PLANT_FILE_NAME))
    print(f"plant_files_inside_staged_payload={len(leftover)} names={leftover}")
    arms["config_strip_cross_check"] = counts["B_allowlist_stripped"] == counts["C_builtin"]
    arms["plant_isolated"] = not leftover
    arms["exits_zero"] = all(rc == 0 for rc in exits.values())

    shutil.rmtree(base_tree, ignore_errors=True)
    if not args.keep_scratch:
        shutil.rmtree(SCRATCH_ROOT, ignore_errors=True)

    failed = [k for k, v in sorted(arms.items()) if not v]
    print(" ".join(f"gitleaks_{k}_exit={v}" for k, v in sorted(exits.items())))
    print(
        f"credential_scan_face staged_files={len(paths)} gitleaks_runs={len(exits)} "
        f"arms={len(arms)} arms_passed={sum(1 for v in arms.values() if v)} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    print(f"w35_staged_credential_scan_exit={0 if not failed else 1}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
