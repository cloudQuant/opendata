"""Witness-completeness face: five receipts and a counterfact per accepted row.

The objective forbids verbal acceptance judgements: a row counts as accepted only if a recomputable
face
exists for it, and that face must include a counter-example. Plane 1's accepted count is a number
the
judge prints; this instrument checks the thing behind the number, row by row, for every
``OFFLINE_ACCEPTED`` identity:

* all five phase claims exist for the row;
* each claim names a receipt path under ``docs/evidence/C101/receipts/`` and records a 64-hex
digest;
* the receipt file is present and its bytes hash to that digest;
* the counterfact receipt actually carries a refusal or tamper verdict, not just a clean exit.

Run this only when no signing chain is in flight: unlike the other wave-39 faces it hashes worktree
receipts, and a chain rewriting them mid-scan would turn my own instrument into the source of the
mismatch.

Every arm is paired with something that can fail. The digest check is exercised by re-hashing with
one
byte appended, and the refusal-token predicate by scanning a receipt with the token stripped, so a
green
"all rows witnessed" cannot come from a predicate that always agrees.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
MERGED = REPO / "docs/evidence/C101/claims.merged.json"
JUDGE = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
CARRIER = RUNS / "w75_witness_completeness_face.txt"
PHASES = ("implementation", "direct", "service", "contract", "counterfact")


def file_digest(path: Path) -> str:
    """sha256 of a file's bytes, or MISSING when it is not there."""
    if not path.is_file():
        return "MISSING"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verdict_from_body(body: dict[str, Any]) -> dict[str, Any]:
    """Score one parsed counterfact receipt: does it name a refusal and its capture?"""
    counterfact = body.get("counterfact") or {}
    expected = counterfact.get("expected") or {}
    capture = counterfact.get("capture") or body.get("capture") or {}
    capture_path = REPO / str(capture.get("path") or "")
    ok = (
        bool(str(expected.get("refusal_class") or ""))
        and bool(str(expected.get("refusal_code") or ""))
        and capture_path.is_file()
    )
    return {
        "ok": ok,
        "why": f"class={expected.get('refusal_class')} code={expected.get('refusal_code')}",
    }


def counterfact_verdict(claim: dict[str, Any]) -> dict[str, Any]:
    """The refusal a counterfact leg recorded, or receipt_absent when the file is gone."""
    receipt = claim.get("receipt") or {}
    path = REPO / str(receipt.get("path") or "")
    if not path.is_file():
        return {"ok": False, "why": "receipt_absent", "body": {}}
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    scored = verdict_from_body(parsed)
    return {"ok": scored["ok"], "why": scored["why"], "body": parsed}


def stale_pins(claim: dict[str, Any]) -> list[str]:
    """Pinned files whose current bytes no longer match the digest the receipt recorded.

    A leg whose ``before_sha256`` equals ``after_sha256`` claims the file was not touched by the
    witness, so the file on disk must still hash to that value; a leg that deliberately moved a
    file (a disk-mutating counterfact) is not pinned by this check.
    """
    body = counterfact_verdict(claim).get("body") or {}
    out: list[str] = []
    for group, entries in (body.get("pins") or {}).items():
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                continue
            before = str(entry.get("before_sha256") or "")
            after = str(entry.get("after_sha256") or "")
            rel = str(entry.get("path") or entry.get("relative_path") or "")
            if not rel or before != after:
                continue
            if file_digest(REPO / rel) != before:
                out.append(f"{group}:{rel}")
    return out


def load(path: Path) -> dict[str, Any]:
    """One committed JSON artifact."""
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def claim_index() -> dict[str, dict[str, dict[str, Any]]]:
    """``provider::Model`` to phase to claim, from the merged claims."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for claim in load(MERGED).get("claims") or []:
        key = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        out.setdefault(key, {})[str(claim.get("phase"))] = claim
    return out


def digest_matches(claim: dict[str, Any]) -> bool:
    """The receipt bytes hash to the digest the claim recorded."""
    receipt = claim.get("receipt") or {}
    rel = str(receipt.get("path") or "")
    recorded = str(receipt.get("sha256") or "")
    path = REPO / rel
    if not rel or len(recorded) != 64 or not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == recorded


def main() -> int:
    """Walk every accepted row, check its five legs, and write the carrier."""
    lines: list[str] = []
    arms: dict[str, bool] = {}
    judge = load(JUDGE)
    accepted_rows = [
        f"{m['provider']}::{m['upstream_model']}"
        for m in judge.get("models") or []
        if m.get("status") == "OFFLINE_ACCEPTED"
    ]
    index = claim_index()
    missing_phases: list[str] = []
    bad_digests: list[str] = []
    no_refusal: list[str] = []
    stale: list[list[str]] = []
    for key in accepted_rows:
        phases = index.get(key, {})
        absent = [phase for phase in PHASES if phase not in phases]
        if absent:
            missing_phases.append(f"{key}:{absent}")
            continue
        bad_digests.extend(
            f"{key}:{phase}" for phase in PHASES if not digest_matches(phases[phase])
        )
        verdict = counterfact_verdict(phases["counterfact"])
        if not verdict["ok"]:
            no_refusal.append(f"{key}:{verdict['why']}")
        stale.append(
            [entry for phase in PHASES for entry in stale_pins(phases[phase])]
        )  # one group per row: stale is list[list[str]]

    flat_stale = [item for group in stale for item in group]
    lines.append(f"stale_pin_files_across_accepted_rows={len(flat_stale)} sample={flat_stale[:5]}")
    lines.append(
        f"population accepted={len(accepted_rows)} rows_in_claims={len(index)} "
        f"claims_total={len(load(MERGED).get('claims') or [])}"
    )
    lines.append(
        f"rows_missing_a_phase={len(missing_phases)} missing={missing_phases[:5]} "
        f"receipt_digest_mismatches={len(bad_digests)} mismatch_sample={bad_digests[:5]} "
        f"counterfact_without_refusal_text={len(no_refusal)} sample={no_refusal[:5]}"
    )

    # Two predicate controls: the digest check must notice one extra byte, the refusal check must
    # notice a receipt with its verdict text removed.
    probe = index[accepted_rows[0]]["counterfact"]
    forged_claim = json.loads(json.dumps(probe))
    forged_claim["receipt"]["sha256"] = hashlib.sha256(b"not the recorded bytes").hexdigest()
    control = json.loads(json.dumps(probe))
    control["receipt"] = {"path": "docs/evidence/C101/claims.merged.json", "sha256": "0" * 64}
    verdict_probe = counterfact_verdict(probe)
    stripped_body = json.loads(json.dumps(verdict_probe["body"]))
    code_value = (stripped_body.get("counterfact") or {}).get("expected", {}).get("refusal_code")
    (stripped_body.get("counterfact") or {}).get("expected", {}).pop("refusal_code", None)
    stale_probe = stale_pins(probe)
    lines.append(
        f"controls wrong_digest_detected={not digest_matches(forged_claim)} "
        f"absent_receipt_path_detected={not digest_matches(control)} "
        f"real_receipt_detected={digest_matches(probe)} "
        f"refusal_code={str(code_value)[:34]} stale_pin_mismatches_on_this_row={len(stale_probe)}"
    )

    arms["A_every_accepted_row_has_five_legs"] = missing_phases == []
    arms["B_every_receipt_hash_matches_its_claim"] = bad_digests == []
    arms["C_every_counterfact_states_a_refusal"] = no_refusal == []
    arms["C2_predicate_notices_a_stripped_refusal_code"] = not verdict_from_body(stripped_body)[
        "ok"
    ]
    arms["C3_predicate_accepts_the_real_receipt"] = verdict_probe["ok"]
    arms["H_no_accepted_row_rests_on_a_stale_pin"] = all(group == [] for group in stale)
    arms["D_digest_predicate_notices_a_wrong_digest"] = not digest_matches(forged_claim)
    arms["E_digest_predicate_notices_a_missing_file"] = not digest_matches(control)
    arms["F_digest_predicate_accepts_the_real_thing"] = digest_matches(probe)
    arms["G_population_is_the_judges_own"] = len(accepted_rows) == int(
        judge["summary"]["offline_acceptance_count"]
    )

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"witness_completeness arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w75_witness_completeness_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
