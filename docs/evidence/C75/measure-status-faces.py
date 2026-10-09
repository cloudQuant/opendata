"""C75 read-only face measurement for the iteration-2 status prose.

Every number printed here is recomputed from the tree; nothing is typed from the old prose.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
sys.path.insert(0, str(REPO))

from opendata.data.providers import catalog  # noqa: E402
from opendata.data.registry import get_registry  # noqa: E402
from opendata.pipeline.alert_matrix import registered_legs  # noqa: E402
from scripts.quality import model_capability_census as census  # noqa: E402

registry = get_registry()
catalog.register_providers(registry)

caps = list(registry.capabilities())
auto = [c for c in caps if c.participates_in_auto()]
descriptors = list(registry.list_model_descriptors())
legs = registered_legs()

print(f"capabilities={len(caps)}")
print(f"auto_routable={len(auto)}")
print(f"model_descriptors={len(descriptors)}")
print(f"descriptor_models_unique={len({d.model for d in descriptors})}")
print(f"sources_with_capabilities={len({c.source for c in caps})}")
print(f"leg_domains={len(legs)}")
print(f"leg_count={sum(len(v) for v in legs.values())}")
print(f"canonical_capabilities={len(census.canonical_capabilities())}")

ledger = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
with ledger.open(encoding="utf-8-sig", newline="") as fh:
    rows = list(csv.DictReader(fh))
status = Counter(r["implementation_task_status"] for r in rows)
print(f"ledger_rows={len(rows)}")
print(f"ledger_status={dict(sorted(status.items()))}")
print(f"ledger_providers={len({r['provider'] for r in rows})}")
print(f"ledger_model_identities={len({(r['provider'], r['upstream_model']) for r in rows})}")
print(f"ledger_upstream_models_unique={len({r['upstream_model'] for r in rows})}")
for col in ("live_verification_status", "scenario_status", "rights_status"):
    counts = Counter(r[col] for r in rows)
    print(f"ledger_{col}={dict(sorted(counts.items()))}")
