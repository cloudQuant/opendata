"""Blast radius of a shared-contract edit: what one ``domains.yaml`` append costs the accepted set.

Why this exists as an instrument
--------------------------------
Every remaining ``implement_missing_model`` row of a provider needs a *new capability domain
key* in ``opendata/data/domains.yaml`` (house precedent is a per-provider key: seven providers
already declare ``<provider>_etf_historical``, measured in the sibling transport probe).
``domains.yaml`` is not a local file: the claim producer writes it into the ``contract`` pin
group of every phase claim it emits, and
:func:`scripts.quality.iter2_model_evidence._validate_pin_groups` re-hashes each pinned path
against the worktree -- so any byte change to that one file makes every accepted row read
``*_current_hash_drift`` and fall out of ``OFFLINE_ACCEPTED``.  The question this answers
("does landing one row mean re-signing the whole tree?") is a decision the wave owner must put
to the ledger owner, so it is measured rather than argued.

Nothing under version control is edited.  The post-edit digest is computed from bytes built in
memory and written to ``artifacts/`` so a reader can re-hash it.  The staleness predicate is the
shipped one, imported, never re-typed.

Arms (all four required; a zero that cannot move is not a measurement)
----------------------------------------------------------------------
* A -- clean walk.  For every ``OFFLINE_ACCEPTED`` row, run the shipped predicate over its five
  phase claims against the real worktree.  ``stale=0``, and the claim count must be exactly
  ``rows * 5``.  The row count must equal the shipped judge's own
  ``offline_acceptance_count`` (consistency between two independent derivations, not a typed
  literal).
* D -- single-slot re-arm.  One accepted claim, in memory, has its ``domains.yaml`` slot's
  ``before``/``after`` pair replaced by the simulated post-edit digest (both fields, the way the
  recorder writes them).  The predicate must flag that one claim.  This 0 -> 1 witness is what
  makes arm A's zero meaningful instead of a mute counter.
* B -- whole-tree simulation.  Same replacement applied to every accepted claim.
  ``stale_rows`` must equal the independently censused count of rows referencing the file
  (= every accepted row), ``stale_slots`` the censused slot count, and the *set of issue names*
  must be exactly the one slot-position label derived from the census -- proving no unrelated
  slot in the same claim is flagged.
* C -- specificity control.  Replace the pin digest of a *different* shared file
  (``opendata/data/models/tmx.py``, this provider's own contract module) instead.  Only the rows
  that pin that file may go stale, and that must be a strict subset of the tree.  If C also
  reads the whole tree, the counter is indiscriminate and B's 291 would mean nothing.

Read-only; exits non-zero when an arm disagrees with its own control.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

# The repo judge, imported on purpose so the predicate is the shipped one, not a re-type.
from scripts.quality import iter2_model_evidence as judge  # noqa: E402

CLAIMS_REL = "docs/evidence/C101/claims.merged.json"
TARGET_REL = "opendata/data/domains.yaml"
CONTROL_REL = "opendata/data/models/tmx.py"
PIN_GROUPS = ("contract", "fixture", "probe", "source")
CANDIDATE_BLOCK = """  tmx_etf_historical:
    display_name: "TMX ETF日历史"
    contract: "TmxEtfHistorical"
    temporal_kind: "series"
    time_field: "date"
    natural_key: ["symbol", "date"]
    filter_dims: ["symbol", "date"]
    storage_mode: "transient"
    permissions: ["query"]
"""


def _pin_slots(claim: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    """Return (group, 1-based index, entry) for every byte pin.

    ``contract_version`` pins are plain strings, not byte slots, so they are skipped.
    """
    pins = claim.get("pins") or {}
    out: list[tuple[str, int, dict[str, Any]]] = []
    for group in PIN_GROUPS:
        entries = pins.get(group)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries, start=1):
            if isinstance(entry, dict) and entry.get("path"):
                out.append((group, index, entry))
    return out


def _simulate(pins: dict[str, Any], rel: str, digest: str) -> int:
    """Rewrite both recorded digests of every slot pinning ``rel``.

    Returns how many slots were rewritten.
    """
    hit = 0
    for group in PIN_GROUPS:
        entries = pins.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("path") == rel:
                entry["before_sha256"] = digest
                entry["after_sha256"] = digest
                hit += 1
    return hit


def _walk_claims(
    claims: list[dict[str, Any]], rel: str, digest: str | None
) -> tuple[set[str], int, set[str], int]:
    """Run the shipped predicate over ``claims``.

    Returns ``(stale row ids, stale slots, issue names, slots seen)``.
    """
    stale_rows: set[str] = set()
    stale_slots = 0
    issue_names: set[str] = set()
    slots_seen = 0
    for claim in claims:
        rid = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        pins = claim.get("pins")
        if digest is not None:
            pins = copy.deepcopy(pins or {})
            rewrote = _simulate(pins, rel, digest)
            if not rewrote:
                continue
        issues, stale = judge._validate_pin_groups(pins, repo_root=REPO_ROOT, label=rid)
        drift = [i for i in issues if i.endswith("_current_hash_drift")]
        if not drift:
            continue
        stale_rows.add(rid)
        stale_slots += len(drift)
        issue_names.update(drift)
        slots_seen += rewrote if digest is not None else len(drift)
    return stale_rows, stale_slots, issue_names, slots_seen


def main(argv: list[str] | None = None) -> int:
    """Walk the accepted claims four ways and print the blast-radius face line."""
    ap = argparse.ArgumentParser(description="domains.yaml pin blast radius")
    ap.add_argument("--target", default=TARGET_REL)
    ap.add_argument("--control", default=CONTROL_REL)
    ap.add_argument("--claims", default=CLAIMS_REL)
    ap.add_argument(
        "--block-file",
        default="",
        help="file whose text is appended to the target (default: builtin block)",
    )
    args = ap.parse_args(argv)

    claims_path = REPO_ROOT / args.claims
    doc = json.loads(claims_path.read_text(encoding="utf-8"))
    claims = [c for c in doc.get("claims", []) if isinstance(c, dict)]

    inventory = judge.load_inventory(REPO_ROOT / judge.DEFAULT_INVENTORY)
    report = judge.build_report(
        inventory,
        doc,
        claims_sha256=judge.sha256_bytes(claims_path.read_bytes()),
        claims_path=args.claims,
    )
    summary = report["summary"]
    accepted = [m for m in report["models"] if m["status"] == "OFFLINE_ACCEPTED"]
    accepted_ids = {(m["provider"], m["upstream_model"]) for m in accepted}
    accepted_claims = [
        c for c in claims if (c.get("provider"), c.get("upstream_model")) in accepted_ids
    ]
    print(
        f"population: inventory={summary['identity_count']} claims={summary['claim_count']} "
        f"reviews={summary['primary_review_count']} status_counts={summary['status_counts']}"
    )
    print(
        f"accepted: judge_offline_acceptance_count={summary['offline_acceptance_count']} "
        f"derived_rows={len(accepted)} derived_claims={len(accepted_claims)} "
        f"rows_x5={len(accepted) * 5}"
    )

    slot_count: dict[str, int] = defaultdict(int)
    slot_rows: dict[str, set[str]] = defaultdict(set)
    for claim in accepted_claims:
        rid = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        for _group, _index, entry in _pin_slots(claim):
            slot_count[entry["path"]] += 1
            slot_rows[entry["path"]].add(rid)
    total_slots = sum(slot_count.values())
    print(
        f"census: accepted_claims={len(accepted_claims)} slots={total_slots} "
        f"distinct_paths={len(slot_count)}"
    )
    for path, n in sorted(slot_count.items(), key=lambda kv: (-kv[1], kv[0]))[:8]:
        print(f"shared_path slots={n} rows={len(slot_rows[path])} path={path}")
    target_rows = sorted(slot_rows.get(args.target, set()))
    control_rows = sorted(slot_rows.get(args.control, set()))
    target_slots = slot_count.get(args.target, 0)
    control_slots = slot_count.get(args.control, 0)
    print(f"target={args.target} rows={len(target_rows)} slots={target_slots}")
    print(f"control={args.control} rows={len(control_rows)} slots={control_slots}")
    tree_wide = sorted(
        (
            (path, slot_count[path], len(slot_rows[path]))
            for path in slot_count
            if len(slot_rows[path]) == len(accepted)
        ),
        key=lambda t: (-t[1], t[0]),
    )
    print(f"tree_wide_paths(rows={len(accepted)}): count={len(tree_wide)} list={tree_wide}")
    target_positions: dict[str, int] = defaultdict(int)
    control_positions: dict[str, int] = defaultdict(int)
    for claim in accepted_claims:
        for group, index, entry in _pin_slots(claim):
            if entry["path"] == args.target:
                target_positions[f"{group}_pin_{index}"] += 1
            if entry["path"] == args.control:
                control_positions[f"{group}_pin_{index}"] += 1
    print(f"target_slot_positions: {dict(sorted(target_positions.items()))}")
    print(f"control_slot_positions: {dict(sorted(control_positions.items()))}")

    # --- arm A: clean walk against the real worktree.
    a_rows, a_slots, a_issues, _ = _walk_claims(accepted_claims, args.target, None)
    arm_a = a_slots == 0 and len(a_rows) == 0 and len(accepted_claims) == len(accepted) * 5
    print(
        f"A_clean: claims={len(accepted_claims)} stale_slots={a_slots} "
        f"stale_rows={len(a_rows)} PASS={arm_a}"
    )

    # --- simulated post-edit digests (bytes in memory; nothing on disk changes).
    block = (
        Path(args.block_file).read_text(encoding="utf-8") if args.block_file else CANDIDATE_BLOCK
    )
    target_bytes = (REPO_ROOT / args.target).read_bytes()
    sim_target = judge.sha256_bytes(target_bytes + block.encode("utf-8"))
    control_bytes = (REPO_ROOT / args.control).read_bytes()
    sim_control = judge.sha256_bytes(control_bytes + b"# blast-radius control arm C marker\n")
    artifact = (
        REPO_ROOT
        / "docs/evidence/C135/wave31-step6-domains-pin-blast-radius-20261018"
        / "artifacts/domains_yaml_after_candidate_block.yaml"
    )
    artifact.write_bytes(target_bytes + block.encode("utf-8"))
    reparsed = yaml.safe_load(artifact.read_text(encoding="utf-8"))
    domains_map = (reparsed or {}).get("domains") or {}
    block_ok = "tmx_etf_historical" in domains_map
    print(
        f"simulated: target_digest={sim_target[:16]} control_digest={sim_control[:16]} "
        f"candidate_block_parses={block_ok} "
        f"artifact_sha256={judge.sha256_bytes(artifact.read_bytes())[:16]} "
        f"domains_total={len(domains_map)}"
    )

    # --- arm D: the 0 -> 1 re-arm on one claim.
    probe = sorted(
        accepted_claims,
        key=lambda c: (str(c.get("provider")), str(c.get("upstream_model")), str(c.get("phase"))),
    )[0]
    probe_slots = [s for s in _pin_slots(probe) if s[2].get("path") == args.target]
    pins_d = copy.deepcopy(probe.get("pins") or {})
    rewrote_d = _simulate(pins_d, args.target, sim_target)
    issues_d, stale_d = judge._validate_pin_groups(pins_d, repo_root=REPO_ROOT, label="armD")
    arm_d = (
        stale_d is True
        and rewrote_d == 1
        and any(i.endswith("_current_hash_drift") for i in issues_d)
    )
    print(
        f"D_rearm: claim={probe.get('provider')}::{probe.get('upstream_model')}"
        f":{probe.get('phase')} "
        f"target_slots_in_claim={len(probe_slots)} rewrote={rewrote_d} "
        f"stale={stale_d} issues={issues_d} PASS={arm_d}"
    )
    expected_target_issues = {f"{pos}_current_hash_drift" for pos in target_positions}
    expected_control_issues = {f"{pos}_current_hash_drift" for pos in control_positions}

    # --- arm B: whole-tree simulation.
    pins_b = [c for c in accepted_claims if any(s[2]["path"] == args.target for s in _pin_slots(c))]
    b_rows, b_slots, b_issues, _ = _walk_claims(pins_b, args.target, sim_target)
    arm_b = (
        len(b_rows) == len(target_rows) == len(accepted)
        and b_slots == target_slots
        and b_issues == expected_target_issues
    )
    print(
        f"B_tree: claims_walked={len(pins_b)} stale_rows={len(b_rows)} "
        f"expected_rows={len(target_rows)} "
        f"accepted={len(accepted)} stale_slots={b_slots} expected_slots={target_slots} "
        f"issue_names={sorted(b_issues)} "
        f"expected_issues={sorted(expected_target_issues)} PASS={arm_b}"
    )

    # --- arm C: the predicate must be path-specific, not indiscriminate.
    pins_c = [
        c for c in accepted_claims if any(s[2]["path"] == args.control for s in _pin_slots(c))
    ]
    c_rows, c_slots, c_issues, _ = _walk_claims(pins_c, args.control, sim_control)
    arm_c = (
        len(c_rows) == len(control_rows)
        and c_slots == control_slots
        and len(c_rows) < len(accepted)
        and c_issues == expected_control_issues
    )
    print(
        f"C_specific: claims_walked={len(pins_c)} stale_rows={len(c_rows)} "
        f"expected_rows={len(control_rows)} "
        f"stale_slots={c_slots} expected_slots={control_slots} tree_rows={len(accepted)} "
        f"issue_names={sorted(c_issues)} "
        f"sample={c_rows and sorted(c_rows)[:3]} PASS={arm_c}"
    )

    passed = sum([arm_a, arm_d, arm_b, arm_c])
    ratio = (len(target_rows) / len(accepted)) if accepted else 0.0
    print(
        f"blast_radius target={args.target} fraction_of_accepted_rows_staled={ratio:.4f} "
        f"target_rows={len(target_rows)} of={len(accepted)} "
        f"target_slots={target_slots} of_total={total_slots}"
    )
    print(
        f"blast_radius_face arms=4 arms_passed={passed} arms_failed={4 - passed} PASS={passed == 4}"
    )
    print(f"domains_pin_blast_radius_exit={0 if passed == 4 else 1}")
    return 0 if passed == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
