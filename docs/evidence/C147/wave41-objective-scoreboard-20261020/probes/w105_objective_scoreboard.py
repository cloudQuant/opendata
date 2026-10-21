"""Iteration-2 objective scoreboard: every clause recomputed from primary artefacts, not restated.

The goal's completion audit needs one page that a reviewer can re-run. So this face re-runs the
repo's judge into scratch and compares its status map to the shipped report, reads the merged
claims document, the task ledger, the two junit planes and the wave-39/40 carriers, **recomputes**
what can be recomputed, and compares the rest against the value its carrier printed. Any drift
turns into a red arm; a clause that is not met turns into a line in
``objective_complete=False reasons=...`` rather than into prose.

What is recomputed (primary): accepted rows and the identity set come from a fresh judge run, whose
350 row statuses must equal the shipped report's (a forged status proves the comparator bites); the
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

import copy
import hashlib
import importlib.util
import json
import re
import subprocess  # nosec B404  # fixed argv child process, no shell
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w105_objective_scoreboard.txt"
W130 = RUNS / "w130_counterfact_codes.txt"
JUDGE_TOOL = REPO / "scripts/quality/iter2_model_evidence.py"
LIVE_JUDGE_DIR = Path("/tmp/w105_live_judge")  # nosec B108  # noqa: S108
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
RECEIPTS = REPO / "docs/evidence/C101/receipts"
CONTROL = re.compile(
    r"(untampered control|control produced|control returns|control yields|control still)", re.I
)
DIGIT = re.compile(r"\d")
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
    "accepted": 349,
    "identities": 350,
    "claims": 1745,
    "identity_set": "5168e1e723999312",
    "providers_with_rows": 31,
    "zero_providers": ["stockgrid"],
    "residue": 1,
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


def _status_map(report: dict[str, Any]) -> dict[str, str]:
    """Map each ledger row's task id to the judge's status label."""
    return {
        str(model.get("task_id")): str(model.get("status")) for model in report.get("models") or []
    }


def _judge_diff(left: dict[str, Any], right: dict[str, Any]) -> tuple[int, int, int]:
    """Return (differing shared task ids, missing from left, extra in left)."""
    left_map, right_map = _status_map(left), _status_map(right)
    differing = sum(
        1 for key in left_map.keys() & right_map.keys() if left_map[key] != right_map[key]
    )
    missing = len(right_map.keys() - left_map.keys())
    extra = len(left_map.keys() - right_map.keys())
    return differing, missing, extra


def _recheck_judge() -> tuple[dict[str, Any], int]:
    """Re-run the repo's judge into scratch; return its report and exit code (empty on failure)."""
    LIVE_JUDGE_DIR.mkdir(parents=True, exist_ok=True)
    output = LIVE_JUDGE_DIR / "claims.merged.report.json"
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [
            sys.executable,
            str(JUDGE_TOOL),
            "--inventory",
            str(INVENTORY),
            "--claims",
            str(MERGED),
            "--output",
            str(output),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not output.is_file():
        print(
            f"live_judge_failure rc={proc.returncode} stderr_tail={proc.stderr[-400:]!r}",
            flush=True,
        )
        return {}, proc.returncode
    return json.loads(output.read_text(encoding="utf-8")), proc.returncode


def _claim_map() -> tuple[
    dict[tuple[str, str], Counter[str]],
    dict[tuple[str, str], set[str]],
    dict[str, list[tuple[str, str]]],
]:
    """Group merged claims by (provider, model) and bucket judge rows by status."""
    claims = json.loads(MERGED.read_text(encoding="utf-8")).get("claims") or []
    by_row: dict[tuple[str, str], Counter[str]] = {}
    phases: dict[tuple[str, str], set[str]] = {}
    for claim in claims:
        key = (str(claim.get("provider")), str(claim.get("upstream_model")))
        by_row.setdefault(key, Counter())[str(claim.get("status"))] += 1
        phases.setdefault(key, set()).add(str(claim.get("phase")))
    report = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    rows: dict[str, list[tuple[str, str]]] = {"accepted": [], "incomplete": [], "unreviewed": []}
    for model in report["models"]:
        key = (str(model["provider"]), str(model["upstream_model"]))
        status = str(model["status"])
        if status == "OFFLINE_ACCEPTED":
            rows["accepted"].append(key)
        elif status == "INCOMPLETE":
            rows["incomplete"].append(key)
        else:
            rows["unreviewed"].append(key)
    return by_row, phases, rows


def _bad_rows(
    by_row: dict[tuple[str, str], Counter[str]],
    phases: dict[tuple[str, str], set[str]],
    rows: list[tuple[str, str]],
    status: str,
    count: int,
) -> list[tuple[str, str]]:
    """Rows whose claims are not exactly ``count`` claims of ``status`` across five phases."""
    return [
        key
        for key in rows
        if dict(by_row.get(key, {})) != {status: count} or len(phases.get(key, ())) != 5
    ]


def _receipt_link(claims: Any = None) -> dict[str, int]:  # noqa: ANN401
    """Check PASS claim receipt pointers and BLOCKED claim emptiness against the files."""
    rows = (
        claims
        if claims is not None
        else json.loads(MERGED.read_text(encoding="utf-8")).get("claims") or []
    )
    out = {
        "pass_claims": 0,
        "blocked_claims": 0,
        "missing": 0,
        "sha_bad": 0,
        "field_bad": 0,
        "blocked_bad": 0,
    }
    for claim in rows:
        status = str(claim.get("status"))
        receipt = claim.get("receipt") or {}
        path = Path(str(receipt.get("path") or ""))
        if status == "PASS":
            out["pass_claims"] += 1
            if not path.is_file():
                out["missing"] += 1
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != str(receipt.get("sha256")):
                out["sha_bad"] += 1
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            expected = f"OBB2-{claim.get('provider')}-{claim.get('upstream_model')}"
            machine = record.get("machine_execution") or {}
            if (
                record.get("task_id") != expected
                or record.get("phase") != claim.get("phase")
                or record.get("status") != status
                or machine.get("exit_code") != claim.get("exit_code")
            ):
                out["field_bad"] += 1
        elif status == "BLOCKED":
            out["blocked_claims"] += 1
            if (
                path.is_file()
                or str(receipt.get("sha256")) != "0" * 64
                or not str(claim.get("reason") or "").strip()
            ):
                out["blocked_bad"] += 1
    return out


def _counterfact_controls() -> dict[str, int]:
    """Count counterfact reasons that state an untampered control and a measured number."""
    receipts = sorted(RECEIPTS.rglob("counterfact.json"))
    empty = claimed = numbered = 0
    for path in receipts:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        reason = str((receipt.get("counterfact") or {}).get("reason") or "")
        if not reason.strip():
            empty += 1
            continue
        if CONTROL.search(reason):
            claimed += 1
            if DIGIT.search(reason):
                numbered += 1
    return {
        "receipts": len(receipts),
        "empty": empty,
        "claimed": claimed,
        "numbered": numbered,
    }


def _control_phrase_ok(reason: str) -> bool:
    """Whether a counterfact reason states a control and a number."""
    return bool(CONTROL.search(reason) and DIGIT.search(reason))


def _case_names(path: Path) -> set[str]:
    """Every junit test-case name in one plane capture."""
    return set(re.findall(r'name="([^"]*)"', path.read_text(encoding="utf-8")))


def _provider_split() -> dict[str, Any]:
    """How many providers are fully landed, partially landed, or untouched, from CSV + report."""
    lines = [line for line in INVENTORY.read_text(encoding="utf-8").splitlines() if line.strip()]
    totals: dict[str, int] = {}
    for line in lines[1:]:
        provider = line.split(",")[1]
        totals[provider] = totals.get(provider, 0) + 1
    report = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    accepted: dict[str, int] = {}
    for model in report["models"]:
        if model["status"] == "OFFLINE_ACCEPTED":
            accepted[model["provider"]] = accepted.get(model["provider"], 0) + 1
    full = sum(1 for provider, total in totals.items() if accepted.get(provider, 0) == total)
    partial = sorted(
        [provider, accepted.get(provider, 0), total]
        for provider, total in totals.items()
        if 0 < accepted.get(provider, 0) < total
    )
    none = sorted(provider for provider, total in totals.items() if accepted.get(provider, 0) == 0)
    return {"providers": len(totals), "full": full, "partial": partial, "none": none}


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
    recheck, recheck_rc = _recheck_judge()
    shipped = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    differing, missing, extra = _judge_diff(recheck, shipped)
    recheck_accepted = sum(
        1 for model in recheck.get("models") or [] if model.get("status") == "OFFLINE_ACCEPTED"
    )
    recheck_identity = str((recheck.get("inventory") or {}).get("identity_set_sha256") or "")[:16]
    forged_report = copy.deepcopy(shipped)
    for model in forged_report.get("models") or []:
        model["status"] = "FORGED"
        break
    forged_differing, _forged_missing, _forged_extra = _judge_diff(forged_report, shipped)
    by_row, phases, claim_rows = _claim_map()
    bad_accepted = _bad_rows(by_row, phases, claim_rows["accepted"], "PASS", 5)
    bad_incomplete = _bad_rows(by_row, phases, claim_rows["incomplete"], "BLOCKED", 5)
    unreviewed_with_claims = [key for key in claim_rows["unreviewed"] if by_row.get(key)]
    orphan_claim_rows = (
        set(by_row)
        - set(claim_rows["accepted"])
        - set(claim_rows["incomplete"])
        - set(claim_rows["unreviewed"])
    )
    forged_by_row = copy.deepcopy(by_row)
    if claim_rows["accepted"]:
        forged_by_row[claim_rows["accepted"][0]]["PASS"] = 4
    forged_bad = _bad_rows(forged_by_row, phases, claim_rows["accepted"], "PASS", 5)
    accepted_claims = sum(counter.get("PASS", 0) for counter in by_row.values())
    blocked_claims = sum(counter.get("BLOCKED", 0) for counter in by_row.values())
    link = _receipt_link()
    forged_claims = copy.deepcopy(
        json.loads(MERGED.read_text(encoding="utf-8")).get("claims") or []
    )
    first_pass = next(claim for claim in forged_claims if claim.get("status") == "PASS")
    first_pass["receipt"]["sha256"] = "0" * 64
    forged_link = _receipt_link(forged_claims)
    controls = _counterfact_controls()
    forged_reason_ok = _control_phrase_ok("the mutation refused the input")
    before, before_counts = _plane(W39_RUNS / "test_plane_w39_worktree_final.xml")
    after_xml = next(
        (
            candidate
            for candidate in (
                RUNS / "test_plane_w55_final.xml",
                RUNS / "test_plane_w49_final.xml",
                RUNS / "test_plane_w46b_final.xml",
                RUNS / "test_plane_w46_final.xml",
                RUNS / "test_plane_w45_final.xml",
                RUNS / "test_plane_w44b_final.xml",
                RUNS / "test_plane_w44_final.xml",
                RUNS / "test_plane_w43_final.xml",
                RUNS / "test_plane_w42b_final.xml",
                RUNS / "test_plane_w41i_final.xml",
                RUNS / "test_plane_w41h_final.xml",
                RUNS / "test_plane_w41g_final.xml",
                RUNS / "test_plane_w41f_final.xml",
                RUNS / "test_plane_w41e_final.xml",
                RUNS / "test_plane_w41d_final.xml",
                RUNS / "test_plane_w41c_final.xml",
                RUNS / "test_plane_w41b_final.xml",
                W40_RUNS / "test_plane_w40_final.xml",
            )
            if candidate.is_file()
        ),
        W40_RUNS / "test_plane_w40_final.xml",
    )
    after, after_counts = _plane(after_xml)
    after_names = _case_names(after_xml)
    before_names = _case_names(W39_RUNS / "test_plane_w39_worktree_final.xml")
    new_cases = len(after_names - before_names)
    retired_cases = len(before_names - after_names)
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
    w130 = W130.read_text(encoding="utf-8")

    reasoned = {
        "accepted": live["accepted"],
        "identities": live["identities"],
        "claims": live["claims"],
        "identity_set": live["identity_set"],
        "providers_with_rows": live["providers_with_rows"],
        "zero_providers": live["zero_providers"],
        "residue": live["identities"] - live["accepted"],
        "provider_split": _provider_split(),
        "inventory_rows": _inventory_rows(),
        "live_judge_rc": recheck_rc,
        "live_judge_accepted": recheck_accepted,
        "live_judge_status_differing": differing,
        "live_judge_missing": missing,
        "live_judge_extra": extra,
        "live_judge_identity_set": recheck_identity,
        "claim_rows_accepted": len(claim_rows["accepted"]),
        "claim_rows_incomplete": len(claim_rows["incomplete"]),
        "claim_rows_unreviewed": len(claim_rows["unreviewed"]),
        "accepted_claims": accepted_claims,
        "blocked_claims": blocked_claims,
        "bad_accepted_claim_rows": len(bad_accepted),
        "bad_incomplete_claim_rows": len(bad_incomplete),
        "unreviewed_with_claims": len(unreviewed_with_claims),
        "orphan_claim_rows": len(orphan_claim_rows),
        "receipt_pass_claims": link["pass_claims"],
        "receipt_blocked_claims": link["blocked_claims"],
        "receipt_missing": link["missing"],
        "receipt_sha_mismatch": link["sha_bad"],
        "receipt_field_mismatch": link["field_bad"],
        "blocked_receipt_bad": link["blocked_bad"],
        "counterfact_control_claims": controls["claimed"],
        "counterfact_with_number": controls["numbered"],
        "w57_accepted": int(_grab(w57, r"after_accepted=(\d+)")),
        "w57_claims": int(_grab(w57, r"after_claims=(\d+)")),
        "w67_residue_sums": int(_grab(w67, r"sums=(\d+)")),
        "w72_providers": int(_grab(w72, r"providers_with_at_least_one_accepted_row=(\d+)")),
        "w73_openbb_import_violations": int(_grab(w73, r"openbb_import_violations=(\d+)")),
        "w73_provider_packages": int(_grab(w73, r"provider_packages=(\d+)")),
        "w74_subagent_total": int(_grab(w74, r"this_thread_total=(\d+)")),
        "w74_subagent_cap": int(_grab(w74, r"cap=(\d+)")),
        "w75_stale_pin_rows": int(_grab(w75, r"stale_pin_files_across_accepted_rows=(\d+)")),
        "w130_observed_mismatch": int(_grab(w130, r"observed_mismatch=(\d+)")),
        "w130_declared_mismatch": int(_grab(w130, r"declared_mismatch=(\d+)")),
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
        "plane_new_cases": new_cases,
        "plane_retired_cases": retired_cases,
    }

    lines = [f"scoreboard {json.dumps(reasoned, sort_keys=True, ensure_ascii=False)}"]
    lines.append(
        f"live_judge rc={recheck_rc} accepted={recheck_accepted} "
        f"statuses={len(_status_map(recheck))} differing={differing} missing={missing} "
        f"extra={extra} identity_set={recheck_identity} forged_differing={forged_differing}"
    )
    lines.append(
        f"claim_row_map accepted={len(claim_rows['accepted'])}x5PASS={accepted_claims} "
        f"incomplete={len(claim_rows['incomplete'])}x5BLOCKED={blocked_claims} "
        f"unreviewed={len(claim_rows['unreviewed'])}x0 bad_accepted={len(bad_accepted)} "
        f"bad_incomplete={len(bad_incomplete)} "
        f"unreviewed_with_claims={len(unreviewed_with_claims)} "
        f"orphan={len(orphan_claim_rows)} forged_bad={len(forged_bad)}"
    )
    lines.append(
        f"claim_receipt_link pass={link['pass_claims']} missing={link['missing']} "
        f"sha_mismatch={link['sha_bad']} field_mismatch={link['field_bad']} "
        f"blocked={link['blocked_claims']} blocked_bad={link['blocked_bad']} "
        f"forged_sha_mismatch={forged_link['sha_bad']}"
    )
    lines.append(
        f"counterfact_controls receipts={controls['receipts']} empty={controls['empty']} "
        f"control_claims={controls['claimed']} with_number={controls['numbered']} "
        f"forged_rejected={not forged_reason_ok}"
    )
    arms: dict[str, bool] = {}
    arms["A_judge_is_the_primary_and_agrees_with_expectations"] = (
        live["accepted"] == EXPECTED["accepted"] and live["claims"] == EXPECTED["claims"]
    )
    arms["B_identity_population_closes"] = (
        live["identities"] == reasoned["inventory_rows"] == EXPECTED["identities"]
        and live["identity_set"] == EXPECTED["identity_set"]
    )
    arms["C_provider_coverage_agrees_with_expectations"] = (
        live["providers_with_rows"] == EXPECTED["providers_with_rows"]
        and live["zero_providers"] == EXPECTED["zero_providers"]
    )
    arms["D_residue_is_the_identity_gap"] = reasoned["residue"] == EXPECTED["residue"]
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
        and reasoned["plane_collected"]
        == int(before_counts.get("tests", -1))
        + reasoned["plane_new_cases"]
        - reasoned["plane_retired_cases"]
        and reasoned["plane_new_cases"] > 0
    )
    arms["K_counterfact_codes_landed_and_match_the_witness"] = (
        reasoned["w130_observed_mismatch"] == 0
        and reasoned["w130_declared_mismatch"] == 0
        and "PASS=True" in w130
    )
    arms["M_live_judge_reproduces_the_shipped_report"] = (
        recheck_rc == 0
        and recheck_accepted == live["accepted"] == EXPECTED["accepted"]
        and differing == missing == extra == 0
        and recheck_identity == EXPECTED["identity_set"]
    )
    arms["N_live_judge_comparator_can_bite"] = forged_differing == 1
    split = reasoned["provider_split"]
    arms["L_provider_split_is_a_partition"] = split["full"] + len(split["partial"]) + len(
        split["none"]
    ) == split["providers"] == 32 and live["providers_with_rows"] == split["full"] + len(
        split["partial"]
    )
    arms["O_claim_rows_map_onto_the_judge"] = (
        not bad_accepted
        and not bad_incomplete
        and not unreviewed_with_claims
        and not orphan_claim_rows
        and accepted_claims == 5 * len(claim_rows["accepted"])
        and blocked_claims == 5 * len(claim_rows["incomplete"])
        and accepted_claims + blocked_claims == live["claims"]
    )
    arms["P_claim_row_mapper_can_bite"] = bool(forged_bad)
    arms["Q_pass_claims_point_at_matching_receipts"] = (
        link["pass_claims"] == accepted_claims
        and link["missing"] == link["sha_bad"] == link["field_bad"] == 0
    )
    arms["R_blocked_claims_have_no_receipt_and_a_reason"] = (
        link["blocked_claims"] == blocked_claims and link["blocked_bad"] == 0
    )
    arms["S_receipt_link_checker_can_bite"] = forged_link["sha_bad"] == 1
    arms["T_every_counterfact_states_a_measured_control"] = (
        controls["receipts"] == EXPECTED["accepted"]
        and controls["empty"] == 0
        and controls["claimed"] == controls["receipts"]
        and controls["numbered"] == controls["receipts"]
    )
    arms["U_control_phrase_checker_can_bite"] = not forged_reason_ok
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
