"""Judge the iteration-2 registry census re-base against the live tree and the committed batch5 pin.

Read-only. Every line prints PASS/FAIL for one census face, then the probe forges one in-memory
mutation and shows the same judge turns FAIL, so the zeros are proven reachable.
"""

import copy
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

from opendata.data.domains import load_domains
from opendata.data.providers import register_providers
from opendata.data.providers.catalog import list_providers
from opendata.data.registry import ProviderRegistry

import importlib

FIXTURE = Path("tests/fixtures/registry_batch5_snapshot.json")
HEAD_COMMIT = subprocess.run(
    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()
HEAD_PIN_BYTES = subprocess.run(
    ["git", "show", f"{HEAD_COMMIT}:tests/fixtures/registry_batch5_snapshot.json"],
    capture_output=True,
    check=True,
).stdout
snap = json.loads(FIXTURE.read_bytes())
rows = snap["provider_model_identities"]
counts = snap["counts"]
domains = load_domains()
qsem = {d for d, s in domains.items() if s.semantics_declared}
registry = ProviderRegistry()
register_providers(registry)
descriptors = {(d.source, d.model, d.domain) for d in registry.list_model_descriptors()}
bindings = {}
for provider in list_providers():
    for binding in provider.fetcher_bindings:
        cls = getattr(importlib.import_module(binding.module), binding.class_name)
        for model in binding.canonical_model_ids or (binding.model_key,):
            bindings[(provider.source, model, cls.capability.domain)] = bool(
                binding.canonical_model_ids
            )

live = {
    "domain_count": len(domains),
    "query_semantic_domain_count": len(qsem),
    "provider_model_identity_count": len(bindings),
    "deferred_source_domain_leg_count": len(
        {(r[0], r[2]) for r in bindings if r[2] in qsem}
    ),
    "registered_source_domain_leg_count": len({(r[0], r[2]) for r in bindings}),
    "descriptor_eligible_identity_count": sum(1 for v in bindings.values() if v),
    "live_descriptor_count": len(descriptors),
}


def judge(snapshot, live_values):
    lines = []
    rows_l = snapshot["provider_model_identities"]
    counts_l = snapshot["counts"]
    key = lambda r: (r["source"], r["model"], r["domain"])
    pinned = {key(r) for r in rows_l}
    checks = [
        ("pinned domain ids == live", set(snapshot["domain_ids"]) == set(live_values["domains"])),
        ("pinned query-semantic ids == live", set(snapshot["query_semantic_domain_ids"]) == set(live_values["qsem"])),
        ("pinned identities == live bindings", pinned == set(live_values["bindings"])),
        ("deferred legs are the query-semantic legs", {key(r) and (r["source"], r["domain"]) for r in rows_l if r["domain"] in live_values["qsem"]} == live_values["deferred_legs"]),
        ("descriptor set == canonical-eligible set", live_values["descriptors"] == {key(r) for r in rows_l if r["canonical_ids_declared"]}),
        ("canonical_ids_declared matches live", all(r["canonical_ids_declared"] == live_values["binding_map"][key(r)] for r in rows_l)),
        ("identity count pin == live", counts_l["provider_model_identity_count"] == len(pinned)),
        ("eligible + model_key_only == identities", counts_l["descriptor_eligible_identity_count"] + counts_l["model_key_only_identity_count"] == counts_l["provider_model_identity_count"]),
        ("eligible legacy rows are all ths", {r["source"] for r in rows_l if r["canonical_ids_declared"] and r["domain"] not in live_values["qsem"]} == {"ths"}),
        ("semantic fields moved 0 vs batch5 pin", live_values["semantic_moves"] == {"contract": 0, "verified": 0}),
        ("scenario total explained by attribution", live_values["scenario_explained"]),
        ("identity delta == added - removed", live_values["identity_delta_explained"]),
        ("legacy legs unchanged at 33", counts_l["legacy_source_domain_leg_count"] == 33),
        ("frozen adjudication block byte-identical to the committed pin", live_values["adjudication_unchanged"]),
        ("export census delta re-derives", live_values["export_delta_rederives"]),
        ("approved_delta still reviewed-absent scenario", snapshot["approved_delta"]["scenario_present"] is False),
    ]
    for label, ok in checks:
        lines.append((label, bool(ok)))
    return lines


head_snapshot = json.loads(HEAD_PIN_BYTES)
head_rows = {(r["source"], r["model"], r["domain"]): r for r in head_snapshot["provider_model_identities"]}
live_rows = {(r["source"], r["model"], r["domain"]): r for r in rows}
common = set(head_rows) & set(live_rows)
moves = Counter()
for k in common:
    for f in ("binding", "scenario_present", "contract", "verified"):
        if head_rows[k][f] != live_rows[k][f]:
            moves[f] += 1
rebase = snap["iter2_census_rebase"]["registry_census_rebase"]
scenario_total = rebase["scenario_present_total"]
live_ctx = {
    "domains": set(domains),
    "qsem": qsem,
    "bindings": set(bindings),
    "binding_map": bindings,
    "descriptors": descriptors,
    "deferred_legs": {(r[0], r[2]) for r in bindings if r[2] in qsem},
    "semantic_moves": {"contract": moves["contract"], "verified": moves["verified"]},
    "scenario_explained": (
        scenario_total["batch5_pin"]
        + scenario_total["moved_on_existing_identity"]
        + scenario_total["carried_by_added_identity"]
        == scenario_total["live"]
        == sum(1 for r in rows if r["scenario_present"])
        and scenario_total["moved_on_existing_identity"] == moves["scenario_present"]
    ),
    "identity_delta_explained": (
        len(live_rows) - len(head_rows)
        == len(rebase["identities_added"]) - len(rebase["identities_removed"])
    ),
    "adjudication_unchanged": head_snapshot["batch5_count_adjudication"] == snap["batch5_count_adjudication"],
    "export_delta_rederives": {
        k: snap["iter2_census_rebase"]["live_counts"][k]
        - snap["batch5_count_adjudication"]["counts"][k]
        for k in snap["iter2_census_rebase"]["delta_vs_adjudication"]
    }
    == snap["iter2_census_rebase"]["delta_vs_adjudication"],
}

fixture_digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
import re

pin_sites = {
    "tests/test_provider_domains.py": re.compile(
        r'^BATCH5_SNAPSHOT_SHA256 = "([0-9a-f]{64})"', re.M
    ),
    "tests/test_pipeline_jobs.py": re.compile(
        r"sha256\(snapshot_bytes\)\.hexdigest\(\)\s*\n\s*== \"([0-9a-f]{64})\""
    ),
}
constants = {}
for path, pattern in pin_sites.items():
    found = pattern.findall(Path(path).read_text(encoding="utf-8"))
    constants[path] = found

print("=== iteration-2 registry census re-base judge ===")
print("live census:", json.dumps(live, sort_keys=True))
print("batch5_count_adjudication.counts:", json.dumps(snap["batch5_count_adjudication"]["counts"], sort_keys=True))
failed = 0
for label, ok in judge(snap, live_ctx):
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    failed += 0 if ok else 1
print(f"fixture_sha256={fixture_digest}")
for path, toks in constants.items():
    ok = toks == [fixture_digest]
    print(f"[{'PASS' if ok else 'FAIL'}] {path} pins exactly the live fixture digest; found={toks}")
    failed += 0 if ok else 1
print(f"head_commit={HEAD_COMMIT} head_pin_sha256={hashlib.sha256(HEAD_PIN_BYTES).hexdigest()}")
print("moves vs committed batch5 pin:", dict(moves))
print("scenario_present by source (moved rows):", dict(Counter(k[0] for k in common if head_rows[k]["scenario_present"] != live_rows[k]["scenario_present"])))
print("binding moves by source:", dict(Counter(k[0] for k in common if head_rows[k]["binding"] != live_rows[k]["binding"])))

forged = copy.deepcopy(snap)
target = next(r for r in forged["provider_model_identities"] if r["source"] == "ths" and r["model"] == "stock_daily")
target["canonical_ids_declared"] = False
forged_lines = judge(forged, live_ctx)
turned = [label for (label, ok), (base_label, base_ok) in zip(forged_lines, judge(snap, live_ctx)) if ok != base_ok]
print(f"[{'PASS' if turned else 'FAIL'}] counterfact: one eligibility flip turns {len(turned)} face(s) FAIL -> {turned}")
failed += 0 if turned else 1
print(f"census_verify_failed={failed}")
print("CENSUS_VERIFY_EXIT:", "GREEN" if failed == 0 else "RED")
