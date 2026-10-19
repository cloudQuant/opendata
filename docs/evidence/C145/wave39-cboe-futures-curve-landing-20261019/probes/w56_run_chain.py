"""Run the wave-39 full-tree re-sign chain on the shipped driver object, into this bundle.

Why the whole tree: this wave adds one ``opendata/data/domains.yaml`` key (``cboe_futures_curve``),
and that file is pinned inside every landed row's ``contract`` group, so re-producing only cboe
would
leave 27 other providers stale on the same digest and plane 1 could not rise. The radius is counted
here from ``docs/evidence/C101/claims.merged.json`` -- which claims pin which path, grouped by
owning
provider -- rather than assumed.

This probe does not re-implement the chain. It imports wave 37's ``w39_run_chain.py`` and re-points
its ``BUNDLE``, ``RUNS`` and ``PRE_CHAIN_OUTPUT``, and refuses to start if any write target resolves
outside this bundle. That guard exists because an earlier wave re-pointed only ``RUNS`` and the
driver's bundle-relative ``PRE_CHAIN_OUTPUT`` then overwrote another wave's committed carrier.

The pre-chain judge run is the counterfact: committed claims read against this edited tree. Its
printed ``0 accepted`` is what makes "these edits required a re-sign" a measurement instead of a
claim.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
DRIVER = (
    REPO / "docs/evidence/C141/wave37-cboe-index-snapshots-20261019/probes" / "w39_run_chain.py"
)
#: Paths this wave edited whose pins decide the re-sign radius.
PIN_TARGETS = (
    "opendata/data/domains.yaml",
    "opendata/data/providers/cboe/specs.py",
    "opendata/data/providers/cboe/provider.py",
    "opendata/data/models/__init__.py",
    "scripts/quality/iter2_witnesses/_parts/cboe/futures.py",
)


def _load_driver() -> Any:  # noqa: ANN401  # the shipped chain driver is this probe's instrument
    """Import wave 37's driver object and re-point every bundle-relative target."""
    spec = importlib.util.spec_from_file_location("w37_chain_driver", DRIVER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import chain driver {DRIVER}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w37_chain_driver"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS
    module.PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.report.json"
    return module


def _write_targets(chain: Any) -> tuple[dict[str, str], list[str]]:  # noqa: ANN401
    """Resolve every path the driver writes and prove none leaves this bundle."""
    targets = {
        "RUNS": str(Path(str(chain.RUNS))),
        "PRE_CHAIN_OUTPUT": str(REPO / str(chain.PRE_CHAIN_OUTPUT)),
    }
    bundle_root = str(RUNS)
    outside = sorted(name for name, path in targets.items() if not path.startswith(bundle_root))
    return targets, outside


def _pin_owner_face() -> dict[str, Any]:
    """Count merged claims pinning each edited path, split by owning provider."""
    document = json.loads((REPO / "docs/evidence/C101/claims.merged.json").read_text("utf-8"))
    owners: dict[str, int] = {}
    per_target: dict[str, int] = {}
    for claim in document.get("claims") or []:
        paths: set[str] = set()
        for group in (claim.get("pins") or {}).values():
            for entry in group if isinstance(group, list) else [group]:
                if isinstance(entry, dict):
                    paths.add(str(entry.get("path") or entry.get("relative_path") or ""))
        hit = [target for target in PIN_TARGETS if target in paths]
        for target in hit:
            per_target[target] = per_target.get(target, 0) + 1
        if hit:
            provider = str(claim.get("provider"))
            owners[provider] = owners.get(provider, 0) + 1
    return {
        "providers": len(owners),
        "claims_pinned": sum(owners.values()),
        "total_claims": len(document.get("claims") or []),
        "by_target": per_target,
        "distinct_providers": sorted(owners),
    }


def main() -> int:
    """Measure the pin radius, prove staleness, then run the shipped chain into this bundle."""
    chain = _load_driver()
    RUNS.mkdir(parents=True, exist_ok=True)
    targets, outside = _write_targets(chain)
    print(f"write_targets={targets} outside_bundle={outside}", flush=True)
    if outside:
        print(f"w56_run_chain_exit=1 # driver would write outside {BUNDLE}", flush=True)
        raise SystemExit(f"driver write target outside this bundle: {outside}")
    radius = _pin_owner_face()
    print(f"pin_radius={json.dumps(radius, ensure_ascii=False)}", flush=True)
    rc = int(chain.main())
    summary_path = RUNS / "chain_summary.txt"
    text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    header = (
        "# wave-39 re-sign chain (cboe::FuturesCurve landed)\n"
        "# carried by "
        + " ".join([chain.PY, str(REPO / BUNDLE / "probes" / "w56_run_chain.py")])
        + "\n"
        + f"pin_radius={json.dumps(radius, ensure_ascii=False)}\n\n"
    )
    summary_path.write_text(header + text, encoding="utf-8")
    print(f"w56_run_chain_reused_driver={DRIVER.name} chain_returned={rc}", flush=True)
    print(f"w56_run_chain_exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
