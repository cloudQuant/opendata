"""Re-anchor the census attribution on the committed batch5 pin (HEAD), not interim bytes."""

import json
from collections import Counter
from pathlib import Path

HEAD_PIN = Path("/tmp/batch5_pin_head.json")  # nosec B108  # noqa: S108
FIXTURE = Path("tests/fixtures/registry_batch5_snapshot.json")
HEAD_SHA = "7ce0ca613405e10dc3616e2cd1e6964b957331eb068a20ce49d997927b49c9f8"
import hashlib  # noqa: E402

assert hashlib.sha256(HEAD_PIN.read_bytes()).hexdigest() == HEAD_SHA  # nosec B101  # noqa: S101

head = json.loads(HEAD_PIN.read_text(encoding="utf-8"))
snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
kb = {(r["source"], r["model"], r["domain"]): r for r in head["provider_model_identities"]}
ka = {(r["source"], r["model"], r["domain"]): r for r in snap["provider_model_identities"]}
added = sorted(set(ka) - set(kb))
removed = sorted(set(kb) - set(ka))
fields = ("binding", "scenario_present", "contract", "verified")
moves = {}
for k in sorted(set(kb) & set(ka)):
    changed = {f: [kb[k][f], ka[k][f]] for f in fields if kb[k][f] != ka[k][f]}
    if changed:
        moves[k] = changed

rebase = snap["iter2_census_rebase"]["registry_census_rebase"]
rebase["pin_chain"] = {
    "committed_batch5_pin": {"sha256": HEAD_SHA, "provider_model_identity_count": len(kb)},
    "interim_rebase_pin": {
        "sha256": "294bb5b0396d5bc56e45a935523b237d14cde4caa5b4da2adc3cd13a3d771420",
        "why": "export-census re-base only; identity rows byte-identical on every common key",
    },
    "pin_before_attribution_patch": {
        "sha256": "46487b773140b8da6079c942eb1a438fff7ad53398fda3d88963f7d39d5e0699",
        "why": "descriptor-eligibility field added; identity rows unchanged",
    },
    "live_pin": {
        "provider_model_identity_count": len(ka),
        "sha256_note": "this file's own digest is pinned by BATCH5_SNAPSHOT_SHA256 "
        "after this patch",
    },
}
rebase["identities_added"] = [list(k) for k in added]
rebase["identities_removed"] = [list(k) for k in removed]
rebase["identity_fields_moved"] = len(moves)
rebase["field_shapes_moved"] = sorted({f for c in moves.values() for f in c})
rebase["field_move_counts"] = {f: sum(1 for c in moves.values() if f in c) for f in fields}
rebase["binding_moves_by_source"] = dict(Counter(k[0] for k, c in moves.items() if "binding" in c))
rebase["scenario_moves_by_source"] = dict(
    Counter(k[0] for k, c in moves.items() if "scenario_present" in c)
)
rebase["identity_renames"] = {
    "note": (
        "the three federal_reserve rows below move off the bare domain onto the source-prefixed "
        "iteration-2 domain; they are one rename per row, not three new models"
    ),
    "renamed": [
        {
            "source": a[0],
            "model": a[1],
            "domain_before": r[2],
            "domain_after": a[2],
        }
        for a in added
        for r in removed
        if a[0] == r[0] and a[1] == r[1]
    ],
    "net_identity_change": len(added) - len(removed),
}
rebase["scenario_present_total"] = {
    "batch5_pin": sum(1 for r in head["provider_model_identities"] if r["scenario_present"]),
    "live": sum(1 for r in snap["provider_model_identities"] if r["scenario_present"]),
    "moved_on_existing_identity": rebase["field_move_counts"]["scenario_present"],
    "carried_by_added_identity": sum(1 for k in added if ka[k]["scenario_present"]),
}
rebase["semantic_field_moves"] = {
    "contract": rebase["field_move_counts"]["contract"],
    "verified": rebase["field_move_counts"]["verified"],
    "rule": "both must stay 0; a nonzero value means iteration 2 changed a reviewed "
    "output contract",
}
rebase["approved_delta_conflict"] = {
    "identity": ["fred", "SeniorLoanOfficerSurvey", "senior_loan_officer_survey"],
    "batch5_pin_scenario_present": kb[
        ("fred", "SeniorLoanOfficerSurvey", "senior_loan_officer_survey")
    ]["scenario_present"],
    "live_scenario_present": ka[("fred", "SeniorLoanOfficerSurvey", "senior_loan_officer_survey")][
        "scenario_present"
    ],
    "frozen_record": "approved_delta.scenario_present and "
    "approved_delta.scenario_policy stay byte-frozen",
    "resolution": (
        "recorded, not flipped: the iteration-2 消费用途 backfill wrote a descriptive scenario "
        "string for this binding, while the C65-approved use case is still absent, so verified "
        "stays false and the reviewed prose is untouched"
    ),
    "c65_use_case_approved": False,
}
rebase["counterfact"] = {
    "statement": (
        "every number in this block is a diff of the committed batch5 pin against the live "
        "registry: adding or removing one scenario string moves scenario_present_total.live and "
        "field_move_counts.scenario_present together; registering a new canonical binding moves "
        "identities_added; changing a contract class or a verified flag stops being a census move "
        "and trips semantic_field_moves, which is pinned at zero"
    ),
    "arms": {
        "identity_delta_equals_added_minus_removed": len(ka) - len(kb) == len(added) - len(removed),
        "scenario_total_explained": rebase["scenario_present_total"]["batch5_pin"]
        + rebase["scenario_present_total"]["moved_on_existing_identity"]
        + rebase["scenario_present_total"]["carried_by_added_identity"]
        == rebase["scenario_present_total"]["live"],
        "semantic_moves_zero": rebase["semantic_field_moves"]["contract"] == 0
        and rebase["semantic_field_moves"]["verified"] == 0,
    },
}
assert rebase["counterfact"]["arms"]["identity_delta_equals_added_minus_removed"]  # nosec B101  # noqa: S101
assert rebase["counterfact"]["arms"]["scenario_total_explained"], rebase["scenario_present_total"]  # nosec B101  # noqa: S101
assert rebase["counterfact"]["arms"]["semantic_moves_zero"]  # nosec B101  # noqa: S101

FIXTURE.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(rebase, indent=2, ensure_ascii=False, sort_keys=True))
