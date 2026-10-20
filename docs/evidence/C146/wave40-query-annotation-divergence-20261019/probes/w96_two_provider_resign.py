"""Re-sign the two providers whose adapters wave 40 edited, and prove the radius was provider-local.

Wave 40 repaired two runtime annotation defects -- ``tradingeconomics``'s calendar query model and
``benzinga``'s ``BaseModel`` import -- and both adapter files are pinned inputs, so the claims that
pin them went stale. The measured radius is ten pin sites over exactly ten claims, five per
provider, so the whole repair is two provider re-signs and not a tree pass.

This probe does not re-implement the chain: it imports wave 37's driver, re-points ``BUNDLE`` /
``RUNS`` / ``PRE_CHAIN_OUTPUT`` into this bundle, refuses to start if a write target leaves it, and
reads pin freshness through wave 39's corrected reader (``after_sha256``, missing-field sites
counted separately) rather than re-deriving the digests.

Arms are two-sided: ``stale_before`` has to be non-zero or the repair was not needed / the reader is
blind, ``stale_after`` has to be zero or the re-sign missed a site, and the plane-1 reading has to
come back to 339 accepted with the identity set unchanged -- a re-sign that lands fewer rows is a
regression, not a repair.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C146/wave40-query-annotation-divergence-20261019"
RUNS = REPO / BUNDLE / "runs"
DRIVER = REPO / "docs/evidence/C141/wave37-cboe-index-snapshots-20261019/probes/w39_run_chain.py"
PIN_READER = (
    REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/probes/w76_cboe_resign.py"
)
PROVIDERS = ("benzinga", "tradingeconomics")
#: The two adapter files wave 40 edited; their claims are the ones that must go stale then fresh.
PIN_TARGETS = (
    "opendata/data/providers/benzinga/models/price_target.py",
    "opendata/data/providers/tradingeconomics/models/economic_calendar.py",
)
EXPECTED_ACCEPTED = 339
EXPECTED_IDENTITY_SET = "5168e1e723999312"


def _load(name: str, path: Path) -> Any:  # noqa: ANN401  # imported instruments carry the discipline
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import instrument {path}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_targets(chain: Any) -> tuple[dict[str, str], list[str]]:  # noqa: ANN401
    """Resolve every path the driver writes and prove none leaves this bundle."""
    targets = {
        "RUNS": str(chain.RUNS),
        "PRE_CHAIN_OUTPUT": str(REPO / str(chain.PRE_CHAIN_OUTPUT)),
    }
    outside = sorted(
        name
        for name, path in targets.items()
        if not path.startswith(str(RUNS)) and not path.startswith(str(REPO / "docs/evidence/C101"))
    )
    return targets, outside


def _pin_face(reader: Any) -> dict[str, Any]:  # noqa: ANN401
    reader.PIN_TARGETS = PIN_TARGETS
    face: dict[str, Any] = reader._stale_pin_face()
    return face


def main() -> int:
    """Producer -> recorder for both providers, merge twice, judge, and read pin freshness back."""
    chain = _load("w37_chain_driver_wave40", DRIVER)
    chain.BUNDLE = BUNDLE
    chain.RUNS = RUNS
    chain.PRE_CHAIN_OUTPUT = f"{BUNDLE}/runs/judge_pre_chain.wave40.report.json"
    reader = _load("w76_pin_reader_wave40", PIN_READER)
    RUNS.mkdir(parents=True, exist_ok=True)
    targets, outside = _write_targets(chain)
    before = _pin_face(reader)
    print(
        f"re_point bundle={chain.BUNDLE} write_targets={targets} outside_bundle={outside}",
        flush=True,
    )
    print(f"stale_face_before={json.dumps(before, sort_keys=True)}", flush=True)
    arms: dict[str, bool] = {}
    arms["A_stale_before_is_nonzero_and_provider_local"] = before[
        "stale_sites_total"
    ] > 0 and sorted(before["stale_sites_by_provider"]) == sorted(PROVIDERS)
    if outside:
        print(f"w96_resign_exit=1 # write_target_outside_bundle {outside}")
        return 1

    exits: dict[str, int] = {}
    for provider in PROVIDERS:
        shard = f"docs/evidence/C101/{provider}/claims.json"
        reviewed = f"docs/evidence/C101/{provider}/claims.reviewed.json"
        exits[f"producer_{provider}"] = chain.run(
            f"producer_{provider}",
            ["scripts/quality/iter2_claims_producer.py", "--provider", provider, "--output", shard],
            f"five-phase producer for {provider} after the wave-40 annotation repair",
        )
        exits[f"recorder_{provider}"] = chain.run(
            f"recorder_{provider}",
            [
                "scripts/quality/iter2_primary_review_recorder.py",
                "--claims",
                shard,
                "--output",
                reviewed,
                "--provider",
                provider,
            ]
            + chain.REVIEWER_ARGS,
            f"primary reviews over {provider}'s regenerated shard",
        )
    exits["merge_take1"] = chain.run(
        "merge_take1", ["scripts/quality/iter2_claims_merge.py"], "merge every reviewed shard"
    )
    first = chain.stable_sha(chain.REPO / "docs/evidence/C101/claims.merged.json")
    exits["merge_take2"] = chain.run(
        "merge_take2", ["scripts/quality/iter2_claims_merge.py"], "merge idempotency re-take"
    )
    second = chain.stable_sha(chain.REPO / "docs/evidence/C101/claims.merged.json")
    exits["judge"] = chain.run(
        "judge",
        [
            "scripts/quality/iter2_model_evidence.py",
            "--inventory",
            chain.INVENTORY,
            "--claims",
            "docs/evidence/C101/claims.merged.json",
            "--output",
            chain.JUDGE_OUTPUT,
        ],
        "plane-1 judge after the two-provider re-sign",
    )
    after = _pin_face(reader)
    face = chain.report_face("post_resign_wave40", chain.JUDGE_OUTPUT)
    report = json.loads((REPO / chain.JUDGE_OUTPUT).read_text(encoding="utf-8"))
    identity_set = str(report["inventory"]["identity_set_sha256"])
    nonzero = sorted(name for name, code in exits.items() if code != 0)
    arms["B_stale_after_is_zero"] = after["stale_sites_total"] == 0
    arms["C_merge_is_idempotent"] = first == second
    arms["D_every_member_exited_zero"] = nonzero == []
    arms["E_plane_one_back_at_339"] = face.get("post_resign_wave40_accepted") == EXPECTED_ACCEPTED
    arms["F_identity_set_unchanged"] = identity_set.startswith(EXPECTED_IDENTITY_SET)
    arms["G_no_pin_entries_without_digest"] = (
        before["pin_entries_without_digest"] == 0 and after["pin_entries_without_digest"] == 0
    )
    summary = {
        "stale_face_before": before,
        "stale_face_after": after,
        "arms": arms,
        "exits": exits,
        "merged_stable_sha_first": first,
        "merged_stable_sha_second": second,
        "post_resign_reading": face,
        "identity_set": identity_set[:16],
        "nonzero_members": nonzero,
    }
    passed = all(arms.values())
    text = json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    (RUNS / "chain_summary.wave40.txt").write_text(
        "# wave-40 two-provider re-sign summary\n"
        "# argv: "
        + " ".join([chain.PY, str(RUNS.parent / "probes" / "w96_two_provider_resign.py")])
        + "\n\n"
        + text
        + f"\n\nw96_two_provider_resign_exit={0 if passed else 1}\n",
        encoding="utf-8",
    )
    print(text, flush=True)
    print(f"w96_two_provider_resign_exit={0 if passed else 1}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
