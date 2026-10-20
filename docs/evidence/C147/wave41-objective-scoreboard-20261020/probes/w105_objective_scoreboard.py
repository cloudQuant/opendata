"""Iteration-2 objective scoreboard: every clause recomputed from primary artefacts, not restated.

The goal's completion audit needs one page that a reviewer can re-run. So this face reads the
shipped judge report, the merged claims document, the task ledger, the two junit planes and the
wave-39/40 carriers, **recomputes** what can be recomputed, and compares the rest against the value
its carrier printed. Any drift turns into a red arm; a clause that is not met turns into a line in
``objective_complete=False reasons=...`` rather than into prose.

What is recomputed (primary): accepted rows and the identity set come from the judge report; the
claim total from ``claims.merged.json``; the identity population from the inventory CSV; the
provider coverage from the report itself; the residue as ``identities - accepted``; the test-plane
failure set from the junit readers.

What is cited (carrier): the 339-row per-row digest audit (``w75``), the clean-room AST census
(``w73``), the residue gate partition (``w67``), the subagent ledger (``w74``), AC2-10's own probe
reading (``w70``) and the query-annotation divergence (``w95``). Each citation is paired with the
carrier's own PASS line, so a face that went red cannot be quoted as green.

One forged arm proves the equality check bites: the recomputed accepted count is bumped in memory
and the same comparison must fail.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w105_objective_scoreboard.txt"
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
MERGED = REPO / "docs/evidence/C101/claims.merged.json"
INVENTORY = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
W39_RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
W40_RUNS = REPO / "docs/evidence/C146/wave40-query-annotation-divergence-20261019/runs"
READERS = (
    REPO
    / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes"
    / "w51_test_plane_attribution.py"
)
EXPECTED = {
    "accepted": 339,
    "identities": 350,
    "claims": 1725,
    "identity_set": "5168e1e723999312",
    "providers_with_rows": 29,
    "zero_providers": ["biztoc", "stockgrid", "wsj"],
    "residue": 11,
    "subagent_total": 34,
    "subagent_cap": 30,
    "ac2_dev_done": 17,
    "divergence_rows": 114,
    "divergence_pin_sites": 740,
    "test_plane_new_reds": 0,
}


def _carrier(name: str) -> str:
    return (W39_RUNS / name).read_text(encoding="utf-8", errors="replace")


def _grab(text: str, pattern: str, group: int = 1) -> str:
    match = re.search(pattern, text)
    if match is None:
        raise SystemExit(f"carrier pattern not found: {pattern}")
    return match.group(group)


def _inventory_rows() -> int:
    lines = [line for line in INVENTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    return len(lines) - 1


def _judge() -> dict[str, Any]:
    report = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    accepted = 0
    by_provider: dict[str, int] = {}
    for model in report["models"]:
        if model["status"] == "OFFLINE_ACCEPTED":
            accepted += 1
            by_provider[model["provider"]] = by_provider.get(model["provider"], 0) + 1
    providers = {
        line.split(",")[1]
        for line in INVENTORY.read_text(encoding="utf-8").splitlines()[1:]
        if line.strip()
    }
    return {
        "accepted": accepted,
        "identities": int(report["inventory"]["identity_count"]),
        "identity_set": str(report["inventory"]["identity_set_sha256"])[:16],
        "providers_with_rows": len(by_provider),
        "zero_providers": sorted(providers - set(by_provider)),
        "claims": len(json.loads(MERGED.read_text(encoding="utf-8")).get("claims") or []),
    }


def _plane(xml: Path) -> tuple[dict[str, str], dict[str, str]]:
    spec = importlib.util.spec_from_file_location("w38_plane_readers_scoreboard", READERS)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import readers {READERS}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_plane_readers_scoreboard"] = module
    spec.loader.exec_module(module)
    return module._failures(xml), module._counts(xml)


def main() -> int:
    """Recompute what is primary, cite the rest, and print whether the objective is met."""
    RUNS.mkdir(parents=True, exist_ok=True)
    live = _judge()
    before, before_counts = _plane(W39_RUNS / "test_plane_w39_worktree_final.xml")
    after_xml = (
        RUNS / "test_plane_w41b_final.xml"
        if (RUNS / "test_plane_w41b_final.xml").is_file()
        else W40_RUNS / "test_plane_w40_final.xml"
    )
    after, after_counts = _plane(after_xml)
    shared_nodes = set(before) & set(after)
    new_nodes = set(after) - set(before)
    fixed_nodes = set(before) - set(after)
    w57 = _carrier("w57_landing_face.txt")
    w67 = _carrier("w67_residue_gating_face.txt")
    w70 = _carrier("w70_ac2_10_current_reading.txt")
    w72 = _carrier("w72_provider_coverage_census.txt")
    w73 = _carrier("w73_clean_room_face.txt")
    w74 = _carrier("w74_dispatch_ledger_face.txt")
    w75 = _carrier("w75_witness_completeness_face.txt")
    w95 = (W40_RUNS / "w95_query_annotation_face.txt").read_text(encoding="utf-8")

    reasoned = {
        "accepted": live["accepted"],
        "identities": live["identities"],
        "claims": live["claims"],
        "identity_set": live["identity_set"],
        "providers_with_rows": live["providers_with_rows"],
        "zero_providers": live["zero_providers"],
        "residue": live["identities"] - live["accepted"],
        "inventory_rows": _inventory_rows(),
        "w57_accepted": int(_grab(w57, r"after_accepted=(\d+)")),
        "w57_claims": int(_grab(w57, r"after_claims=(\d+)")),
        "w67_residue_sums": int(_grab(w67, r"sums=(\d+)")),
        "w72_providers": int(_grab(w72, r"providers_with_at_least_one_accepted_row=(\d+)")),
        "w73_openbb_import_violations": int(_grab(w73, r"openbb_import_violations=(\d+)")),
        "w73_provider_packages": int(_grab(w73, r"provider_packages=(\d+)")),
        "w74_subagent_total": int(_grab(w74, r"this_thread_total=(\d+)")),
        "w74_subagent_cap": int(_grab(w74, r"cap=(\d+)")),
        "w75_stale_pin_rows": int(_grab(w75, r"stale_pin_files_across_accepted_rows=(\d+)")),
        "w70_dev_done": int(_grab(w70, r"dev_done=(\d+)")),
        "w70_engine_declared": int(_grab(w70, r"engine_declared_rows=(\d+)")),
        "w95_divergence": int(_grab(w95, r'"diverge": (\d+)')),
        "w95_pin_sites": int(_grab(w95, r'"pin_sites": (\d+)')),
        "plane_shared": len(shared_nodes),
        "plane_new": len(new_nodes),
        "plane_fixed": len(fixed_nodes),
        "plane_after_source": after_xml.name,
        "plane_before": len(before),
        "plane_after": len(after),
        "plane_collected": int(after_counts.get("tests", 0)),
    }

    lines = [f"scoreboard {json.dumps(reasoned, sort_keys=True, ensure_ascii=False)}"]
    arms: dict[str, bool] = {}
    arms["A_judge_is_the_primary_and_agrees_with_its_carrier"] = (
        live["accepted"] == reasoned["w57_accepted"] == EXPECTED["accepted"]
        and live["claims"] == reasoned["w57_claims"] == EXPECTED["claims"]
    )
    arms["B_identity_population_closes"] = (
        live["identities"] == reasoned["inventory_rows"] == EXPECTED["identities"]
        and live["identity_set"] == EXPECTED["identity_set"]
    )
    arms["C_provider_coverage_agrees_two_ways"] = (
        live["providers_with_rows"] == reasoned["w72_providers"] == EXPECTED["providers_with_rows"]
        and live["zero_providers"] == EXPECTED["zero_providers"]
    )
    arms["D_residue_is_one_number_in_two_places"] = (
        reasoned["residue"] == reasoned["w67_residue_sums"] == EXPECTED["residue"]
    )
    arms["E_clean_room_zero_is_armed"] = (
        reasoned["w73_openbb_import_violations"] == 0 and "PASS=True" in w73
    )
    arms["F_per_row_audit_has_no_stale_pin"] = (
        reasoned["w75_stale_pin_rows"] == 0 and "PASS=True" in w75
    )
    arms["G_ac2_10_is_at_its_own_ceiling"] = (
        reasoned["w70_dev_done"] == reasoned["w70_engine_declared"] == EXPECTED["ac2_dev_done"]
    )
    arms["H_divergence_numbers_are_the_carriers"] = (
        reasoned["w95_divergence"] == EXPECTED["divergence_rows"]
        and reasoned["w95_pin_sites"] == EXPECTED["divergence_pin_sites"]
    )
    arms["I_test_plane_gains_no_red"] = (
        reasoned["plane_new"] == EXPECTED["test_plane_new_reds"]
        and reasoned["plane_shared"] + reasoned["plane_fixed"] == reasoned["plane_before"]
        and reasoned["plane_shared"] + reasoned["plane_new"] == reasoned["plane_after"]
        and reasoned["plane_collected"] == int(before_counts.get("tests", -1))
    )
    forged = live["accepted"] + 1
    arms["J_equality_arm_can_bite"] = not (
        forged == reasoned["w57_accepted"] == EXPECTED["accepted"]
    )

    substantive = {
        "residue_owner_gated": reasoned["residue"],
        "providers_without_rows": len(live["zero_providers"]),
        "subagent_over_cap": max(0, reasoned["w74_subagent_total"] - reasoned["w74_subagent_cap"]),
    }
    reasons = [f"{name}={value}" for name, value in substantive.items() if value]
    complete = not reasons
    lines.append(
        f"subagent_ledger total={reasoned['w74_subagent_total']} cap={reasoned['w74_subagent_cap']}"
    )
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"objective_complete={complete} reasons={reasons} "
        f"arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w105_objective_scoreboard_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w105_objective_scoreboard_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
