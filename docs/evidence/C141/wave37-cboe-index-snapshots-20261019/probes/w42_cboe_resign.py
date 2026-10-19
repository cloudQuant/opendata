"""Re-run only cboe's shard after the A2 static plane caught a long line in a touched witness file.

The full-tree chain (``w39_run_chain.py``) had already landed plane 1 at 337/350 when
``a2_check --files`` returned ``FAIL ruff check`` on
``scripts/quality/iter2_witnesses/_parts/cboe/historical.py:18`` -- an E501 that predates this wave
but that this wave inherited by editing that docstring, and "touch it, meet it" makes it mine. The
wrap is a comment-only change, yet the file is a pinned input, so the claims that pin it went stale.

Why re-producing only cboe is the whole repair and not a shortcut: the merged claims were walked and
the two cboe witness shards are pinned by exactly 50 claims, all of them ``provider=cboe``. Witness
ownership is provider-atomic by design (``iter2_claims_fixtures`` docstring), so the other 1660
claims do not read these bytes and stay current. The domain, provider and contract files the other
providers pin were not touched again, so no second full-tree pass is owed.

This probe imports the shipped chain driver and calls its own ``run()`` and ``report_face()``, so
every carrier here carries the same argv-and-exit discipline as take 1. Take-1 carriers are left in
place rather than overwritten; the superseded reading stays beside the replacing one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C141/wave37-cboe-index-snapshots-20261019"
RUNS = REPO / BUNDLE / "runs"
DRIVER = REPO / BUNDLE / "probes" / "w39_run_chain.py"
PROVIDER = "cboe"
#: The pin population this wave re-measured before deciding a second full-tree pass was not owed.
PIN_TARGETS = (
    "scripts/quality/iter2_witnesses/_parts/cboe/historical.py",
    "scripts/quality/iter2_witnesses/_parts/cboe/snapshots.py",
)


def _load_driver() -> Any:  # noqa: ANN401  # the shipped chain driver is this probe's instrument
    spec = importlib.util.spec_from_file_location("w39_chain_driver", DRIVER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import chain driver {DRIVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["w39_chain_driver"] = module
    spec.loader.exec_module(module)
    return module


def _pin_owner_face() -> dict[str, int]:
    """Count the merged claims that pin either cboe witness shard, split by owning provider."""
    document = json.loads((REPO / "docs/evidence/C101/claims.merged.json").read_text("utf-8"))
    owners: dict[str, int] = {}
    for claim in document.get("claims") or []:
        paths: set[str] = set()
        for group in (claim.get("pins") or {}).values():
            for entry in group if isinstance(group, list) else [group]:
                if isinstance(entry, dict):
                    paths.add(str(entry.get("path") or entry.get("relative_path") or ""))
        if any(target in paths for target in PIN_TARGETS):
            owners[str(claim.get("provider"))] = owners.get(str(claim.get("provider")), 0) + 1
    owners["total_claims"] = len(document.get("claims") or [])
    return owners


def main() -> int:
    """Producer -> recorder -> merge (twice) -> judge for the one stale provider shard."""
    chain = _load_driver()
    RUNS.mkdir(parents=True, exist_ok=True)
    exits: dict[str, int] = {}
    owners = _pin_owner_face()
    pinned = sum(v for k, v in owners.items() if k != "total_claims")
    print(f"pin_owners={owners} pinned_claims={pinned} total_claims={owners['total_claims']}")

    shard = f"docs/evidence/C101/{PROVIDER}/claims.json"
    reviewed = f"docs/evidence/C101/{PROVIDER}/claims.reviewed.json"
    exits["producer_cboe.take2"] = chain.run(
        "producer_cboe.take2",
        ["scripts/quality/iter2_claims_producer.py", "--provider", PROVIDER, "--output", shard],
        "five-phase producer for cboe after the historical shard E501 wrap",
    )
    exits["recorder_cboe.take2"] = chain.run(
        "recorder_cboe.take2",
        [
            "scripts/quality/iter2_primary_review_recorder.py",
            "--claims",
            shard,
            "--output",
            reviewed,
            "--provider",
            PROVIDER,
        ]
        + chain.REVIEWER_ARGS,
        "primary reviews over cboe's regenerated shard",
    )
    exits["merge_take1.take2"] = chain.run(
        "merge_take1.take2",
        ["scripts/quality/iter2_claims_merge.py"],
        "merge every reviewed shard, take 2 of the wave",
    )
    take1_sha = chain.stable_sha(chain.REPO / "docs/evidence/C101/claims.merged.json")
    exits["merge_take2.take2"] = chain.run(
        "merge_take2.take2",
        ["scripts/quality/iter2_claims_merge.py"],
        "merge idempotency re-take",
    )
    take2_sha = chain.stable_sha(chain.REPO / "docs/evidence/C101/claims.merged.json")
    exits["judge.take2"] = chain.run(
        "judge.take2",
        [
            "scripts/quality/iter2_model_evidence.py",
            "--inventory",
            chain.INVENTORY,
            "--claims",
            "docs/evidence/C101/claims.merged.json",
            "--output",
            chain.JUDGE_OUTPUT,
        ],
        "plane-1 judge after the cboe-only re-sign",
    )
    face = chain.report_face("post_resign", chain.JUDGE_OUTPUT)
    nonzero = sorted(name for name, code in exits.items() if code != 0)
    summary = {
        "pin_owners": owners,
        "exits": exits,
        "merged_stable_sha_take2_first": take1_sha,
        "merged_stable_sha_take2_second": take2_sha,
        "merge_idempotent": take1_sha == take2_sha,
        "post_resign_reading": face,
        "nonzero_members": nonzero,
    }
    line = " ".join(f"{k}={v}" for k, v in summary.items())
    (RUNS / "chain_summary.take2.txt").write_text(
        "# wave-37 cboe-only re-sign summary (take 2)\n"
        "# argv: "
        + " ".join([chain.PY, str(REPO / BUNDLE / "probes" / "w42_cboe_resign.py")])
        + "\n\n"
        + json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        + "\n\n"
        + line
        + f"\n\nw42_cboe_resign_exit={0 if not nonzero else 1}\n",
        encoding="utf-8",
    )
    print(line)
    return 0 if not nonzero else 1


if __name__ == "__main__":
    sys.exit(main())
