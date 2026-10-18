"""Re-compute this wave's plane-1 face from the files on disk, using the shipped judge.

Every driver in the wave archived its own stdout beside this probe (steps 2c, 2e, 2f, 2g), but
an archived stdout is a report, not a re-computation: it records what the driver printed, not
what the product on disk still means. This instrument imports ``iter2_model_evidence`` -- the
module the shipped ``iter2_model_evidence`` CLI runs -- re-derives every number it publishes from
``docs/evidence/C101`` as those files stand right now, and then falsifies its own reading by
dropping one primary review in a copy. If the accepted count did not move, the count would be a
transcription rather than a computation.

Arms:
* ``A_shards_join_the_merge`` - the merge is exactly the union of the 26 provider shards, and the
  recorder left every verdict the producer wrote intact (per-claim status equality, both roots);
* ``B_identity_set_still_pinned`` - the inventory CSV, the merge document and the judge report
  agree on the pinned 350-identity set digest;
* ``C_judge_recomputes_the_archived_report`` - re-running ``build_report`` in memory yields a
  document equal to the one on disk, including its own ``claims_input`` digest;
* ``D_status_partition`` - the three status buckets partition the 350 rows and the per-model list
  agrees with the published summary;
* ``E_accepted_equals_reviews_equals_prediction`` - accepted rows equal the review count, which
  equals the ``signed_total`` the re-sign driver printed in its own committed carrier;
* ``F_no_pin_claims_equal_the_produced_blocked`` - the claims with no ``probe`` pin equal, in
  count and in population, the ``blocked`` the produce driver tallied in its committed carrier;
* ``G_dropping_a_review_moves_the_count`` - the counterfact: delete one review from a copy and
  show the accepted face lose exactly that row, so arms C-F are live readings, not literals.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
PROBE_DIR = Path(__file__).resolve().parent
RUNS = PROBE_DIR.parent / "runs"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))

import iter2_model_evidence as judge  # noqa: E402  # the shipped judge, not a paraphrase of it

EVIDENCE = REPO_ROOT / "docs/evidence/C101"
MERGED = EVIDENCE / "claims.merged.json"
REPORT = EVIDENCE / "judge/claims.merged.report.json"
RESIGN_CARRIER = RUNS / "wave29-step2f-resign_all_shards.txt"
PRODUCE_CARRIER = RUNS / "wave29-step2c-reproduce_all_shards.txt"
SIGNED_TOTAL_RE = re.compile(r"^resign_face .*?\bsigned_total=(\d+)", re.M)
BLOCKED_RE = re.compile(r"^shard provider=\S+ .*?\bblocked=(\d+)", re.M)


def claim_index(doc: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """Map every claim in a document to its status, keyed by provider, model and phase."""
    return {
        (str(claim["provider"]), str(claim["upstream_model"]), str(claim["phase"])): str(
            claim["status"]
        )
        for claim in doc["claims"]
    }


def review_keys(doc: dict[str, Any]) -> set[tuple[str, str]]:
    """Return the identities a document carries an ACCEPT review for."""
    return {
        (str(review["provider"]), str(review["upstream_model"]))
        for review in doc.get("primary_reviews") or []
        if review.get("decision") == "ACCEPT"
    }


def probe_of(claim: dict[str, Any]) -> list[Any]:
    """The ``probe`` pin list of one claim; empty means the phase never executed."""
    return list((claim.get("pins") or {}).get("probe") or [])


def carrier_total(path: Path, pattern: re.Pattern[str]) -> tuple[int | None, str]:
    """Sum a counter across a committed carrier's own ledger lines, or report it missing."""
    if not path.is_file():
        return None, f"missing carrier {path.name}"
    hits = [int(m.group(1)) for m in pattern.finditer(path.read_text(encoding="utf-8"))]
    if not hits:
        return None, f"no ledger line matching {pattern.pattern!r} in {path.name}"
    return sum(hits), f"lines={len(hits)} sum={sum(hits)}"


def carrier_face_field(path: Path, pattern: re.Pattern[str]) -> tuple[int | None, str]:
    """Read one field off a carrier's own face line; a second hit is a bug in this probe."""
    if not path.is_file():
        return None, f"missing carrier {path.name}"
    hits = [int(m.group(1)) for m in pattern.finditer(path.read_text(encoding="utf-8"))]
    if len(hits) != 1:
        return None, f"want one face match in {path.name}, got {len(hits)}: {hits}"
    return hits[0], f"face_field_hits=1 value={hits[0]}"


def main() -> int:
    """Read the wave's product off disk, re-run the judge over it, and print one face line."""
    # The shipped drivers ran from the repo root and record relative paths in the report, so the
    # re-run has to resolve the same way or arm C compares documents written at different roots.
    os.chdir(REPO_ROOT)
    merged_doc = json.loads(MERGED.read_text(encoding="utf-8"))
    archived = json.loads(REPORT.read_text(encoding="utf-8"))
    shards = sorted(p.parent.name for p in EVIDENCE.glob("*/claims.reviewed.json"))
    shard_claims: dict[tuple[str, str, str], str] = {}
    verdict_moves: list[str] = []
    for provider in shards:
        produced = json.loads((EVIDENCE / provider / "claims.json").read_text(encoding="utf-8"))
        signed = json.loads(
            (EVIDENCE / provider / "claims.reviewed.json").read_text(encoding="utf-8")
        )
        produced_index, signed_index = claim_index(produced), claim_index(signed)
        for key, status in signed_index.items():
            if produced_index.get(key) != status:
                verdict_moves.append(f"{provider}/{key[0]}/{key[1]}/{key[2]}")
            shard_claims[(provider, key[1], key[2])] = status
    merged_claims = claim_index(merged_doc)
    arm_a = shard_claims == merged_claims and not verdict_moves

    inventory_ok = True
    inventory: Any = None
    inventory_error = ""
    try:
        inventory = judge.load_inventory(Path(judge.DEFAULT_INVENTORY))
    except (OSError, ValueError) as exc:  # EvidenceIndexError is a ValueError; printed, not raised
        inventory_ok = False
        inventory_error = str(exc)
    digest_roots = {
        "csv": str((inventory or {}).get("identity_set_sha256", "")),
        "merge": str(merged_doc.get("identity_set_sha256", "")),
        "report": str(archived["inventory"]["identity_set_sha256"]),
        "pinned": judge.EXPECTED_IDENTITY_SHA256,
    }
    arm_b = (
        inventory_ok
        and len({*digest_roots.values()}) == 1
        and len(archived["models"]) == judge.EXPECTED_MODEL_COUNT
    )

    merged_bytes = MERGED.read_bytes()
    rerun = (
        judge.build_report(
            inventory,
            merged_doc,
            claims_sha256=judge.sha256_bytes(merged_bytes),
            claims_path=MERGED.relative_to(REPO_ROOT).as_posix(),
        )
        if inventory_ok
        else None
    )
    summary = archived["summary"]
    arm_c = (
        rerun is not None
        and rerun == archived
        and rerun["claims_input"]["sha256"] == judge.sha256_bytes(merged_bytes)
    )

    recomputed = Counter(str(model["status"]) for model in archived["models"])
    published = {k: int(v) for k, v in summary["status_counts"].items()}
    arm_d = (
        dict(recomputed) == published
        and sum(published.values()) == summary["identity_count"] == judge.EXPECTED_MODEL_COUNT
    )

    accepted = {
        (str(model["provider"]), str(model["upstream_model"]))
        for model in archived["models"]
        if model["status"] == "OFFLINE_ACCEPTED"
    }
    five_pass = {
        (provider, model)
        for provider, model in {key[:2] for key in merged_claims}
        if all(
            merged_claims.get((provider, model, phase)) == "PASS" for phase in judge.REQUIRED_PHASES
        )
    }
    signed_total, signed_reading = carrier_face_field(RESIGN_CARRIER, SIGNED_TOTAL_RE)
    arm_e = (
        accepted == review_keys(merged_doc) == five_pass
        and len(accepted)
        == summary["offline_acceptance_count"]
        == len(merged_doc["primary_reviews"])
        == signed_total
    )

    no_pin = [c for c in merged_doc["claims"] if not probe_of(c)]
    no_pin_blocked = [c for c in no_pin if str(c["status"]) == "BLOCKED"]
    no_pin_exit3 = [c for c in no_pin if int(c.get("exit_code", -1)) == 3]
    blocked_reading = ""
    blocked_total, blocked_state = carrier_total(PRODUCE_CARRIER, BLOCKED_RE)
    if blocked_state.startswith("lines="):
        blocked_reading = f"carrier_blocked_claims={blocked_total}"
    arm_f = (
        blocked_state.startswith("lines=")
        and len(no_pin) == len(no_pin_blocked) == len(no_pin_exit3) == blocked_total
    )

    control = copy.deepcopy(merged_doc)
    victim = sorted(accepted)[0]
    control["primary_reviews"] = [
        review
        for review in control["primary_reviews"]
        if (str(review["provider"]), str(review["upstream_model"])) != victim
    ]
    control_report = (
        judge.build_report(
            inventory,
            control,
            claims_sha256=judge.sha256_bytes(merged_bytes),
            claims_path=MERGED.relative_to(REPO_ROOT).as_posix(),
        )
        if inventory_ok
        else None
    )
    control_status = next(
        (
            str(model["status"])
            for model in (control_report or archived)["models"]
            if (str(model["provider"]), str(model["upstream_model"])) == victim
        ),
        "",
    )
    control_accepted = (control_report or {}).get("summary", {}).get("offline_acceptance_count")
    arm_g = (
        control_report is not None
        and control_accepted == summary["offline_acceptance_count"] - 1
        and control_status != "OFFLINE_ACCEPTED"
    )

    print(
        f"product_roots shards={len(shards)} merged_claims={len(merged_claims)} "
        f"merge_claims_field={len(merged_doc['claims'])} report_models={len(archived['models'])}"
    )
    print(f"digest_roots={digest_roots} inventory_error={inventory_error!r}")
    print(f"verdict_moves={len(verdict_moves)} moves={verdict_moves[:5]}")
    print(f"published_status={published} recomputed_status={dict(recomputed)}")
    print(
        f"accepted={len(accepted)} reviews={len(merged_doc['primary_reviews'])} "
        f"five_pass={len(five_pass)} {signed_reading} signed_total={signed_total}"
    )
    print(
        f"no_pin={len(no_pin)} no_pin_blocked={len(no_pin_blocked)} "
        f"no_pin_exit3={len(no_pin_exit3)} {blocked_reading}"
    )
    print(
        f"control_victim={victim[0]}::{victim[1]} control_accepted={control_accepted} "
        f"control_status={control_status}"
    )
    if rerun is not None:
        invalid = len(rerun["invalid_claims"])
        print(f"rerun_global_issues={rerun['global_issues']} rerun_invalid_claims={invalid}")
    for name, ok in (
        ("A_shards_join_the_merge", arm_a),
        ("B_identity_set_still_pinned", arm_b),
        ("C_judge_recomputes_the_archived_report", arm_c),
        ("D_status_partition", arm_d),
        ("E_accepted_equals_reviews_equals_prediction", arm_e),
        ("F_no_pin_claims_equal_the_produced_blocked", arm_f),
        ("G_dropping_a_review_moves_the_count", arm_g),
    ):
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    passed = sum(1 for ok in (arm_a, arm_b, arm_c, arm_d, arm_e, arm_f, arm_g) if ok)
    print(
        f"verify_merge_judge_face arms=7 arms_passed={passed} accepted={len(accepted)} "
        f"identity_set={str(digest_roots['pinned'])[:16]} verdict="
        f"{'GREEN' if passed == 7 else 'RED'}"
    )
    return 0 if passed == 7 else 1


if __name__ == "__main__":
    sys.exit(main())
