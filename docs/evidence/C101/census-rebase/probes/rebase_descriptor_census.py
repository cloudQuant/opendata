"""Pin descriptor eligibility: one descriptor exists per identity with canonical_model_ids."""
import importlib
import json
from collections import Counter
from pathlib import Path

from opendata.data.domains import load_domains
from opendata.data.providers import register_providers
from opendata.data.providers.catalog import list_providers
from opendata.data.registry import ProviderRegistry
from opendata.pipeline import alert_matrix

FIXTURE = Path("tests/fixtures/registry_batch5_snapshot.json")
snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
domains = load_domains()
qsem = {d for d, s in domains.items() if s.semantics_declared}

live_declared = {}
emitted = 0
collisions = []
for provider in list_providers():
    for binding in provider.fetcher_bindings:
        fetcher_cls = getattr(importlib.import_module(binding.module), binding.class_name)
        domain = fetcher_cls.capability.domain
        for model in binding.canonical_model_ids or (binding.model_key,):
            emitted += 1
            identity = (provider.source, model, domain)
            declared = bool(binding.canonical_model_ids)
            if identity in live_declared and live_declared[identity] != declared:
                collisions.append(identity)
            live_declared[identity] = declared
assert collisions == [], f"same identity both canonical-id-declared and model-key-only: {collisions[:3]}"

missing = [k for k in live_declared if k not in
           {(r["source"], r["model"], r["domain"]) for r in snap["provider_model_identities"]}]
assert not missing, f"live binding not pinned: {missing[:3]}"

for row in snap["provider_model_identities"]:
    row["canonical_ids_declared"] = live_declared[(row["source"], row["model"], row["domain"])]
snap["provider_model_identities"].sort(key=lambda r: (r["source"], r["model"], r["domain"]))

registry = ProviderRegistry()
register_providers(registry)
descriptors = {(d.source, d.model, d.domain) for d in registry.list_model_descriptors()}
eligible = [r for r in snap["provider_model_identities"] if r["canonical_ids_declared"]]
eligible_keys = {(r["source"], r["model"], r["domain"]) for r in eligible}
assert descriptors == eligible_keys, (
    f"descriptors {len(descriptors)} != eligible {len(eligible_keys)}; "
    f"desc-only={sorted(descriptors - eligible_keys)[:3]} elig-only={sorted(eligible_keys - descriptors)[:3]}"
)

deferred_legs = {(r["source"], r["domain"]) for r in eligible if r["domain"] in qsem}
legacy_eligible = [r for r in eligible if r["domain"] not in qsem]
model_key_only = [r for r in snap["provider_model_identities"] if not r["canonical_ids_declared"]]
in_deferred_partition = {(s, d) for s, d in
                        {(r["source"], r["domain"]) for r in eligible if r["domain"] in qsem}}
legs = alert_matrix.registered_legs()
assert sorted(legs) == sorted(domains)

counts = snap["counts"]
counts["descriptor_eligible_identity_count"] = len(eligible)
counts["model_key_only_identity_count"] = len(model_key_only)
counts["descriptor_eligible_legacy_identity_count"] = len(legacy_eligible)

rebase = snap["iter2_census_rebase"]["registry_census_rebase"]
rebase["field_added_by_iter2_census_rebase"] = {
    "name": "canonical_ids_declared",
    "why": (
        "batch5 pinned the identity rows but not which of them name a canonical model id; "
        "ProviderRegistry.list_model_descriptors() only yields a descriptor for those, so a "
        "descriptor census cannot be checked against the pin without this field"
    ),
    "measured_by": "binding.canonical_model_ids truthiness in catalog.list_providers()",
    "is_semantic_field": False,
}
rebase["descriptor_partition"] = {
    "rule": (
        "catalog.register_providers calls registry.register_provider_models only when "
        "binding.canonical_model_ids is non-empty, so one descriptor exists per identity with "
        "declared canonical model ids and none for a model_key-only binding"
    ),
    "provider_model_identity_count": len(snap["provider_model_identities"]),
    "descriptor_eligible_identity_count": len(eligible),
    "live_descriptor_count": len(descriptors),
    "descriptor_eligible_query_semantic_count": len(eligible) - len(legacy_eligible),
    "descriptor_eligible_legacy_count": len(legacy_eligible),
    "descriptor_eligible_legacy_by_source": dict(Counter(r["source"] for r in legacy_eligible)),
    "model_key_only_identity_count": len(model_key_only),
    "model_key_only_by_source": dict(Counter(r["source"] for r in model_key_only)),
    "model_key_only_by_domain": dict(Counter(r["domain"] for r in model_key_only)),
    "pre_rebase_assertion": (
        "tests compared every live descriptor to the deferred (query-semantic) source/domain "
        "partition; the partition is exact on the query-semantic side but the legacy side carries "
        "canonical model ids for ths, so the pin must state eligibility per identity"
    ),
    "counterfact": (
        "dropping canonical_model_ids from ths/models/stock_daily.py removes one descriptor and one "
        "eligible identity together; the assertion stays green, so this pin is falsified only by a "
        "descriptor set that differs from the eligible set or by an eligible count that is not "
        "live_descriptor_count"
    ),
    "before": {"canonical_ids_declared": "absent", "descriptors_pinned_against": "deferred partition"},
}

FIXTURE.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(
    f"identities={len(snap['provider_model_identities'])} eligible={len(eligible)} "
    f"descriptors={len(descriptors)} model_key_only={len(model_key_only)} "
    f"legacy_eligible={len(legacy_eligible)} "
    f"qsem_eligible={len(eligible) - len(legacy_eligible)}"
)
print("legacy eligible by source:", rebase["descriptor_partition"]["descriptor_eligible_legacy_by_source"])
print("model_key_only by source:", rebase["descriptor_partition"]["model_key_only_by_source"])
print("model_key_only domains:", sorted(rebase["descriptor_partition"]["model_key_only_by_domain"]))
print("eligible deferred legs:", len(in_deferred_partition), "registered legs:",
      sum(len(v) for v in legs.values()))
