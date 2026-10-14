"""C103: re-base the ``scenario_present`` census in the batch5 oracle against the live registry.

``tests/test_provider_domains.py:1784`` asserts per pinned identity row that
``(live binding.scenario is not None) is row["scenario_present"]``.  Nine OECD bindings gained a
scenario string during iteration 2, so the rows lag the registry by nine presence flips while the
pinned identity set itself is unchanged.  The fixture's own ``registry_census_rebase`` block
declares those presence counts to be "a diff of the committed batch5 pin against the live
registry", so they are derived quantities and are re-based here; ``contract`` and ``verified``
moves are refused outright because the block pins them at zero.

Every number written comes from an artifact:
  * the committed batch5 pin is the git blob whose sha256 equals
    ``registry_census_rebase.pin_chain.committed_batch5_pin.sha256`` -- it is recovered, not typed;
  * the live census comes from ``list_providers()`` exactly as the guard builds it.

Usage: python docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py [--apply]
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

SNAPSHOT_REL = "tests/fixtures/registry_batch5_snapshot.json"
REQUIRED_ROW_KEYS = ("source", "model", "domain", "binding", "scenario_present", "contract", "verified")


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()
sys.path.insert(0, str(REPO))
APPLY = "--apply" in sys.argv


def git(*args: str) -> str:
    return subprocess.run(("git", *args), cwd=REPO, capture_output=True, text=True, check=True).stdout


def recover_pin_blob(expected_sha: str, expected_rows: int) -> tuple[str, list[dict[str, object]]]:
    """Find the git revision whose snapshot blob hashes to the digest the fixture recorded."""
    revisions = git("log", "--all", "--format=%H", "--", SNAPSHOT_REL).split()
    print(f"face: candidate_revisions={len(revisions)}")
    for rev in revisions:
        proc = subprocess.run(
            ["git", "show", f"{rev}:{SNAPSHOT_REL}"], cwd=REPO, capture_output=True, check=False
        )
        if proc.returncode:
            continue
        digest = hashlib.sha256(proc.stdout).hexdigest()
        if digest != expected_sha:
            continue
        rows = json.loads(proc.stdout)["provider_model_identities"]
        if len(rows) != expected_rows:
            raise SystemExit(f"pin blob {rev} has {len(rows)} rows, expected {expected_rows}")
        return rev, rows
    raise SystemExit(f"no git revision of {SNAPSHOT_REL} hashes to the recorded {expected_sha}")


def live_census() -> tuple[dict[tuple[str, str], dict[str, object]], list[str]]:
    from opendata.data.domains import load_domains
    from opendata.data.providers import list_providers, register_providers
    from opendata.data.registry import ProviderRegistry

    register_providers()
    registry = ProviderRegistry()
    register_providers(registry)
    domains = load_domains()

    # Mirrors tests/test_provider_domains.py:1774-1779.
    bindings = {
        (provider.source, model_id): binding
        for provider in list_providers()
        for binding in provider.fetcher_bindings
        for model_id in (binding.canonical_model_ids or (binding.model_key,))
    }
    rows: dict[tuple[str, str], dict[str, object]] = {}
    unresolvable: list[str] = []
    domain_spec_missing: list[str] = []
    for key, binding in bindings.items():
        source, model = key
        entry: dict[str, object] = {
            "binding": f"{binding.module}:{binding.class_name}",
            "scenario_present": binding.scenario is not None,
            "canonical_ids_declared": bool(binding.canonical_model_ids),
        }
        try:
            fetcher = registry.resolve_model(source, model)
        except Exception as exc:  # noqa: BLE001 - the denominator is printed, never swallowed
            unresolvable.append(f"{source}/{model}:{type(exc).__name__}")
            entry["resolve_failed"] = True
            rows[key] = entry
            continue
        domain = fetcher.capability.domain
        entry["domain"] = domain
        entry["verified"] = bool(fetcher.capability.verified)
        spec = domains.get(domain)
        if spec is None:
            domain_spec_missing.append(f"{source}/{model}@{domain}")
        else:
            entry["contract"] = spec.contract
        rows[key] = entry
    print(f"face: live_bindings={len(rows)} unresolvable={len(unresolvable)}")
    if unresolvable:
        print(f"face: unresolvable_examples={unresolvable[:5]}")
    print(f"face: domain_spec_missing={len(domain_spec_missing)} {domain_spec_missing[:5]}")
    return rows, unresolvable


def main() -> int:
    snapshot_path = REPO / SNAPSHOT_REL
    original_bytes = snapshot_path.read_bytes()
    old_payload_sha = hashlib.sha256(original_bytes).hexdigest()
    snapshot = json.loads(original_bytes)

    rebase = snapshot["iter2_census_rebase"]
    print(f"face: iter2_census_rebase_keys={sorted(rebase)}")
    census = rebase["registry_census_rebase"]
    print(f"face: registry_census_rebase_keys={sorted(census)}")
    pin_chain = census["pin_chain"]
    committed = pin_chain["committed_batch5_pin"]
    print(f"face: recorded_pin_sha={committed['sha256']}")

    pin_rev, pin_rows = recover_pin_blob(committed["sha256"], committed["provider_model_identity_count"])
    print(f"face: recovered_pin_rev={pin_rev}")
    for label, rows in (("committed_pin", pin_rows), ("current_fixture", snapshot["provider_model_identities"])):
        missing_keys = [k for k in REQUIRED_ROW_KEYS if any(k not in row for row in rows)]
        if missing_keys:
            raise SystemExit(f"{label} rows lack keys {missing_keys}; refusing to default them")
    print(f"face: pin_rows={len(pin_rows)} pin_scenario_present={sum(1 for r in pin_rows if r['scenario_present'])}")
    print(
        "face: fixture_rows="
        f"{len(snapshot['provider_model_identities'])} "
        f"fixture_scenario_present="
        f"{sum(1 for r in snapshot['provider_model_identities'] if r['scenario_present'])}"
    )

    live, unresolvable = live_census()
    pin_by_key = {(row["source"], row["model"]): row for row in pin_rows}
    added = sorted(set(live) - set(pin_by_key))
    removed = sorted(set(pin_by_key) - set(live))
    common = sorted(set(live) & set(pin_by_key))

    scenario_moves = [key for key in common if live[key]["scenario_present"] != pin_by_key[key]["scenario_present"]]
    moves_by_source = dict(Counter(key[0] for key in scenario_moves))
    carried_by_added = sum(1 for key in added if live[key]["scenario_present"])
    live_scenario_total = sum(1 for entry in live.values() if entry["scenario_present"])
    pin_scenario_total = sum(1 for row in pin_rows if row["scenario_present"])

    binding_moves = [key for key in common if live[key]["binding"] != pin_by_key[key]["binding"]]
    binding_moves_by_source = dict(Counter(key[0] for key in binding_moves))
    resolvable = [key for key in common if "resolve_failed" not in live[key]]
    contract_judged = [key for key in resolvable if "contract" in live[key]]
    contract_moves = [key for key in contract_judged if live[key]["contract"] != pin_by_key[key]["contract"]]
    verified_moves = [key for key in resolvable if live[key]["verified"] != pin_by_key[key]["verified"]]
    domain_moves = [key for key in resolvable if live[key]["domain"] != pin_by_key[key]["domain"]]

    print(f"face: live_rows={len(live)} live_scenario_present_total={live_scenario_total}")
    print(f"face: identities_added={len(added)} identities_removed={len(removed)} net={len(added) - len(removed)}")
    print(f"face: added_examples={added[:6]}")
    print(f"face: scenario_moves_on_existing={len(scenario_moves)} by_source={moves_by_source}")
    print(f"face: carried_by_added_identity={carried_by_added}")
    print(f"face: binding_moves={len(binding_moves)} by_source={binding_moves_by_source}")
    print(
        "face: semantic_moves="
        f"contract={len(contract_moves)} verified={len(verified_moves)} "
        f"(contract judged on {len(contract_judged)}, verified on {len(resolvable)} of "
        f"{len(common)} common identities)"
    )
    print(f"face: domain_moves={len(domain_moves)} {domain_moves[:4]}")

    # The block's own identity_renames list is the only sanctioned reason a contract name may differ.
    renamed_rows = census["identity_renames"]["renamed"]
    renamed_ids = {(row["source"], row["model"]) for row in renamed_rows}
    contract_moves_outside_renames = [key for key in contract_moves if key not in renamed_ids]
    domain_moves_outside_renames = [key for key in domain_moves if key not in renamed_ids]
    print(f"face: identity_renames_records={len(renamed_rows)}")
    print(
        "face: moves_outside_rename_partition="
        f"contract={len(contract_moves_outside_renames)} domain={len(domain_moves_outside_renames)}"
    )
    balanced = pin_scenario_total + len(scenario_moves) + carried_by_added == live_scenario_total
    print(f"face: arithmetic_balanced={balanced}")

    from opendata.data import models
    from opendata.data.models.base import ContractModel

    actual_contract = sum(
        1
        for name in models.__all__
        if isinstance(getattr(models, name), type) and issubclass(getattr(models, name), ContractModel)
    )
    actual_non_base = sum(
        1
        for name in models.__all__
        if isinstance(getattr(models, name), type)
        and getattr(models, name) is not ContractModel
        and issubclass(getattr(models, name), ContractModel)
    )
    actual_non_names = sorted(
        name
        for name in models.__all__
        if not (
            isinstance(getattr(models, name), type)
            and issubclass(getattr(models, name), ContractModel)
        )
    )
    print(
        "face: live_export_census="
        f"all_symbols={len(models.__all__)} contract={actual_contract} "
        f"non_base={actual_non_base} non_contract={len(actual_non_names)} {actual_non_names}"
    )
    print(f"face: candidate_counts_before={snapshot['candidate_counts']}")

    documented_totals = census["scenario_present_total"]
    documented_field_moves = census["field_move_counts"]
    print(f"face: documented_totals_before={documented_totals}")
    print(f"face: documented_field_moves_before={documented_field_moves}")
    print(f"face: documented_moves_by_source_before={census['scenario_moves_by_source']}")
    print(f"face: documented_binding_moves_before={census['binding_moves_by_source']}")
    print(
        "face: documented_identity_add_remove_before="
        f"{census['identities_added']}/{census['identities_removed']}"
    )
    print(f"face: documented_semantic_field_moves_before={census['semantic_field_moves']}")
    print(f"face: documented_derived_from={census.get('derived_from')}")
    print(f"face: documented_re_pinned_sources={rebase.get('re_pinned_sources')}")
    print(f"face: frozen_and_unchanged={rebase.get('frozen_and_unchanged')}")

    # The flip set the fixture rows need: current fixture rows vs live.
    fixture_rows = snapshot["provider_model_identities"]
    fixture_by_key = {(row["source"], row["model"]): row for row in fixture_rows}
    flips = [
        (key, fixture_by_key[key]["scenario_present"], live[key]["scenario_present"])
        for key in sorted(fixture_by_key)
        if key in live and bool(fixture_by_key[key]["scenario_present"]) != bool(live[key]["scenario_present"])
    ]
    print(f"face: fixture_row_flips_needed={len(flips)}")
    for (source, model), before, after in flips:
        print(f"face: flip {source}/{model} {before} -> {after}")

    if contract_moves_outside_renames or verified_moves or domain_moves_outside_renames:
        print(
            "verdict: REFUSED - a contract/verified/domain move outside the recorded rename "
            "partition is semantic; the block pins those at zero"
        )
        for key in (contract_moves_outside_renames + verified_moves + domain_moves_outside_renames)[:8]:
            print(f"face: semantic_move {key}")
        return 1
    if removed:
        print("verdict: REFUSED - live registry lost pinned identities; a census re-base cannot erase that")
        return 1
    if not balanced:
        print("verdict: REFUSED - the derived move split does not reconcile with the live total")
        return 1
    if not flips:
        print("verdict: REFUSED - the fixture rows already match live, nothing to re-base")
        return 1

    live_counts = rebase["live_counts"]
    candidate_counts = snapshot["candidate_counts"]
    counts_measured = {
        "all_symbols": len(models.__all__),
        "contract_model_classes_including_base": actual_contract,
        "non_base_contract_model_classes": actual_non_base,
    }
    measured = {**counts_measured, "non_contract_exports": len(actual_non_names)}
    # candidate_counts carries non_contract_exports as a RECORD LIST, live_counts does not carry the
    # key at all, and batch4_baseline holds the pre-iteration-2 name list. Comparing an int against
    # those shapes refuses on a None or a list, so each block is judged on the shape it really holds.
    candidate_export_records = candidate_counts["non_contract_exports"]
    baseline_export_names = snapshot["batch4_baseline"]["non_contract_exports"]
    export_plan = {
        key: {
            "candidate_counts": candidate_counts.get(key),
            "live_counts": live_counts.get(key),
            "batch4_baseline": snapshot["batch4_baseline"].get(key),
            "live_measured": value,
        }
        for key, value in counts_measured.items()
    }
    export_plan["non_contract_exports"] = {
        "candidate_record_count": len(candidate_export_records),
        "candidate_record_names": sorted(str(record["name"]) for record in candidate_export_records),
        "batch4_baseline_names": sorted(baseline_export_names),
        "live_measured_count": len(actual_non_names),
        "live_measured_names": actual_non_names,
    }
    print(f"face: export_counts_plan={export_plan}")
    print(f"face: live_counts_all_keys={sorted(live_counts)}")
    print(f"face: candidate_counts_all_keys={sorted(candidate_counts)}")
    for key, value in counts_measured.items():
        for block_name, block in (("live_counts", live_counts), ("candidate_counts", candidate_counts)):
            if key not in block:
                print(f"face: {block_name} does not carry {key}")
            elif block[key] != value:
                print(
                    f"verdict: REFUSED - {block_name}.{key}={block[key]} but live measures {value}; "
                    "a census re-base cannot carry an export count that disagrees with live"
                )
                return 1
    if len(candidate_export_records) != len(actual_non_names):
        print(
            "verdict: REFUSED - candidate_counts.non_contract_exports records "
            f"{len(candidate_export_records)} names but live exports {len(actual_non_names)}"
        )
        return 1
    if sorted(str(record["name"]) for record in candidate_export_records) != actual_non_names:
        print("verdict: REFUSED - the pinned non-contract export names differ from live __all__")
        return 1
    if sorted(baseline_export_names) == actual_non_names:
        print("verdict: REFUSED - batch4_baseline would be identical to live, so it is not a baseline")
        return 1

    staged_rows = json.loads(original_bytes)
    for (source, model), _before, after in flips:
        for row in staged_rows["provider_model_identities"]:
            if (row["source"], row["model"]) == (source, model):
                row["scenario_present"] = after
    # candidate_counts is NOT touched here: the gate above proves the pinned export counts already
    # agree with live, so re-pinning them would move a byte-frozen field for no reason.
    staged_total = sum(1 for r in staged_rows["provider_model_identities"] if r["scenario_present"])
    print(f"face: staged_scenario_total={staged_total} live_scenario_total={live_scenario_total}")
    if staged_total != live_scenario_total:
        print("verdict: REFUSED - the flipped fixture still disagrees with the live census")
        return 1
    if len(staged_rows["provider_model_identities"]) != len(snapshot["provider_model_identities"]):
        print("verdict: REFUSED - the write would change the pinned identity row count")
        return 1

    new_census = staged_rows["iter2_census_rebase"]["registry_census_rebase"]
    new_census["pin_chain"]["committed_batch5_pin"] = {
        "sha256": committed["sha256"],
        "provider_model_identity_count": len(pin_rows),
        "recovered_from_git_rev": pin_rev,
        "scenario_present": pin_scenario_total,
    }
    new_census["c103_scenario_rebase"] = {
        "measured_by": "docs/evidence/C103/batch5-scenario-census/probes/scenario_rebase.py --apply",
        "identity_key": "(source, model) -- the key tests/test_provider_domains.py:1781-1784 judges",
        "why_this_block_exists": (
            "registry_census_rebase.scenario_present_total is keyed by (source, model, domain), so its "
            "29 moves and 4 carried-by-added exclude the three federal_reserve renames; the guard "
            "below keys identities by (source, model), so those three rows count as moves on an "
            "existing identity. Both partitions are published instead of one being rewritten."
        ),
        "batch5_pin_scenario_present": pin_scenario_total,
        "live_scenario_present": live_scenario_total,
        "moved_on_existing_identity": len(scenario_moves),
        "carried_by_added_identity": carried_by_added,
        "arithmetic": f"{pin_scenario_total} + {len(scenario_moves)} + {carried_by_added} = {live_scenario_total}",
        "balanced": balanced,
        "moves_by_source": moves_by_source,
        "oecd_moves_this_round": len(flips),
        "binding_moves": len(binding_moves),
        "binding_moves_by_source": binding_moves_by_source,
        "contract_moves_all": len(contract_moves),
        "contract_moves_outside_renames": len(contract_moves_outside_renames),
        "verified_moves": len(verified_moves),
        "verified_move_rule": "any nonzero value here is a reviewed-output-contract change and is refused",
        "resolvable_denominator": {
            "common_identities": len(common),
            "judged_for_contract": len(contract_judged),
            "judged_for_verified": len(resolvable),
            "unresolvable": len(unresolvable),
            "model_key_only_identity_count": census["descriptor_partition"][
                "model_key_only_identity_count"
            ],
            "unresolvable_note": (
                f"the {len(unresolvable)} identities resolve_model cannot reach are the model_key-only "
                "cohort counted at registry_census_rebase.descriptor_partition."
                "model_key_only_identity_count"
            ),
        },
        "fixture_rows_scenario_present_before": sum(
            1 for row in snapshot["provider_model_identities"] if row["scenario_present"]
        ),
        "fixture_rows_scenario_present_after": sum(
            1 for row in staged_rows["provider_model_identities"] if row["scenario_present"]
        ),
        "fixture_total_equals_live_after_write": staged_total == live_scenario_total,
        "rows_flipped_this_round": [
            f"{source}/{model}: {before} -> {after}" for (source, model), before, after in flips
        ],
        "export_counts_this_round": {
            "written_by_this_probe": False,
            "gate": "each value is refused unless live_counts and candidate_counts both equal it",
            "measured": measured,
        },
        "refusal_arms": (
            "the probe returns 1 without writing on: a contract/verified/domain move outside "
            "identity_renames, any removed live identity, an unbalanced move split, an empty flip "
            "set, an export count that disagrees with live, a serializer round-trip that is not "
            "byte-identical, or any frozen field that would move in the write"
        ),
    }

    staged_text = json.dumps(staged_rows, ensure_ascii=False, indent=2) + "\n"
    staged_bytes = staged_text.encode()
    # Positive control on the serializer itself: re-dumping the UNTOUCHED snapshot must be
    # byte-identical, otherwise this write would reformat 235 KB and the diff would hide the edits.
    roundtrip_bytes = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode()
    print(f"face: serializer_roundtrip_identical={roundtrip_bytes == original_bytes}")
    if roundtrip_bytes != original_bytes:
        print("verdict: REFUSED - the serializer would churn unrelated bytes")
        return 1
    print(f"face: rows_total_after={sum(1 for r in staged_rows['provider_model_identities'] if r['scenario_present'])}")
    historical_keys = (
        "scenario_present_total",
        "field_move_counts",
        "scenario_moves_by_source",
        "binding_moves_by_source",
        "identities_added",
        "identities_removed",
        "semantic_field_moves",
        "identity_renames",
        "descriptor_partition",
        "approved_delta_conflict",
    )
    historical_unchanged = all(
        new_census[key] == census[key] for key in historical_keys if key in new_census
    )
    print(f"face: historical_census_keys={len(historical_keys)} unchanged={historical_unchanged}")
    frozen_gates = {
        "candidate_sha256": staged_rows["candidate_sha256"] == snapshot["candidate_sha256"],
        "approved_delta": staged_rows["approved_delta"] == snapshot["approved_delta"],
        "source_hashes": staged_rows["source_hashes_sha256"] == snapshot["source_hashes_sha256"],
        "counts": staged_rows["counts"] == snapshot["counts"],
        "candidate_counts": staged_rows["candidate_counts"] == snapshot["candidate_counts"],
        "batch4_baseline": staged_rows["batch4_baseline"] == snapshot["batch4_baseline"],
        "domain_ids": staged_rows["domain_ids"] == snapshot["domain_ids"],
        "historical_census": historical_unchanged,
    }
    for label, is_unchanged in frozen_gates.items():
        print(f"face: frozen[{label}]=unchanged={is_unchanged}")
    if not all(frozen_gates.values()):
        print("verdict: REFUSED - a byte-frozen field would have moved in this write")
        return 1
    new_payload_sha = hashlib.sha256(staged_bytes).hexdigest()
    print(f"face: payload_sha_before={old_payload_sha}")
    print(f"face: payload_sha_after={new_payload_sha}")

    if not APPLY:
        print("verdict: DRY RUN - nothing written (pass --apply to write)")
        return 0

    snapshot_path.write_bytes(staged_bytes)
    print(f"face: applied_written_sha_matches={hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == new_payload_sha}")
    print(
        f"face: applied_bytes_delta={len(staged_bytes) - len(original_bytes)}"
    )
    print(
        f"verdict: APPLIED - {len(flips)} scenario_present rows re-based from live bindings, "
        "every export count left byte-identical after being checked against live, "
        "and a derived c103_scenario_rebase block appended"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
