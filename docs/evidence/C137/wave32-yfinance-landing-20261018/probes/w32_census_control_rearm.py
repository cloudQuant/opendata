"""Re-arm the negative control wave 32 consumed, without touching the shipped C128 bytes.

``docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py`` gates
``B_landed_negative`` on ``verdict not in TRIPPABLE`` *and* membership in its own residue, and its
``CONTROL_NEGATIVE`` is ``yfinance::BalanceSheet`` -- the exact row this wave landed. The arm is now
red (``arms=8 arms_passed=7 arms_failed=1``) and the shipped module's own comment says what to do
about it: "The next landing that consumes this control should re-base it the same way and keep the
delta in its own face." This probe keeps the delta.

Why an in-memory forged copy rather than another residue row. The shipped retirement note rejects
two populations by argument, and this wave measures it instead: arm ``B_population_search`` walks
the census's own 33-row residue table and asks for the consumed control's shape (non-trippable
*and* bound, since an ``UNBOUND`` row "cannot be tripped by anything" and would make the arm
vacuous). The answer is zero -- every non-trippable residue row in this generation is ``UNBOUND``
(32 of them) and the single bound one (``yfinance::KeyMetrics``) reads trippable. So no
disk-resident identity can serve, and a name picked off the registry could be consumed by the next
landing anyway.

The re-armed control is therefore derived in memory and never written: the shipped row measurer
``incomplete_row_guard_check._row`` is called unmodified while ``pathlib.Path.read_text`` answers,
for the module paths *this wave changed*, with ``git show HEAD:<path>`` bytes -- the pre-landing
shape of the very class that was consumed. The patch is inert for every other path and for every
other axis. No verdict, bucket name or predicate is re-typed here: ``TRIPPABLE``, ``_verdict`` and
the site scan all come from the imported shipped module, and arm ``C_harness_inert`` proves the
substitution mechanism itself cannot move a reading (patching with the worktree bytes reproduces the
live face field for field).

Arms (all seven can fail):

``A_red_attributed``     the shipped census's own arm table, verbatim: exactly one failed arm, its
                         name, the consumed row's judged status and its measured verdict. Red is
                         attributed to consumption (status ``OFFLINE_ACCEPTED`` + verdict trippable
                         + membership lost), not to a regression (all other shipped arms green).
``B_population_search``  the residue candidate search above, complete and empty.
``C_harness_inert``      worktree bytes behind the patch == live face, field for field.
``D_forged_control``     HEAD bytes behind the patch read non-trippable, while every non-site axis
                         (bound / declared / row-class identity / domain / files) is unchanged.
``E_two_sided``          the re-armed predicate passes on the forged copy and refuses in BOTH
                         directions: losing population membership, and reading the landed verdict.
``F_unconsumable``       the forged identity is absent from all 350 judged identities, so no future
                         landing can retire this control the way wave 32 retired the last one.
``G_blind_spot``         the census's known blindness, printed and gated: exactly one residue row
                         reads ``PRODUCIBLE_NOW`` / ``DOCUMENT_REACHABLE`` while the installed
                         yfinance publishes no valuation-measures surface. The census reads this
                         repository (AST sites, registry, claims shard, seam surface); it never
                         imports the installed SDK, so it cannot see that absence. Recorded as a
                         finding for the ledger owner, not fixed.

Exits non-zero when an arm fails. Nothing under version control is edited by this probe.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import pathlib
import re
import subprocess  # nosec B404
import sys
import unittest.mock
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
C128 = REPO_ROOT / "docs/evidence/C128/plane1-residue-readiness-20261017/probes"
SHIPPED_CENSUS = C128 / "residue_axis_census.py"
ROW_RE = re.compile(r"(\w+)=(\S+)")
FORGED_SUFFIX = "#w32-in-memory-forged-copy"

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(C128))

import incomplete_row_guard_check as axes  # noqa: E402  # the shipped C128 row measurer
import seam_surface_match as seam  # noqa: E402  # the shipped judged-report reader

_READ_TEXT = pathlib.Path.read_text


def _run_shipped_census() -> tuple[str, int]:
    """Run the shipped census as a subprocess and keep its stdout; its exit code is a reading."""
    res = subprocess.run(  # nosec B603  # noqa: S603
        [sys.executable, str(SHIPPED_CENSUS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    return res.stdout, res.returncode


def _git_show_head(rel_path: str) -> str:
    """Read one path's bytes as they stood before this wave, in memory only."""
    return subprocess.run(  # nosec B603  # noqa: S603
        ["git", "show", f"HEAD:{rel_path}"],  # nosec B404  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,  # nosec B404
    ).stdout


def _wave_changed_paths() -> list[str]:
    """Paths the working tree actually differs on from HEAD -- the only ones the arm may forge."""
    return subprocess.run(  # nosec B603
        ["git", "diff", "HEAD", "--name-only"],  # nosec B404  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,  # nosec B404
    ).stdout.split()


def _face_row(census_out: str, identity: str) -> dict[str, str]:
    for line in census_out.splitlines():
        if line.startswith("row ") and line.split()[1] == identity:
            return dict(ROW_RE.findall(line))
    raise AssertionError(f"shipped census printed no row line for {identity}")


def _patched_row(status_label: str, byte_map: dict[str, str]) -> dict[str, Any]:
    """Call the shipped ``_row`` while the listed paths answer with forged in-memory bytes."""

    def read_text(self: pathlib.Path, *args: str, **kwargs: str) -> str:
        forged = byte_map.get(str(self))
        return _READ_TEXT(self, *args, **kwargs) if forged is None else forged

    with unittest.mock.patch.object(pathlib.Path, "read_text", read_text):
        face: dict[str, Any] = axes._row(axes.CONTROL_NEGATIVE, status_label)
    return face


def main() -> int:
    """Run the shipped census, forge the control in memory, gate the re-armed arm."""
    print(
        f"argv: {' '.join([sys.executable, *sys.argv])}   "
        f"(cwd={REPO_ROOT}, PYTHONPATH={REPO_ROOT}, nothing under version control written)"
    )
    census_out, census_rc = _run_shipped_census()
    report = seam._load_report()
    judged: dict[str, str] = {str(r["model_identity"]): str(r["status"]) for r in report["models"]}
    residue = {identity for identity, status in judged.items() if status != "OFFLINE_ACCEPTED"}
    live = axes._row(axes.CONTROL_NEGATIVE, judged.get(axes.CONTROL_NEGATIVE, "?"))
    arms_line = next(ln for ln in census_out.splitlines() if "arms_failed=" in ln)
    arm_b_line = next(ln for ln in census_out.splitlines() if ln.startswith("B_landed_negative"))
    failed_arms = [
        ln.split()[0]
        for ln in census_out.splitlines()
        if re.match(r"^[A-H]_\w+ ", ln) and ln.rstrip().endswith("PASS=False")
    ]
    print(f"shipped_census {arms_line}")
    print(f"shipped_census_arm_b {arm_b_line} exit={census_rc}")
    arm_a = (
        failed_arms == ["B_landed_negative"]
        and live["verdict"] in axes.TRIPPABLE
        and judged.get(axes.CONTROL_NEGATIVE) == "OFFLINE_ACCEPTED"
        and axes.CONTROL_NEGATIVE not in residue
    )
    print(
        f"A_red_attributed failed_arms={failed_arms} "
        f"judged_status={judged.get(axes.CONTROL_NEGATIVE)} verdict={live['verdict']} "
        f"trippable={live['verdict'] in axes.TRIPPABLE} "
        f"in_residue={axes.CONTROL_NEGATIVE in residue} "
        f"other_shipped_arms_green={census_out.count('PASS=True')} PASS={arm_a}"
    )

    row_verdicts = Counter(
        dict(ROW_RE.findall(ln))["verdict"]
        for ln in census_out.splitlines()
        if ln.startswith("row ")
    )
    candidates = [
        ln.split()[1]
        for ln in census_out.splitlines()
        if ln.startswith("row ")
        and dict(ROW_RE.findall(ln))["verdict"] not in axes.TRIPPABLE
        and dict(ROW_RE.findall(ln))["verdict"] != "UNBOUND"
    ]
    arm_b_pop = len(residue) == sum(row_verdicts.values()) and not candidates
    print(
        f"B_population_search searched={sum(row_verdicts.values())} of={len(residue)} "
        f"verdicts={dict(sorted(row_verdicts.items()))} candidates={candidates} PASS={arm_b_pop}"
    )

    byte_map = {
        str((REPO_ROOT / rel).resolve()): _git_show_head(rel)
        for rel in sorted(live.get("files", []))
        if rel in _wave_changed_paths()
    }
    inert_map = {path: _READ_TEXT(Path(path), encoding="utf-8") for path in byte_map}
    inert = _patched_row(live["status"], inert_map)
    arm_c = inert == live
    print(
        f"C_harness_inert patched_paths={sorted(byte_map)} fields_compared={len(inert)} "
        f"identical={arm_c} verdict={inert['verdict']} PASS={arm_c}"
    )

    forged = _patched_row(live["status"], byte_map)
    site_keys = (
        "counts",
        "trippable",
        "doc_sites",
        "raises",
        "code_raises",
        "shipped_sites",
        "verdict",
    )
    differing = {
        key: (live.get(key), forged.get(key))
        for key in sorted(set(live) | set(forged))
        if live.get(key) != forged.get(key)
    }
    site_only = set(differing) <= set(site_keys)
    arm_d = forged["verdict"] not in axes.TRIPPABLE and site_only and bool(byte_map)
    print(
        f"D_forged_control verdict={forged['verdict']} counts={forged.get('counts')} "
        f"raises={forged.get('raises')} code_raises={forged.get('code_raises')} "
        f"trippable_n={len(forged.get('trippable', []))} live_verdict={live['verdict']} "
        f"differing_keys={sorted(differing)} "
        f"outside_site_axes={sorted(set(differing) - set(site_keys))} "
        f"only_site_axes_moved={site_only} "
        f"declared_same={live.get('declared') == forged.get('declared')} "
        f"row_class_same={live.get('is_contract') == forged.get('is_contract')} PASS={arm_d}"
    )

    forged_identity = f"{axes.CONTROL_NEGATIVE}{FORGED_SUFFIX}"
    forged_population = residue | {forged_identity}

    def arm_b_predicate(verdict: str, identity: str, population: set[str]) -> bool:
        return verdict not in axes.TRIPPABLE and identity in population

    holds = arm_b_predicate(forged["verdict"], forged_identity, forged_population)
    refuses_landing = not arm_b_predicate(forged["verdict"], forged_identity, residue)
    refuses_trip = not arm_b_predicate(live["verdict"], forged_identity, forged_population)
    arm_e = holds and refuses_landing and refuses_trip
    print(
        f"E_two_sided forged={forged_identity} holds={holds} "
        f"refuses_if_consumed_by_landing={refuses_landing} "
        f"refuses_if_verdict_tripped={refuses_trip} PASS={arm_e}"
    )

    arm_f = forged_identity not in judged and axes.CONTROL_NEGATIVE in judged
    print(
        f"F_unconsumable judged_identities={len(judged)} "
        f"forged_in_report={forged_identity in judged} "
        f"consumed_control_in_report={axes.CONTROL_NEGATIVE in judged} "
        f"consumed_status={judged.get(axes.CONTROL_NEGATIVE)} PASS={arm_f}"
    )

    cross_tab = next(
        (ln for ln in census_out.splitlines() if ln.startswith("1_cross_tab")), "<missing>"
    )
    km_row = _face_row(census_out, "yfinance::KeyMetrics")
    produced = [
        vid
        for vid in sorted(residue)
        if _face_row(census_out, vid).get("bucket") == "PRODUCIBLE_NOW"
    ]
    sdk_version = importlib.metadata.version("yfinance")
    yf = importlib.import_module("yfinance")
    surface = sorted(
        name for name in dir(yf.Ticker) if re.search(r"key_?metric|valuation", name, re.I)
    )
    arm_g = produced == ["yfinance::KeyMetrics"] and not surface
    print(
        f"G_blind_spot census_cross_tab={cross_tab.split(' ', 1)[1]} "
        f"producible_now_rows={produced} key_metrics={km_row['status']}/{km_row['verdict']} "
        f"bucket={km_row['bucket']} action={km_row['action']} "
        f"installed_yfinance={sdk_version} sdk_valuation_surface={surface or 'ABSENT'} "
        f"census_reads='AST sites + registry + claims shard + seam surface, never the SDK' "
        f"PASS={arm_g}"
    )

    arms = (arm_a, arm_b_pop, arm_c, arm_d, arm_e, arm_f, arm_g)
    print(
        f"rearm_face consumed_control={axes.CONTROL_NEGATIVE} "
        f"rearmed_control={forged_identity} forged_verdict={forged['verdict']} "
        f"byte_source=git_show_HEAD_in_memory paths={len(byte_map)} "
        f"shipped_census_arms={arms_line.partition('arms=')[2].split()[0]}"
    )
    print(
        f"w32_census_control_rearm_face arms={len(arms)} arms_passed={sum(arms)} "
        f"arms_failed={len(arms) - sum(arms)} PASS={all(arms)}"
    )
    rc = 0 if all(arms) else 1
    print(f"w32_census_control_rearm_exit={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
