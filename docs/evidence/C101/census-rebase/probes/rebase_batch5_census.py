"""Re-derive the live registry faces of the batch5 identity snapshot."""

import importlib
import json
from pathlib import Path

from opendata.data.domains import load_domains
from opendata.data.providers.catalog import list_providers
from opendata.pipeline import alert_matrix

FIXTURE = Path("tests/fixtures/registry_batch5_snapshot.json")
snap = json.loads(FIXTURE.read_text(encoding="utf-8"))
domains = load_domains()
qsem = {d for d, s in domains.items() if s.semantics_declared}
legacy = set(domains) - qsem
legs_live = list(alert_matrix.registered_legs())
live_deferred_order = [d for d in legs_live if d in qsem]

rows: list[dict[str, object]] = []
for provider in list_providers():
    for binding in provider.fetcher_bindings:
        fetcher = getattr(importlib.import_module(binding.module), binding.class_name)
        domain = fetcher.capability.domain
        rows.extend(
            {
                "binding": f"{binding.module}:{binding.class_name}",
                "contract": domains[domain].contract,
                "domain": domain,
                "model": model,
                "scenario_present": bool(binding.scenario),
                "source": provider.source,
                "verified": bool(fetcher.capability.verified),
            }
            for model in binding.canonical_model_ids or (binding.model_key,)
        )
rows.sort(key=lambda r: (r["source"], r["model"], r["domain"]))
before = {
    k: len(snap[k])
    for k in (
        "provider_model_identities",
        "source_domain_legs",
        "deferred_source_domain_legs",
        "domain_ids",
        "deferred_domain_ids",
        "query_semantic_domain_ids",
    )
}


def key(r: dict[str, object]) -> tuple[object, object, object]:
    """Identify one census row by source, model and domain."""
    return (r["source"], r["model"], r["domain"])


pinned_rows = {key(r): r for r in snap["provider_model_identities"]}
added = [r for r in rows if key(r) not in pinned_rows]
moved = [
    (key(r), {f: (pinned_rows[key(r)][f], r[f]) for f in r if pinned_rows[key(r)][f] != r[f]})
    for r in rows
    if key(r) in pinned_rows and pinned_rows[key(r)] != r
]
removed = [k for k in pinned_rows if k not in {key(r) for r in rows}]

snap["provider_model_identities"] = rows
snap["source_domain_legs"] = [
    {"domain": d, "source": s} for s, d in sorted({(r["source"], r["domain"]) for r in rows})
]
snap["deferred_source_domain_legs"] = [
    {"domain": d, "source": s}
    for s, d in sorted({(r["source"], r["domain"]) for r in rows if r["domain"] in qsem})
]
snap["deferred_domain_ids"] = live_deferred_order
snap["domain_ids"] = legs_live
snap["query_semantic_domain_ids"] = sorted(qsem)

counts = snap["counts"]
counts["domain_count"] = len(domains)
counts["query_semantic_domain_count"] = len(qsem)
counts["deferred_domain_count"] = len(live_deferred_order)
counts["deferred_source_domain_leg_count"] = len(snap["deferred_source_domain_legs"])
counts["registered_source_domain_leg_count"] = len(snap["source_domain_legs"])
counts["provider_model_identity_count"] = len(rows)
candidate = snap["candidate_counts"]
candidate["fixed_identities"] = len(rows)
candidate["fixed_bound"] = len(snap["deferred_source_domain_legs"])
candidate["domain_specs"] = len(domains)
candidate["provider_model_legs"] = len(rows)
candidate["registered_sources_or_descriptors"] = len(list(list_providers()))

rebase = snap["iter2_census_rebase"]
rebase["registry_census_rebase"] = {
    "derived_from": "opendata.data.providers.catalog.list_providers() bindings "
    "+ load_domains() semantics_declared + alert_matrix.registered_legs()",
    "identities_added": [key(r) for r in added],
    "identities_removed": removed,
    "identity_fields_moved": len(moved),
    "field_shapes_moved": sorted({f for _k, changed in moved for f in changed}),
    "semantic_fields_untouched": ["contract", "verified"],
    "counts": {
        "domain_count": counts["domain_count"],
        "query_semantic_domain_count": counts["query_semantic_domain_count"],
        "deferred_source_domain_leg_count": counts["deferred_source_domain_leg_count"],
        "registered_source_domain_leg_count": counts["registered_source_domain_leg_count"],
        "provider_model_identity_count": counts["provider_model_identity_count"],
        "legacy_source_domain_leg_count": counts["legacy_source_domain_leg_count"],
    },
    "before": before,
}
FIXTURE.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(
    f"identities: pinned={before['provider_model_identities']} live={len(rows)} "
    f"added={len(added)} removed={len(removed)} moved_fields={len(moved)}"
)
for k, changed in moved[:6]:
    print("   moved:", k, changed)
print("field shapes moved:", rebase["registry_census_rebase"]["field_shapes_moved"])
print(
    "deferred legs:",
    counts["deferred_source_domain_leg_count"],
    "legacy legs:",
    counts["legacy_source_domain_leg_count"],
)
print(
    "domain ids:",
    len(snap["domain_ids"]),
    "query-semantic:",
    len(snap["query_semantic_domain_ids"]),
)
