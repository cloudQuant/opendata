"""Run the wave-38 full-tree re-sign chain, re-using wave 37's shipped driver object.

Why the whole tree again: this wave edits ``opendata/data/providers/catalog.py`` (seeking_alpha
moves from ``_reserved_provider`` to a bound provider) and ``opendata/data/domains.yaml`` (new key
``seeking_alpha_calendar_earnings``). ``catalog.py`` is pinned as ``source`` by 1685 claims across
all 28 witnessed providers, and ``domains.yaml`` by every landed row's ``contract`` group, so the
re-sign radius is the tree -- measured the way wave 37 measured the cboe-shard radius, by counting
pin owners rather than assuming.

This probe does not re-implement the chain. It imports ``w39_run_chain.py`` and re-points its
``RUNS`` directory at this bundle, so every carrier here carries the shipped driver's own
argv-and-exit discipline and wave 37's committed carriers stay untouched -- a driver cloned by hand
both duplicates the premises and, in an earlier wave, destroyed 61 committed carriers.

The first run of this probe broke that rule anyway, and the correction is recorded here rather than
quietly applied: the driver writes its pre-chain judge report to ``PRE_CHAIN_OUTPUT``, which is
bundle-relative but was NOT re-pointed along with ``RUNS``. So wave 38's pre-chain report landed on
top of wave 37's committed ``.../C141/.../runs/judge_pre_chain.report.json`` and ``.md``. Both files
were moved, byte-for-byte, into this bundle (they are wave 38's reading: 1710 claims, 0 accepted,
337 STALE) and wave 37's copies were restored with ``git checkout HEAD --``; ``git diff --stat HEAD
-- docs/evidence/C141`` reads empty afterwards, and the closure face arms that the wave-38 commit
touches no foreign carrier. ``PRE_CHAIN_OUTPUT`` is now re-pointed with ``RUNS``, and both write
targets are armed to stay inside this bundle, so a re-run cannot repeat the overwrite.

The pre-chain judge run is the counterfact: it reads the *committed* claims against this edited
tree, so "these edits required a re-sign" is a printed reading (``0 offline accepted``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
DRIVER = (
    REPO / "docs/evidence/C141/wave37-cboe-index-snapshots-20261019/probes" / "w39_run_chain.py"
)
PIN_TARGETS = (
    "opendata/data/providers/catalog.py",
    "opendata/data/domains.yaml",
)


def _load_driver() -> Any:  # noqa: ANN401  # the shipped chain driver is this probe's instrument
    spec = importlib.util.spec_from_file_location("w37_chain_driver", DRIVER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import chain driver {DRIVER}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w37_chain_driver"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS  # carriers land in this bundle
    module.PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.report.json"
    return module


def _write_targets(  # the driver object is this probe's instrument, not a product type
    chain: Any,  # noqa: ANN401
) -> tuple[dict[str, str], list[str]]:
    """Resolve every bundle-relative path the driver writes and prove none leaves this bundle.

    The first run of this probe re-pointed only ``RUNS``, so the driver's ``PRE_CHAIN_OUTPUT`` --
    bundle-relative, computed at import -- still named wave 37's carrier and overwrote it. Both
    targets are now re-pointed and armed here, because a re-point that misses one is the defect the
    arm is meant to catch.
    """
    targets = {
        "RUNS": str(Path(str(chain.RUNS))),
        "PRE_CHAIN_OUTPUT": str(REPO / str(chain.PRE_CHAIN_OUTPUT)),
    }
    bundle_root = str(RUNS)
    outside = sorted(name for name, path in targets.items() if not path.startswith(bundle_root))
    return targets, outside


def _pin_owner_face() -> dict[str, Any]:
    """Count merged claims pinning catalog.py or domains.yaml, split by owning provider."""
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
        print(f"w45_run_chain_exit=1 # driver would write outside {BUNDLE}", flush=True)
        raise SystemExit(f"driver write target outside this bundle: {outside}")
    radius = _pin_owner_face()
    print(f"pin_radius={radius}", flush=True)
    rc = int(chain.main())
    summary_path = RUNS / "chain_summary.txt"
    text = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
    header = (
        "# wave-38 re-sign chain (seeking_alpha::CalendarEarnings landed)\n"
        "# carried by "
        + " ".join([chain.PY, str(REPO / BUNDLE / "probes" / "w45_run_chain.py")])
        + "\n"
        + f"pin_radius={json.dumps(radius, ensure_ascii=False)}\n\n"
    )
    summary_path.write_text(header + text, encoding="utf-8")
    print(f"w45_run_chain_reused_driver={DRIVER.name} chain_returned={rc}", flush=True)
    print(f"w45_run_chain_exit={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
