"""Re-sign cboe only after the A2 static plane re-wrapped two pinned wave-39 files.

What happened: ``a2_check --files`` on the wave's product files found one ruff E501 in
``opendata/data/providers/cboe/models/futures_settlement_public.py`` and three in
``scripts/quality/iter2_witnesses/_parts/cboe/futures.py``, plus an I001 import order in
``opendata/data/models/__init__.py``. The fixes are whitespace and ordering only -- no assertion,
literal or code path changes -- but every one of those files except ``__init__.py`` is a *pinned
input*, so the chain that had already signed them (``w56_run_chain.py``) left their claims stale on
a comment line.

Why cboe-only is the whole repair, measured here rather than asserted: the walk of
``docs/evidence/C101/claims.merged.json`` inside this probe prints the stale-pin count grouped by
owning provider. If a single provider owns every stale pin, witness ownership (provider-atomic by
``iter2_claims_fixtures``) means the other shards never read these bytes and stay current, and no
second full-tree pass is owed. ``models/__init__.py`` is pinned by 0 claims, so the I001 fix cost
no re-sign at all. The same face is re-run after the chain and must read zero -- that pair, not a
sentence, is the proof.

Take-1 carriers stay in place: every carrier here is named ``*.take2`` and the write-target guard
refuses to start if the imported driver points anywhere outside this bundle.
"""

from __future__ import annotations

import copy
import hashlib
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
PROVIDER = "cboe"
#: The pin site the in-memory counterfact breaks; it is pinned by the most sites of the three.
FORGED_TARGET = "scripts/quality/iter2_witnesses/_parts/cboe/futures.py"
#: Files the lint pass rewrote after ``w56_run_chain.py`` had already signed them.
PIN_TARGETS = (
    "opendata/data/providers/cboe/models/futures_settlement_public.py",
    "scripts/quality/iter2_witnesses/_parts/cboe/futures.py",
    "opendata/data/models/__init__.py",
)


def _load_driver() -> Any:  # noqa: ANN401  # the shipped chain driver is this probe's instrument
    """Import wave 37's driver object and re-point every bundle-relative target into C145."""
    spec = importlib.util.spec_from_file_location("w37_chain_driver_take2", DRIVER)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import chain driver {DRIVER}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w37_chain_driver_take2"] = module
    spec.loader.exec_module(module)
    module.BUNDLE = BUNDLE
    module.RUNS = RUNS
    module.PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.take2.report.json"
    return module


def _write_targets(chain: Any) -> tuple[dict[str, str], list[str]]:  # noqa: ANN401
    """Resolve every path the driver writes and prove none leaves this bundle."""
    targets = {
        "RUNS": str(chain.RUNS),
        "PRE_CHAIN_OUTPUT": str(REPO / str(chain.PRE_CHAIN_OUTPUT)),
        "JUDGE_OUTPUT": str(REPO / str(chain.JUDGE_OUTPUT)),
    }
    outside = sorted(
        name
        for name, path in targets.items()
        if not path.startswith(str(RUNS)) and not path.startswith(str(REPO / "docs/evidence/C101"))
    )
    return targets, outside


def _live_pins(rel: str) -> str:
    """SHA-256 of the bytes on disk right now."""
    return hashlib.sha256((REPO / rel).read_bytes()).hexdigest()


def _count_stale(claims: list[dict[str, Any]], live: dict[str, str]) -> dict[str, Any]:
    """Count watched pin sites whose recorded digest differs from ``live``, by owning provider.

    The digest field is ``after_sha256`` (with ``before_sha256`` beside it) because that is what the
    producer writes and what the repo's own reader ``w75`` reads. Take 1 of this probe looked up
    ``sha256``/``digest``, found the empty string for every site, and reported all 71 sites of the
    watched paths as stale *before and after* the chain -- a face that could not have come out
    different, which is the alarm this rewrite turns into an arm (``pin_entries_without_digest``).
    """
    by_target = dict.fromkeys(PIN_TARGETS, 0)
    owners: dict[str, int] = {}
    stale_sites: dict[str, int] = {}
    stale_claims: dict[str, int] = {}
    groups: dict[str, int] = {}
    without_digest = 0
    for claim in claims:
        provider = str(claim.get("provider"))
        touched = False
        claim_stale = False
        for group_name, group in (claim.get("pins") or {}).items():
            entries = group if isinstance(group, list) else [group]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                rel = str(entry.get("path") or "")
                if rel not in live:
                    continue
                by_target[rel] += 1
                touched = True
                recorded = str(entry.get("after_sha256") or "")
                if not recorded:
                    without_digest += 1
                    continue
                if recorded != live[rel]:
                    key = f"{provider}:{group_name}"
                    groups[key] = groups.get(key, 0) + 1
                    stale_sites[provider] = stale_sites.get(provider, 0) + 1
                    claim_stale = True
        if touched:
            owners[provider] = owners.get(provider, 0) + 1
        if claim_stale:
            stale_claims[provider] = stale_claims.get(provider, 0) + 1
    return {
        "total_claims": len(claims),
        "pin_sites_by_target": by_target,
        "claims_touching_targets": owners,
        "stale_sites_by_provider": stale_sites,
        "stale_claims_by_provider": stale_claims,
        "stale_sites_total": sum(stale_sites.values()),
        "stale_groups": groups,
        "pin_entries_without_digest": without_digest,
    }


def _claims() -> list[dict[str, Any]]:
    """The merged claim set as written by the chain."""
    document = json.loads((REPO / "docs/evidence/C101/claims.merged.json").read_text("utf-8"))
    return list(document.get("claims") or [])


def _live() -> dict[str, str]:
    return {target: _live_pins(target) for target in PIN_TARGETS}


def _stale_pin_face() -> dict[str, Any]:
    """Report which pins of these paths no longer match the disk, grouped by owning provider."""
    return _count_stale(_claims(), _live())


def _forged_face() -> dict[str, Any]:
    """Break one shard digest in an in-memory copy and re-count, so the real zero is a measurement.

    The repair this probe performed consumed the only live defect it could have pointed at, so the
    "can differ" half has to be forged rather than remembered: the same predicate over the same
    claim set with a single ``after_sha256`` replaced must report exactly one cboe stale site.
    """
    forged = copy.deepcopy(_claims())
    hit = False
    for claim in forged:
        for group in (claim.get("pins") or {}).values():
            entries = group if isinstance(group, list) else [group]
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("path") or "") == FORGED_TARGET:
                    entry["after_sha256"] = "0" * 64
                    hit = True
                    break
            if hit:
                break
        if hit:
            break
    face = _count_stale(forged, _live())
    face["forge_applied"] = hit
    return face


def main() -> int:
    """Producer -> recorder -> merge (twice) -> judge for the one stale provider shard."""
    chain = _load_driver()
    RUNS.mkdir(parents=True, exist_ok=True)
    targets, outside = _write_targets(chain)
    before = _stale_pin_face()
    forged = _forged_face()
    stale_providers = sorted(before["stale_sites_by_provider"])
    foreign = sorted(set(stale_providers) - {PROVIDER})
    print(
        f"re_point bundle={chain.BUNDLE} write_targets={targets} outside_bundle={outside}",
        flush=True,
    )
    print(f"stale_face_before={json.dumps(before, sort_keys=True)}", flush=True)
    print(f"stale_face_forged={json.dumps(forged, sort_keys=True)}", flush=True)
    if outside:
        print(f"w76_cboe_resign_exit=1 # write_target_outside_bundle {outside}")
        return 1
    if foreign:
        print(f"w76_cboe_resign_exit=1 # stale_owner_outside_cboe {foreign}")
        return 1

    exits: dict[str, int] = {}
    shard = f"docs/evidence/C101/{PROVIDER}/claims.json"
    reviewed = f"docs/evidence/C101/{PROVIDER}/claims.reviewed.json"
    exits[f"producer_{PROVIDER}.take2"] = chain.run(
        f"producer_{PROVIDER}.take2",
        ["scripts/quality/iter2_claims_producer.py", "--provider", PROVIDER, "--output", shard],
        f"five-phase producer for {PROVIDER} after the lint re-wrap",
    )
    exits[f"recorder_{PROVIDER}.take2"] = chain.run(
        f"recorder_{PROVIDER}.take2",
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
        f"primary reviews over {PROVIDER}'s regenerated shard",
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
    after = _stale_pin_face()
    face = chain.report_face("post_resign_take2", chain.JUDGE_OUTPUT)
    nonzero = sorted(name for name, code in exits.items() if code != 0)
    arms = {
        # The field the face reads has to exist on every watched pin, or the count below is a
        # measurement of nothing. Take 1 failed exactly here and reported 71 phantom stales.
        "A_every_watched_pin_carries_after_sha256": all(
            source["pin_entries_without_digest"] == 0 for source in (before, forged, after)
        ),
        "B_forged_digest_reads_stale": forged["stale_sites_total"] >= 1
        and forged["stale_sites_by_provider"].get(PROVIDER, 0) >= 1,
        "C_real_face_reads_zero_stale": after["stale_sites_total"] == 0,
        "D_merge_is_idempotent": take1_sha == take2_sha,
        "E_all_five_members_exited_zero": nonzero == [],
    }
    summary = {
        "stale_face_before": before,
        "stale_face_forged": forged,
        "stale_face_after": after,
        "arms": arms,
        "exits": exits,
        "merged_stable_sha_take2_first": take1_sha,
        "merged_stable_sha_take2_second": take2_sha,
        "merge_idempotent": take1_sha == take2_sha,
        "post_resign_reading": face,
        "nonzero_members": nonzero,
        "red_arms": sorted(name for name, ok in arms.items() if not ok),
    }
    line = " ".join(f"{k}={v}" for k, v in summary.items())
    passed = all(arms.values()) and not nonzero
    (RUNS / "chain_summary.take2.txt").write_text(
        "# wave-39 cboe-only re-sign summary (take 2)\n"
        "# argv: "
        + " ".join([chain.PY, str(RUNS.parent / "probes" / "w76_cboe_resign.py")])
        + "\n\n"
        + json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        + "\n\n"
        + line
        + f"\n\nw76_cboe_resign_exit={0 if passed else 1}\n",
        encoding="utf-8",
    )
    print(line, flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
