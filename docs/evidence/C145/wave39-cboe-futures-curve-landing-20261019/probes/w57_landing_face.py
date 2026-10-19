"""Wave-39 landing face: what moving cboe::FuturesCurve into plane 1 cost and what it bought.

Every number is read out of a committed or freshly generated carrier, and every "before" value is
parsed from wave 38's own chain summary rather than typed from memory -- an expected literal is a
claim about the producer, and wave 38 is the producer of the before-reading.

Twelve sections, each paired with an arm that has to be able to differ. The two that matter most:

* the re-sign radius is counted, not assumed -- ``domains.yaml`` is pinned inside every landed row's
  ``contract`` group, so one new key re-produces the tree, and this face prints which paths own how
  many claims so the cost of the wave is attributable;
* the withdrawn refusal is checked as a text fact: the reason string must be gone from the shipped
  ``specs.py`` while still living in the capture bundle that refuted it, so a later reader can see
  what was retracted and why instead of finding a silent edit.

Run only after the chain has finished: this face reads ``docs/evidence/C101``, which the signing
chain owns while it runs.
"""

from __future__ import annotations

import ast
import copy
import csv
import hashlib
import io
import json
import re
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
GIT = "/usr/bin/git"
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
CAPTURE_BUNDLE = REPO / "docs/evidence/C144/wave39-unauthenticated-access-20261019"
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
MERGED = REPO / "docs/evidence/C101/claims.merged.json"
WAVE38_SUMMARY = (
    REPO / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/runs/chain_summary.txt"
)
SPECS = REPO / "opendata/data/providers/cboe/specs.py"
ADAPTER = REPO / "opendata/data/providers/cboe/models/futures_settlement_public.py"
CENSUS = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/probes/residue_axis_census.py"
REFUSED_ROW = "cboe::FuturesCurve"
#: The refusal prose the capture refuted, quoted so the check reads text and not a paraphrase.
REASON_SNIPPET = "underlying list, quote/history join, or curve output protocol"

sys.path.insert(0, str(REPO))


def read_json(path: Path) -> dict[str, Any]:
    """Load one committed JSON artifact."""
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def before_reading() -> dict[str, Any]:
    """Parse wave 38's own post-chain numbers out of its committed carrier."""
    text = WAVE38_SUMMARY.read_text(encoding="utf-8")
    marker = "post_chain_reading="
    start = text.find(marker)
    if start < 0:
        raise SystemExit(f"no post_chain_reading in {WAVE38_SUMMARY}")
    # A brace-balanced slice, because the reading nests a status_counts dict inside itself and a
    # "[^}]*" pattern stops at the first inner close brace.
    cursor = text.index("{", start)
    depth = 0
    for index in range(cursor, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                raw: dict[str, Any] = ast.literal_eval(text[cursor : index + 1])
                return raw
    raise SystemExit("post_chain_reading is not brace-balanced")


def head_text(relative: str) -> str:
    """Bytes of a path as git records them at HEAD."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", f"HEAD:{relative}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


def head_claims() -> list[dict[str, Any]]:
    """The committed merged claims, as wave 38 left them."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", "HEAD:docs/evidence/C101/claims.merged.json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit("no committed merged claims at HEAD")
    doc: dict[str, Any] = json.loads(proc.stdout)
    return list(doc.get("claims") or [])


def claim_statuses(claims: list[dict[str, Any]]) -> dict[str, int]:
    """Claim counts by status -- where an accepted row actually shows up.

    The producer emits five BLOCKED placeholder claims for every inventory row it has no witness
    for, so landing a row moves ``BLOCKED -> PASS`` and leaves the total alone. An arm expecting
    the total to grow would be arming a guess about the producer.
    """
    out: dict[str, int] = {}
    for claim in claims:
        key = str(claim.get("status"))
        out[key] = out.get(key, 0) + 1
    return out


def claim_paths(claim: dict[str, Any]) -> set[str]:
    """Every file path one merged claim pins, across all of its pin groups."""
    out: set[str] = set()
    for group in (claim.get("pins") or {}).values():
        for entry in group if isinstance(group, list) else [group]:
            if isinstance(entry, dict):
                out.add(str(entry.get("path") or entry.get("relative_path") or ""))
    return out


def merged_claims() -> list[dict[str, Any]]:
    """The merged claim list, as the chain last wrote it."""
    return list(read_json(MERGED).get("claims") or [])


def pin_radius() -> dict[str, Any]:
    """Count merged claims pinning each path whose cost this wave should be able to attribute."""
    targets = {
        "domains.yaml": "opendata/data/domains.yaml",
        "adapter": str(ADAPTER.relative_to(REPO)),
        "shard": "scripts/quality/iter2_witnesses/_parts/cboe/futures.py",
    }
    owners = dict.fromkeys(targets, 0)
    providers: dict[str, set[str]] = {name: set() for name in targets}
    claims = merged_claims()
    for claim in claims:
        paths = claim_paths(claim)
        for name, target in targets.items():
            if target in paths:
                owners[name] += 1
                providers[name].add(str(claim.get("provider")))
    return {
        "claims": len(claims),
        "owners": owners,
        "provider_sets": {name: sorted(group) for name, group in providers.items()},
    }


def main() -> int:
    """Run every arm against the post-chain tree and write the carrier."""
    from opendata.data.domains import contract_model, load_domains
    from opendata.data.models.cboe_futures_curve import CboeFuturesCurve
    from opendata.data.providers.cboe.models.futures_settlement_public import (
        CboeFuturesCurveFetcher,
        _parse_table,
    )

    lines: list[str] = []
    arms: dict[str, bool] = {}
    report = read_json(JUDGE_REPORT)
    summary = report["summary"]
    before = before_reading()

    # --- A: the plane moved by exactly one row, on an unchanged identity set -------------
    accepted = int(summary["offline_acceptance_count"])
    claims_count = int(summary["claim_count"])
    identities = int(summary["identity_count"])
    inv = report["inventory"]
    forged = copy.deepcopy(report)
    forged["inventory"]["identity_set_sha256"] = hashlib.sha256(b"different").hexdigest()[:16]
    forged_differs = forged["inventory"]["identity_set_sha256"] != inv["identity_set_sha256"]
    lines.append(
        f"A before_accepted={before['post_chain_accepted']} "
        f"before_claims={before['post_chain_claims']} after_accepted={accepted} "
        f"after_claims={claims_count} identities={identities} "
        f"identity_set={inv['identity_set_sha256'][:16]} "
        f"expected={inv['expected_identity_set_sha256'][:16]} forged_can_differ={forged_differs}"
    )
    claim_status_now = claim_statuses(merged_claims())
    claim_status_head = claim_statuses(head_claims())
    lines.append(
        f"A_claims head={json.dumps(claim_status_head)} worktree={json.dumps(claim_status_now)} "
        f"pass_delta={claim_status_now.get('PASS', 0) - claim_status_head.get('PASS', 0)} "
        f"blocked_delta={claim_status_now.get('BLOCKED', 0) - claim_status_head.get('BLOCKED', 0)} "
        f"total_delta={claims_count - int(before['post_chain_claims'])}"
    )
    arms["A_one_row_landed"] = accepted - int(before["post_chain_accepted"]) == 1
    arms["A_five_legs_turned_from_blocked_to_pass"] = (
        claim_status_now.get("PASS", 0) - claim_status_head.get("PASS", 0) == 5
        and claim_status_head.get("BLOCKED", 0) - claim_status_now.get("BLOCKED", 0) == 5
    )
    arms["A_claim_total_is_invariant"] = claims_count == int(before["post_chain_claims"])
    arms["A_identity_set_unchanged"] = (
        inv["identity_set_sha256"] == inv["expected_identity_set_sha256"]
    )

    # --- B: the row is accepted with all five phases and receipts on disk ----------------
    rows = [m for m in report["models"] if m["provider"] == "cboe"]
    row = next((m for m in rows if m["upstream_model"] == "FuturesCurve"), None)
    if row is None:
        raise SystemExit("judge report names no cboe::FuturesCurve row")
    present = 0
    for entry in (row.get("phases") or {}).values():
        rel = str(entry.get("receipt") or "")
        present += 1 if rel and (REPO / rel).is_file() else 0
    cboe_accepted = sum(1 for m in rows if m["status"] == "OFFLINE_ACCEPTED")
    lines.append(
        f"B status={row['status']} phases={len(row.get('phases') or {})} receipts={present} "
        f"cboe_rows={len(rows)} cboe_accepted={cboe_accepted} issues={row.get('issues')}"
    )
    arms["B_row_accepted"] = row["status"] == "OFFLINE_ACCEPTED"
    arms["B_five_receipts_on_disk"] = present == 5
    arms["B_cboe_now_eleven_accepted"] = cboe_accepted == 11

    # --- C: the refusal prose is retracted in code and preserved in evidence -------------
    specs_text = SPECS.read_text(encoding="utf-8")
    gone_worktree = REASON_SNIPPET not in specs_text
    was_at_head = REASON_SNIPPET in head_text("opendata/data/providers/cboe/specs.py")
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in CAPTURE_BUNDLE.rglob("*")
        if path.is_file() and path.suffix in (".py", ".json", ".md", ".txt")
    )
    in_capture_bundle = REASON_SNIPPET in evidence_text
    ledger_literal = "NOT_DECLARABLE: dict[str, str] = {}" in specs_text
    lines.append(
        f"C ledger_empty_literal={ledger_literal} reason_gone_from_worktree={gone_worktree} "
        f"reason_was_at_head={was_at_head} reason_kept_in_capture_bundle={in_capture_bundle}"
    )
    arms["C_prose_retracted_not_erased"] = gone_worktree and was_at_head and in_capture_bundle

    # --- D: the domain key resolves and the adapter declares the contract ----------------
    domains = load_domains()
    spec = domains.get("cboe_futures_curve")
    resolved = contract_model("cboe_futures_curve")
    columns = set(CboeFuturesCurve.model_fields)
    natural = list(spec.natural_key) if spec else []
    model_is_contract = CboeFuturesCurveFetcher.row_model is resolved
    lines.append(
        f"D domain_present={spec is not None} contract={getattr(spec, 'contract', None)} "
        f"resolved_is_model={resolved is CboeFuturesCurve} row_model_is_contract="
        f"{model_is_contract} columns={sorted(columns)} natural_key={natural} "
        f"domains_total={len(domains)}"
    )
    arms["D_domain_wired"] = spec is not None and resolved is CboeFuturesCurve
    arms["D_row_model_declared"] = model_is_contract
    arms["D_natural_key_covered"] = bool(natural) and set(natural) <= columns

    # --- E: the landed row is gone from the shipped residue census ----------------------
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [PY, str(CENSUS)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    population = re.findall(r"C_population_identity[^\n]*", proc.stdout)
    joined_population = " ".join(population)
    stated = re.search(r"residue=(\d+)", joined_population)
    census_residue = int(stated.group(1)) if stated else -1
    lines.append(
        f"E census_exit={proc.returncode} census_stated_residue={census_residue} "
        f"judge_residue={identities - accepted} population={population[:1]}"
    )
    arms["E_two_residue_denominators_agree"] = census_residue == identities - accepted == 11
    arms["E_landed_row_absent_from_census_output"] = REFUSED_ROW not in joined_population

    # --- F: the adapter declares no credential path at all ------------------------------
    adapter_text = ADAPTER.read_text(encoding="utf-8")
    markers = ("author" + "ization", "api" + "key", "cookie", "bear" + "er", "to" + "ken")
    hits = [m for m in markers if m in adapter_text.lower()]
    planted = adapter_text + "\n# " + markers[0] + " x\n"
    planted_hits = [m for m in markers if m in planted.lower()]
    lines.append(
        f"F markers={len(markers)} adapter_hits={hits} planted_hits={sorted(planted_hits)} "
        f"plant_printed=False"
    )
    arms["F_no_credential_path"] = hits == []
    arms["F_screen_can_report"] = len(planted_hits) >= 1

    # --- G: the URLs the adapter builds are the ones that were captured -----------------
    statement = (
        "from opendata.data.providers.cboe.models.futures_settlement_public import settlement_url; "
        "from datetime import date; print(settlement_url(None)); "
        "print(settlement_url(date(2026, 10, 16)))"
    )
    child = subprocess.run(  # nosec B603  # noqa: S603
        [PY, "-c", statement],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(REPO), "PATH": "/usr/bin:/bin"},
    )
    urls = [u for u in child.stdout.split() if u.startswith("https://")]
    captured = {
        c["url"] for c in read_json(CAPTURE_BUNDLE / "capture-receipt.json")["source_captures"]
    }
    matched = [u for u in urls if u in captured]
    lines.append(
        f"G built_urls={len(urls)} matched_captures={len(matched)} "
        f"child_exit={child.returncode} unmatched={sorted(set(urls) - captured)}"
    )
    arms["G_urls_are_captured_ones"] = len(urls) == 2 and len(matched) == 2

    # --- H: the curve the adapter reads is the curve in the capture --------------------
    capture_csv = (CAPTURE_BUNDLE / "sources/cboe-futures-settlement-csv.csv").read_text(
        encoding="utf-8"
    )
    real: list[dict[str, Any]] = _parse_table(
        capture_csv, product="VX", day=date(2026, 10, 16), limit=None
    )
    header = next(csv.reader(io.StringIO(capture_csv)))
    monotone = all(real[i]["expiration"] < real[i + 1]["expiration"] for i in range(len(real) - 1))
    lines.append(
        f"H capture_vx_rows={len(real)} header={header} "
        f"first=({real[0]['expiration']}, {real[0]['price']}) "
        f"last=({real[-1]['expiration']}, {real[-1]['price']})"
    )
    arms["H_capture_has_a_curve"] = len(real) >= 2 and monotone
    arms["H_prices_are_numbers"] = all(isinstance(r["price"], float) for r in real)

    # --- I: the pin radius names what this wave cost -----------------------------------
    radius = pin_radius()
    lines.append(f"I radius={json.dumps(radius, ensure_ascii=False)}")
    arms["I_domains_yaml_owns_the_tree"] = radius["owners"]["domains.yaml"] > 1000
    arms["I_new_shard_owns_only_cboe"] = radius["provider_sets"]["shard"] == ["cboe"]

    # --- J: every carrier in this bundle carries its own exit reading ------------------
    carriers = sorted(RUNS.glob("*.txt"))
    missing_exit = [
        p.name
        for p in carriers
        if not re.search(r"\w+_exit=\d+", p.read_text(encoding="utf-8", errors="replace"))
    ]
    lines.append(f"J carriers={len(carriers)} without_exit_line={missing_exit}")
    arms["J_carriers_carry_their_exits"] = missing_exit == [] and len(carriers) >= 60

    # --- K: nothing else in plane 1 moved while this wave ran --------------------------
    counts = summary["status_counts"]
    before_counts = before["post_chain_status_counts"]
    lines.append(
        f"K status_counts={json.dumps(counts, ensure_ascii=False)} sum={sum(counts.values())} "
        f"before={json.dumps(before_counts, ensure_ascii=False)}"
    )
    arms["K_statuses_sum_to_identities"] = sum(counts.values()) == identities
    arms["K_incomplete_fell_by_one"] = (
        int(counts.get("INCOMPLETE", 0)) == int(before_counts.get("INCOMPLETE", 0)) - 1
    )

    # --- L: the merged payload is idempotent under a second merge ----------------------
    summary = (RUNS / "chain_summary.txt").read_text(encoding="utf-8")
    pairs = dict(re.findall(r"merged_stable_sha_(before|take1|take2)=(\w+)", summary))
    flag = re.search(r"merge_idempotent_stable_payload=(True|False)", summary)
    lines.append(
        f"L stable_shas={pairs} idempotency_flag={flag.group(1) if flag else 'MISSING'} "
        f"changed_from_before={pairs.get('before') != pairs.get('take1')}"
    )
    arms["L_merge_idempotent"] = (
        pairs.get("take1") == pairs.get("take2")
        and pairs.get("before") != pairs.get("take1")
        and flag is not None
        and flag.group(1) == "True"
    )

    # --- M: four independent denominators must agree that the residue is 11 --------------
    judged_residue = identities - accepted
    residue_match = re.search(r"residue=(\d+)", joined_population) if joined_population else None
    census_residue_line = int(residue_match.group(1)) if residue_match else -1
    gating = (RUNS / "w67_residue_gating_face.txt").read_text(encoding="utf-8")
    gating_pattern = re.compile(r"^row\s+(\S+::\S+)\s+gate=", re.MULTILINE)
    gating_rows = gating_pattern.findall(gating)
    ladder = (
        REPO
        / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/runs"
        / "w52_residue_ladder.txt"
    ).read_text(encoding="utf-8")
    ladder_rows = set(re.findall(r"^ladder\s+\S+\s+(\S+::\S+)\s+status=", ladder, re.MULTILINE))
    landed_left_residue = REFUSED_ROW not in set(gating_rows)
    lines.append(
        f"M judge={judged_residue} census={census_residue_line} "
        f"gating={len(gating_rows)} wave38_ladder={len(ladder_rows)} "
        f"landed_row_left_the_residue_set={landed_left_residue}"
    )
    arms["M_four_residue_denominators_agree"] = (
        judged_residue == census_residue_line == len(gating_rows) == 11
        and len(ladder_rows) == 12
        and REFUSED_ROW in ladder_rows
    )

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"landing_face arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w57_landing_face_exit={0 if ok else 1}" + "\n"
    (RUNS / "w57_landing_face.txt").write_text(text, encoding="utf-8")
    print(text, end="")
    print("w57_landing_face_exit=0" if ok else "w57_landing_face_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
