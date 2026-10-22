"""Wave-58 copy of the staged-payload credential gate, with one amended arm.

The original lives at ``docs/evidence/C143/.../w41b_staged_credential_scan.py`` and is left
untouched;
this copy exists so a later wave can apply the registered-suppression predicate above without
rewriting wave 38's shipped instrument or its committed carriers.

Original docstring follows.

Pre-commit credential scan over wave 38's staged payload, armed and unarmed.

Clone of the shipped wave-34 instrument (``probes/w36_staged_credential_scan.py`` in
``docs/evidence/C139/wave34-tmxyfinance-rest-20261018``) with two things deliberately NOT
inherited: the scope prefix list (this wave lands cboe product code,
a witness shard, two re-pinned tests, the regenerated ``docs/evidence/C101/**`` tree, and the C140
capture bundle) and the scratch root (a shared ``/tmp`` tree would let two runs read each other).
The arms, the runtime-assembled plant and the "gitleaks exit 0 with a report file" discipline are
copied verbatim because they are what the earlier wave proved.

Why scan the index and not the worktree: the commit carries the staged blob. A worktree-only scan
can be green while the index holds a token an earlier ``git add`` captured
(``feedback-commit-payload-lint-population``), so every byte scanned here comes from
``git show :<path>``.

Arms, each of which can fail:

``scope``
    every staged path starts with one of this wave's declared prefixes, and the offenders are
    printed. The list is typed on purpose: a list derived from the index could not catch a path
    that should not be in the index.
``index_is_worktree``
    each staged blob equals the worktree bytes, so the commit carries what this run read -- except
    the single path the driver itself declares in ``SELF_CARRIER_EXEMPT`` (this screen's own
    carrier, which is written after it has read the index).
``self_carrier_exemption_exact``
    that exemption is at most one path and every declared path lies under this bundle's ``runs/``;
    the ``--control`` face shows it reddening when it names a product file.
``A_shipped_clean`` / ``B_allowlist_stripped_clean`` / ``C_builtin_no_config_clean`` and their
``*_armed`` partners
    zero findings under the shipped ``.gitleaks.toml``, under a config with the repo allowlists
    stripped, and with no config at all; each must read strictly more once a fabricated token is
    planted in a copy of the same tree.
``config_strip_cross_check``
    the stripped and built-in baselines agree, so the shipped config suppresses nothing in this
    payload.
``plant_isolated``
    the plant file exists nowhere inside the staged payload.
``exits_zero``
    every gitleaks invocation exited 0 -- findings and usage errors both leave a report behind,
    and a missing report is recorded as ``-1``, never as ``0``.

The fabricated token is assembled at runtime and never printed, so it cannot enter a carrier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from collections import Counter
from collections.abc import Callable, Sequence  # noqa: TC003  # annotations only
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
GITLEAKS = "/opt/homebrew/bin/gitleaks"
GIT = "/usr/bin/git"
SHIPPED_CONFIG = ".gitleaks.toml"
STRIPPED_CONFIG_TEXT = "[extend]\nuseDefault = true\n"
PLANT_FILE_NAME = "scan_control_plant.txt"
SCRATCH_ROOT = Path("/tmp/w41b_scan")  # nosec B108  # noqa: S108
CARRIER = Path(__file__).resolve().parent.parent / "runs" / "w41b_staged_credential_scan.txt"
#: A screen that writes its carrier inside the tree it screens cannot read its own final bytes: the
#: staged blob is by construction one generation behind the worktree write, so ``index_is_worktree``
#: is unsatisfiable for that one path (measured 2026-10-22: the arm went red on its own carrier).
#: The driver declares the path instead of dropping the arm, and the prefix below is what keeps the
#: declaration honest - naming a product file reddens ``self_carrier_exemption_exact``.
CARRIER_EXEMPT_ALLOWED_PREFIX = "docs/evidence/C147/wave41-objective-scoreboard-20261020/runs/"
SELF_CARRIER_EXEMPT: tuple[str, ...] = ()
#: Wave 38's declared path set: the landed seeking_alpha row's package, the catalog entry that
#: moves it from reserved to implemented, the three test enumerations its registration rotates,
#: the regenerated claim tree, and the two evidence bundles this wave writes (plus C141, whose
#: wave-37 carriers stay untouched).
WAVE_PREFIXES: tuple[str, ...] = (
    "opendata/data/domains.yaml",
    "opendata/data/models/",
    "opendata/data/providers/catalog.py",
    "opendata/data/providers/seeking_alpha/",
    "scripts/quality/iter2_witnesses/seeking_alpha.py",
    "tests/test_seeking_alpha.py",
    "tests/test_provider_catalog.py",
    "tests/test_authority_reconciliation.py",
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
    "tests/fixtures/registry_batch5_snapshot.json",
    "docs/evidence/C101/",
    "docs/evidence/C142/",
    "docs/evidence/C143/",
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


def _worktree(path: str) -> bytes:
    return (REPO_ROOT / path).read_bytes()


def _drift_reading(
    paths: Sequence[str],
    exempt: Sequence[str],
    *,
    index_blob: Callable[[str], bytes] = _blob,
    worktree: Callable[[str], bytes] = _worktree,
) -> tuple[list[str], list[str]]:
    """Split index-vs-worktree divergence into (arm-visible drift, self-writes).

    The readers are parameters so ``--control`` can forge a divergence in a file the run must not
    touch; a control that can only observe equality proves nothing (``feedback-contrast-arm-must-be-
    able-to-differ``).
    """
    drifted: list[str] = []
    self_hits: list[str] = []
    for path in paths:
        if index_blob(path) != worktree(path):
            (self_hits if path in exempt else drifted).append(path)
    return drifted, self_hits


def _exemption_is_carrier_only(exempt: Sequence[str]) -> bool:
    """An exemption may only name a carrier under ``runs/``, never a product file."""
    return all(str(path).startswith(CARRIER_EXEMPT_ALLOWED_PREFIX) for path in exempt)


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

    drifted, self_hits = _drift_reading(paths, SELF_CARRIER_EXEMPT)
    print(f"worktree_differs_from_index_files={len(drifted)} list={drifted[:8]}")
    print(
        f"self_carrier_exemption declared={list(SELF_CARRIER_EXEMPT)} "
        f"carrier_only={_exemption_is_carrier_only(SELF_CARRIER_EXEMPT)} "
        f"used={len(self_hits)} names={self_hits[:3]}"
    )

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

    arms: dict[str, bool] = {
        "scope": not outside,
        "index_is_worktree": not drifted,
        # One name, and that name must live under this bundle's runs/ -- an exemption that could
        # point at opendata/** would launder any unstaged product edit past the arm.
        "self_carrier_exemption_exact": _exemption_is_carrier_only(SELF_CARRIER_EXEMPT)
        and len(SELF_CARRIER_EXEMPT) <= 1,
    }
    deferred: dict[str, bool] = {}
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
        # Wave-58 amendment (owner ruling C, applied to this instrument as well): the strict
        # "zero findings even with the allowlist stripped" reading of the B/C arms is unsatisfiable
        # for any payload that carries a product pattern `.gitleaks.toml` already registers -
        # measured today: 12 H.15 `source_key` lines in federal_reserve/specs.py, which A2's
        # zero-tolerance now obliges a wave to touch. The arm is replaced, not dropped: the
        # suppression must equal EXPECTED_SUPPRESSIONS exactly (see the post-loop block), `A` must
        # still read zero, and the planted control must still move every rule set. An unregistered
        # secret cannot ride through, because it raises A's own count.
        if name == "A_shipped":
            arms[f"{name}_clean"] = rc == 0 and rc_p == 0 and base_n == 0
        else:
            # Deferred to the registered-suppression predicate below: the shipped-config face is
            # the security claim; the stripped/built-in faces only say something via the whitelist.
            deferred[name] = rc == 0 and rc_p == 0
        arms[f"{name}_armed"] = plant_n > base_n

    # Registered-suppression whitelist: the file -> count pairs this payload may have hidden by the
    # shipped allowlist, and nothing else. A new file or a changed count reddens the arm, so every
    # suppression has to be looked at rather than inherited.
    expected = {"opendata/data/providers/federal_reserve/specs.py": 12}
    # Each rule set gets its own file -> count map; summing B and C together would demand a count of
    # 24 for 12 suppressed lines and the arm would refuse a payload it is meant to accept.
    observed: dict[str, dict[str, int]] = {}
    for name in ("B_allowlist_stripped", "C_builtin"):
        report = SCRATCH_ROOT / f"{name}_base.json"
        per_file: dict[str, int] = {}
        if report.is_file():
            for finding in json.loads(report.read_text(encoding="utf-8")):
                raw = str(finding.get("File", ""))
                try:
                    key = str(Path(raw).resolve().relative_to(base_tree.resolve()))
                except (ValueError, OSError):
                    key = raw.lstrip("./")
                per_file[key] = per_file.get(key, 0) + 1
        observed[name] = {k: v for k, v in per_file.items() if v}
    whitelist_ok = counts["A_shipped"] == 0 and all(
        not seen or seen == expected for seen in observed.values()
    )
    arms["suppression_whitelist_exact"] = whitelist_ok
    for name, quiet in deferred.items():
        arms[f"{name}_clean"] = quiet and (counts[name] == 0 or whitelist_ok)
    print(
        f"registered_suppressions_in_payload={observed} "
        f"expected={expected} whitelist_match={whitelist_ok}"
    )
    suppressed = counts["B_allowlist_stripped"] - counts["A_shipped"]
    print(
        f"allowlist_suppressed_in_this_payload={suppressed} "
        "clean_predicate=base_zero AND suppression_whitelist_exact (wave-58 amendment)"
    )
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
    print(f"w41b_staged_credential_scan_exit={0 if not failed else 1}")
    return 0 if not failed else 1


class _Tee:
    """Mirror stdout into a buffer so the run ships its own carrier verbatim."""

    def __init__(self, stream: Any) -> None:  # noqa: ANN401  # a text stream, whatever stdout is
        self.stream = stream
        self.buffer: list[str] = []

    def write(self, text: str) -> int:
        self.buffer.append(text)
        self.stream.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


if __name__ == "__main__":
    tee = _Tee(sys.__stdout__)
    sys.stdout = tee
    try:
        code = main()
    finally:
        sys.stdout = sys.__stdout__
        CARRIER.parent.mkdir(parents=True, exist_ok=True)
        CARRIER.write_text(
            "# carrier for w41b_staged_credential_scan.py (this run's stdout, verbatim)\n"
            f"# argv: {sys.executable} {Path(__file__).resolve()}\n" + "".join(tee.buffer),
            encoding="utf-8",
        )
    sys.exit(code)
