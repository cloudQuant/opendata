"""Is AC-10|03's "matched scope" able to see the five newest providers? Measure both arms.

The pinned OpenBB baseline in ``scripts/quality/provider_source_review_evidence.py`` is
``EXPECTED_BASELINE_PROVIDERS = {ecb, fred, imf, oecd, yfinance}``. The repo now registers twelve
self-developed provider packages, five of them (``bls``, ``cboe``, ``federal_reserve``, ``fmp``,
``sec``) whose OpenBB counterpart sits unused in the same pinned checkout. So for those five the
similarity screen compares our blocks against a baseline that cannot contain their counterpart at
all, and a verdict of ``NO_COMPLEX_COPY_OBSERVED_IN_MATCHED_SCOPE`` over an empty matched scope is
a zero without a denominator.

This harness measures the two arms with the repo's OWN primitives (``measure_tree``,
``measure_baseline``, ``screen_blocks`` -- nothing is reimplemented), differing only in the
baseline provider set:

* arm 5 -- the pinned set as shipped,
* arm 10 -- the pinned set plus the five counterparts that exist on disk,
* clone control -- one of our own blocks injected into the baseline as a fake upstream file, which
  must come back as the top pair with score 1.0. Without it, "zero candidates" in arm 10 would be
  indistinguishable from a screen that cannot fire.

Re-run: ``python3.11 -u docs/evidence/C77/baseline-scope-widening-screen.py`` (``-u`` so the arms
reach disk before the control section runs). Read-only: the OpenBB checkout is opened for reading
and the module attribute is restored in ``finally``; the repo tree is not written.
Face: ``baseline-scope-widening-screen.txt``.

The first run crashed before the control printed -- ``KeyError: 'baseline_path'`` inside the shipped
``_pair_sort_key`` -- because its control path sat outside ``openbb_platform/providers``, so
``Block.is_baseline`` filed the injected clone as a *local* block and the pair row had no baseline
side to sort on. The arms printed normally there (521 local blocks; 8 candidates over the pinned
baseline, 14 over the widened one); the clone control is what the fix bought, and it is the only
thing that makes either candidate count mean anything.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scripts.quality import provider_source_review_evidence as review  # noqa: E402
from scripts.quality import provider_source_review_evidence_build as build  # noqa: E402

CHECKOUT = Path(build.DEFAULT_OPENBB_CHECKOUT)
PINNED = frozenset(review.EXPECTED_BASELINE_PROVIDERS)
NEW_PROVISIONS = frozenset({"bls", "cboe", "federal_reserve", "fmp", "sec"})
#: ``Block.is_baseline`` is derived from this prefix alone, so a control path outside it is filed as
#: a *local* side and the pair row loses ``baseline_path`` -- which is exactly how the first version
#: of this harness died (KeyError in ``_pair_sort_key``). The control has to look upstream to the
#: shipped code, not to the harness.
CONTROL_PATH = f"{build.OPENBB_PROVIDERS_REL}/control/openbb_control/models/clone_control.py"


def screen_with(local_blocks: list[Any], providers: frozenset[str]) -> dict[str, Any]:
    """Run the shipped screen with ``providers`` as the baseline set, then restore the constant."""
    original = review.EXPECTED_BASELINE_PROVIDERS
    try:
        review.EXPECTED_BASELINE_PROVIDERS = providers
        baseline = build.measure_baseline(CHECKOUT, review.EXPECTED_OPENBB_COMMIT)
    finally:
        review.EXPECTED_BASELINE_PROVIDERS = original
    pairs, candidates = build.screen_blocks(local_blocks, baseline["blocks"])
    return {
        "providers": sorted(providers),
        "baseline_py_files": baseline.get("provider_file_counts"),
        "baseline_blocks": len(baseline["blocks"]),
        "pairs": len(pairs),
        "candidates": len(candidates),
        "top": pairs[:8],
        "candidate_rows": candidates,
        "_blocks": baseline["blocks"],
    }


def package_of(rel_path: str) -> str:
    """The provider package a repo-relative source path belongs to."""
    parts = Path(rel_path).parts
    return parts[3] if len(parts) > 3 else "?"


def clone_control(local_blocks: list[Any], baseline_blocks: list[Any]) -> dict[str, Any]:
    """Give the baseline one byte-identical copy of our biggest block and demand it be caught."""
    victim = max(local_blocks, key=lambda block: block.node_count)
    clone = dataclasses.replace(
        copy.deepcopy(victim),
        path=CONTROL_PATH,
        function=f"control_{victim.function}",
    )
    pairs, candidates = build.screen_blocks(local_blocks, [*baseline_blocks, clone])
    caught = [row for row in pairs if row.get("baseline_path") == CONTROL_PATH]
    best = caught[0] if caught else None
    return {
        "victim_local": victim.path,
        "victim_function": victim.function,
        "victim_nodes": victim.node_count,
        "control_side_of_row": "baseline" if best is not None else None,
        "control_ranked_first": bool(pairs and pairs[0].get("baseline_path") == CONTROL_PATH),
        "control_score": best["score"] if best else None,
        "control_is_candidate": any(r.get("baseline_path") == CONTROL_PATH for r in candidates),
        "n_baseline_blocks": len(baseline_blocks) + 1,
    }


def thresholds() -> dict[str, Any]:
    """Print the screen's own cut-offs, because "matched scope" means nothing without them."""
    return {
        name: getattr(build, name)
        for name in (
            "PREFILTER_NODE_RATIO",
            "PREFILTER_DICE",
            "CANDIDATE_ORDERED_RATIO",
            "CANDIDATE_DICE",
            "CANDIDATE_NODE_RATIO",
        )
        if hasattr(build, name)
    }


def main() -> None:
    """Print both arms, the per-package matched scope, and the clone control."""
    started = time.monotonic()
    measurement = build.measure_tree(REPO)
    local_blocks = measurement.local_blocks
    print(f"local blocks screened: {len(local_blocks)}")
    per_pkg: dict[str, int] = {}
    for block in local_blocks:
        per_pkg[package_of(block.path)] = per_pkg.get(package_of(block.path), 0) + 1
    print("local blocks per package: " + json.dumps(per_pkg, sort_keys=True))
    print("screen cut-offs: " + json.dumps(thresholds(), sort_keys=True))
    print(f"measure_tree took {time.monotonic() - started:.1f}s")

    arms = {
        "arm5": screen_with(local_blocks, PINNED),
        "arm10": screen_with(local_blocks, PINNED | NEW_PROVISIONS),
    }
    for name, arm in arms.items():
        print(
            f"\n=== {name}: providers={arm['providers']} "
            f"baseline_blocks={arm['baseline_blocks']} pairs={arm['pairs']} "
            f"candidates={arm['candidates']}"
        )
        for row in arm["top"]:
            print(
                f"  score={row['score']:.4f} local={row['local_path']}"
                f"::{row['local_function']} <- baseline={row['baseline_path']}"
            )

    matched5 = {row["local_path"] for row in arms["arm5"]["top"]}
    newly = [
        row
        for row in arms["arm10"]["top"]
        if package_of(row["local_path"]) in NEW_PROVISIONS and row["local_path"] not in matched5
    ]
    print(f"\nnewly-matched rows inside the five added packages: {len(newly)}")
    for row in newly:
        print(
            f"  score={row['score']:.4f} local={row['local_path']}::{row['local_function']} "
            f"<- baseline={row['baseline_path']}::{row['baseline_function']}"
        )

    control = clone_control(local_blocks, arms["arm10"]["_blocks"])
    print("\n=== clone control (a byte-identical copy of our own block, filed as upstream) ===")
    print(json.dumps(control, indent=2, sort_keys=True))
    top_without_clone = arms["arm10"]["top"][0]["score"] if arms["arm10"]["top"] else None
    print(f"  contrast: best real nearest in arm10 scores {top_without_clone}")
    print(f"total harness time: {time.monotonic() - started:.1f}s")
    verdict = control["control_ranked_first"] and control["control_score"] == 1.0
    print(f"CONTROL FIRED: {verdict}")
    if not verdict:
        print("REFUTE: the screen did not catch its own injected clone; a zero here proves nothing")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
