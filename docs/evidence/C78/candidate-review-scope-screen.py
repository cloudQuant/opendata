"""What actually blocks a rebuild of the provider source-review bundle over today's census.

AC-10|03 was red this round for a stale artifact, and the natural next move is "rebuild it over the
12-package / 474-file census". This harness measures whether that move is available. Three readings,
all with the repo's own primitives (``measure_tree`` / ``measure_baseline`` / ``screen_blocks`` /
``build_bundle`` -- nothing reimplemented):

1. the review population as the builder classifies it, including the files that sit under
   ``_vendor/`` but cannot be proven verbatim (``upstream.lock`` marks them ``manual_edits``), so
   they are *not* excluded from structural comparison;
2. the candidate rows the shipped thresholds actually produce over today's tree with the pinned
   five-provider baseline, printed with both sides' identity so a reviewer can act on them;
3. the rebuild itself, attempted against a throwaway output directory. It is expected to refuse;
4. a control stub with the wrong schema, which must be rejected by ``load_findings`` -- proving the
   refusal in (3) came from the candidate guard and not from my stub being malformed.

Nothing here certifies anything. The bundle in (3) is deliberately *not* written: the point is the
refusal. The reviewer block in the stub findings is copied from the archived C65 artifact's own
reviewer object so the call reaches the candidate guard instead of dying on a schema check, and the
run is labelled ``GUARD PROBE -- CERTIFIES NOTHING``.

Re-run: ``python3.11 -u docs/evidence/C78/candidate-review-scope-screen.py``
Face: ``candidate-review-scope-screen.txt``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from scripts.quality import provider_source_review_evidence as review  # noqa: E402
from scripts.quality import provider_source_review_evidence_build as build  # noqa: E402

CHECKOUT = Path(build.DEFAULT_OPENBB_CHECKOUT)
PINNED = frozenset(review.EXPECTED_BASELINE_PROVIDERS)


def census() -> dict[str, Any]:
    """The builder's own classification of the current tree."""
    measurement = build.measure_tree(REPO)
    edited_vendor = {
        rel: proof for rel, proof in sorted(measurement.unproven.items()) if "/_vendor/" in rel
    }
    return {
        "total_py": len(measurement.records),
        "hand_written_py": measurement.hand_written_files,
        "lock_proven_verbatim_py": measurement.vendored_files,
        "local_blocks_over_floor": len(measurement.local_blocks),
        "packages": len(measurement.provider_files),
        "vendor_paths_classified_hand_written": edited_vendor,
    }


def candidates() -> dict[str, Any]:
    """Run the shipped screen over today's tree against the pinned five-provider baseline."""
    measurement = build.measure_tree(REPO)
    started = time.monotonic()
    baseline = build.measure_baseline(CHECKOUT, review.EXPECTED_OPENBB_COMMIT)
    pairs, rows = build.screen_blocks(measurement.local_blocks, baseline["blocks"])
    return {
        "baseline_providers": sorted(PINNED),
        "baseline_head": baseline["head"],
        "baseline_per_provider": baseline["per_provider"],
        "baseline_py_files": sum(baseline["per_provider"].values()),
        "baseline_blocks": len(baseline["blocks"]),
        "measure_baseline_seconds": round(time.monotonic() - started, 1),
        "nearest_pairs": len(pairs),
        "candidate_count": len(rows),
        "candidate_rows": rows,
    }


def rebuild_attempt() -> dict[str, Any]:
    """Ask ``build_bundle`` to write a round over today's tree and report what it says."""
    archived = json.loads((REPO / review.SOURCE_REVIEW_REL).read_text())
    stub = {
        "schema": build.FINDINGS_SCHEMA,
        "reviewer": dict(archived.get("reviewer") or {}),
        "files": [],
        "scope": "GUARD PROBE -- CERTIFIES NOTHING (no review act was performed for this call)",
    }
    with tempfile.TemporaryDirectory(prefix="c78-bundle-") as tmp:
        findings_path = Path(tmp) / "findings.json"
        out_dir = Path(tmp) / "out"
        findings_path.write_text(json.dumps(stub, ensure_ascii=False), encoding="utf-8")
        before = sorted(p.name for p in out_dir.rglob("*")) if out_dir.exists() else []
        try:
            build.build_bundle(
                REPO,
                findings_path=findings_path,
                out_dir=out_dir,
                openbb_checkout=CHECKOUT,
                self_verify=False,
            )
        except build.BuildError as exc:
            after = sorted(p.name for p in out_dir.rglob("*")) if out_dir.exists() else []
            return {
                "raised": type(exc).__name__,
                "message": str(exc),
                "nothing_written": before == after and not after,
            }
        return {"raised": "none", "message": "the bundle was written", "nothing_written": False}


def control_rejects_earlier() -> dict[str, Any]:
    """Show the guard in (3) is the candidate guard, not a generic failure of my stub.

    ``load_findings`` runs before the tree is measured, so a stub with the wrong schema must die on
    the schema line. If it does, and arm (3) died on a candidate count instead, then arm (3)'s stub
    was structurally accepted and the only thing it lacked was reviewed candidates.
    """
    with tempfile.TemporaryDirectory(prefix="c78-control-") as tmp:
        findings_path = Path(tmp) / "findings.json"
        findings_path.write_text(
            json.dumps({"schema": "not-the-findings-schema", "reviewer": {}}), encoding="utf-8"
        )
        try:
            build.load_findings(findings_path)
        except build.BuildError as exc:
            return {"raised": type(exc).__name__, "message": str(exc)}
        return {"raised": "none", "message": "the invalid stub was accepted"}


def main() -> None:
    """Print the three readings and the verdict the repo's own guard produces."""
    print("GUARD PROBE -- CERTIFIES NOTHING: this run refuses to write a bundle, by design.")
    print(f"repo HEAD: {REPO}")
    print(f"pinned baseline providers = {sorted(PINNED)}")
    print(
        f"builder candidate thresholds = dice>={build.CANDIDATE_DICE} "
        f"node_ratio>={build.CANDIDATE_NODE_RATIO} ordered>={build.CANDIDATE_ORDERED_RATIO}"
    )

    pop = census()
    print("\n=== 1. review population (builder classification, current tree) ===")
    for key in (
        "packages",
        "total_py",
        "hand_written_py",
        "lock_proven_verbatim_py",
        "local_blocks_over_floor",
    ):
        print(f"  {key} = {pop[key]}")
    edited = pop["vendor_paths_classified_hand_written"]
    print(
        f"  under _vendor/ but NOT lock-proven verbatim = {len(edited)} 个（按上游 lock 的 "
        f"manual_edits 标记进入结构对照面）"
    )
    for rel, proof in edited.items():
        print(f"    {rel}\n       {proof}")

    cand = candidates()
    print("\n=== 2. candidates the shipped thresholds produce over today's tree ===")
    for key in (
        "baseline_providers",
        "baseline_head",
        "baseline_per_provider",
        "baseline_py_files",
        "baseline_blocks",
        "measure_baseline_seconds",
        "nearest_pairs",
        "candidate_count",
    ):
        print(f"  {key} = {cand[key]}")
    for row in cand["candidate_rows"]:
        print(
            f"  score={row['score']:.4f} "
            f"local={row['local_path']}::{row['local_span']} "
            f"<- baseline={row['baseline_path']}::{row['baseline_span']}"
        )

    attempt = rebuild_attempt()
    print("\n=== 3. rebuild attempt (write refused?) ===")
    print(f"  raised = {attempt['raised']}")
    print(f"  nothing written to the output dir = {attempt['nothing_written']}")
    print(f"  message = {attempt['message'][:600]}")

    control = control_rejects_earlier()
    print("\n=== 4. control: the schema guard fires before the candidate guard ===")
    print(f"  raised = {control['raised']}")
    print(f"  message = {control['message'][:200]}")

    # The verdict asks two things of the measured numbers: the guard named a candidate count, and
    # that count is section 2's own measurement -- not a number this harness types into a sentence.
    guard_names_measured_count = (
        f"{cand['candidate_count']} similarity candidates" in attempt["message"]
    )
    verdict = (
        "REBUILD IS BLOCKED BY REAL FINDINGS"
        if attempt["raised"] == "BuildError"
        and attempt["nothing_written"]
        and guard_names_measured_count
        and control["raised"] == "BuildError"
        else "REBUILD PATH IS OPEN -- the candidate guard did not fire as expected"
    )
    print(f"\nGUARD NAMES SECTION-2 COUNT: {guard_names_measured_count}")
    print(f"VERDICT : {verdict}")
    print(
        f"COUNTS  : candidates={cand['candidate_count']} "
        f"hand_written={pop['hand_written_py']} lock_proven={pop['lock_proven_verbatim_py']} "
        f"total={pop['total_py']} edited_vendor={len(edited)}"
    )


if __name__ == "__main__":
    main()
