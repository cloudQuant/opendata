"""Falsification probe for the plane-1 reading that wave 21 produces.

Three arms, each printing a verdict line:

* ``A_delta_census`` -- the judge report on disk against ``HEAD``'s report, joined on
  ``model_identity``. The claim under test is a *set* claim, so the census prints both directions:
  ``ADDED`` and ``REMOVED``. A +1 that arrived with a label flip somewhere else is exactly what the
  ``REMOVED`` list shows, and printing only the new total would hide it. The population is the
  judge's own ``models`` array (350), never a hand-counted subset, and the freeze signal
  ``inventory.expected_identity_set_sha256`` is compared between the two generations.
* ``B_pin_census`` -- the new shard must be the file the accepted claims actually pin. Every sec
  claim in the merged JSON carries a ``probe`` group; the census counts claims whose ``probe``
  group names the shard path, requires one distinct digest across them, and requires that digest to
  equal ``sha256`` of the shard as it sits on disk right now. A stale re-sign -- claims pinning
  bytes that no longer exist -- is the failure mode this arm is for, and it also goes red if the
  shard is edited after the producer ran.
* ``C_phase_floor`` -- the +1 must be earned by the five phase claims plus one review, not by the
  judge's display arithmetic. The merged carrier is copied whole (``claims`` and ``primary_reviews``
  are siblings of one document, and a carrier re-serialized with only ``claims`` in it reads 0
  accepted -- the first draft of this arm did exactly that and its own red is why the two sub-arms
  below exist). The repo's own judge (``iter2_model_evidence.py`` via subprocess, never a
  re-implemented judge) then re-runs on three carriers: the control, one with the row's five claims
  dropped, and one with only the row's ``ACCEPT`` review dropped. Expected ``258 / 257 / 257``: the
  row needs both halves of the judge's stated acceptance rule, and a claims-only PASS is candidate.
  The temporary files are written under a scratch directory and removed by this probe; the committed
  evidence is the printed reading plus this file.

Run from the repository root with ``PYTHONPATH=. python`` followed by this path.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess  # nosec B404  # one run(): list argv only, shell never enabled
import sys
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT))

JUDGE: Final[str] = "scripts/quality/iter2_model_evidence.py"
INVENTORY: Final[str] = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
MERGED: Final[str] = "docs/evidence/C101/claims.merged.json"
REPORT: Final[str] = "docs/evidence/C101/judge/claims.merged.report.json"
SHARD: Final[str] = "scripts/quality/iter2_witnesses/_parts/sec/insider_rows.py"
PROVIDER: Final[str] = "sec"
MODEL: Final[str] = "InsiderTrading"
IDENTITY: Final[str] = f"{PROVIDER}::{MODEL}"
OFFLINE_ACCEPTED: Final[str] = "OFFLINE_ACCEPTED"


def _git_head_json(relative: str) -> dict[str, Any]:
    """Read one JSON file exactly as it exists in HEAD, not as it exists in my memory."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH; this arm reads HEAD rather than a hand-typed copy")
    raw = subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own git read
        [git, "show", f"HEAD:{relative}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    result: dict[str, Any] = json.loads(raw)
    return result


def _accepted_map(report: dict[str, Any]) -> dict[str, str]:
    return {str(model["model_identity"]): str(model.get("status")) for model in report["models"]}


def _claims(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = document["claims"]
    return result


def _is_row(claim: dict[str, Any]) -> bool:
    return claim.get("provider") == PROVIDER and claim.get("upstream_model") == MODEL


def _verdict(arm: str, note: str, ok: bool) -> bool:
    print(f"{arm}: {'PASS' if ok else 'FAIL'} {note}")
    return ok


def arm_delta_census() -> bool:
    """Compare this generation's judge report with HEAD's, in both directions."""
    new = json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))
    old = _git_head_json(REPORT)
    new_map, old_map = _accepted_map(new), _accepted_map(old)
    added = sorted(
        identity
        for identity, status in new_map.items()
        if status == OFFLINE_ACCEPTED and old_map.get(identity) != OFFLINE_ACCEPTED
    )
    removed = sorted(
        identity
        for identity, status in old_map.items()
        if status == OFFLINE_ACCEPTED and new_map.get(identity) != OFFLINE_ACCEPTED
    )
    n_new = int(new["summary"]["offline_acceptance_count"])
    n_old = int(old["summary"]["offline_acceptance_count"])
    set_new = str(new["inventory"]["expected_identity_set_sha256"])
    set_old = str(old["inventory"]["expected_identity_set_sha256"])
    ok = (
        added == [IDENTITY]
        and removed == []
        and n_new == n_old + 1
        and len(new_map) == len(old_map) == 350
        and set_new == set_old
        and new_map.get(IDENTITY) == OFFLINE_ACCEPTED
        and old_map.get(IDENTITY) != OFFLINE_ACCEPTED
    )
    return _verdict(
        "A_delta_census",
        f"accepted_old={n_old} accepted_new={n_new} ADDED={added} REMOVED={removed}"
        f" populations={len(old_map)}/{len(new_map)} identity_set={set_new[:16]}"
        f" unchanged={set_new == set_old} {IDENTITY}_old={old_map.get(IDENTITY)}",
        ok,
    )


def arm_pin_census() -> bool:
    """The shard on disk must be the bytes every accepted sec claim pins."""
    claims = _claims(json.loads((REPO_ROOT / MERGED).read_text(encoding="utf-8")))
    sec = [claim for claim in claims if claim.get("provider") == PROVIDER]
    digests = {
        str(entry["after_sha256"])
        for claim in sec
        for entry in (claim["pins"] or {}).get("probe", [])
        if entry["path"] == SHARD
    }

    def pins_shard(claim: dict[str, Any]) -> bool:
        return any(entry["path"] == SHARD for entry in (claim["pins"] or {}).get("probe", []))

    pinning = sum(1 for claim in sec if pins_shard(claim))
    on_disk = hashlib.sha256((REPO_ROOT / SHARD).read_bytes()).hexdigest()
    statuses = sorted({str(claim["status"]) for claim in sec})
    ok = (
        len(digests) == 1
        and digests == {on_disk}
        and pinning == len(sec) > 0
        and len(sec) == 120
        and statuses == ["PASS"]
    )
    return _verdict(
        "B_pin_census",
        f"sec_claims={len(sec)} claims_pinning_shard={pinning}"
        f" distinct_shard_digests={len(digests)} on_disk_sha256={on_disk[:16]}"
        f" digest_matches_disk={digests == {on_disk}} sec_statuses={statuses}",
        ok,
    )


def arm_phase_floor(scratch: Path) -> bool:
    """Re-run the repo's own judge on three carriers; the row needs claims AND a review."""
    document = json.loads((REPO_ROOT / MERGED).read_text(encoding="utf-8"))
    row_claims = [claim for claim in document["claims"] if _is_row(claim)]
    row_reviews = [review for review in document["primary_reviews"] if _is_row(review)]
    if len(row_claims) != 5 or len(row_reviews) != 1:
        return _verdict(
            "C_phase_floor",
            f"expected 5 claims and 1 review for {IDENTITY},"
            f" found {len(row_claims)}/{len(row_reviews)}",
            False,
        )
    control_count = int(
        json.loads((REPO_ROOT / REPORT).read_text(encoding="utf-8"))["summary"][
            "offline_acceptance_count"
        ]
    )
    variants = {
        "control": document,
        "no_claims": {
            **document,
            "claims": [c for c in copy.deepcopy(document["claims"]) if not _is_row(c)],
        },
        "no_review": {
            **document,
            "primary_reviews": [
                r for r in copy.deepcopy(document["primary_reviews"]) if not _is_row(r)
            ],
        },
    }
    scratch.mkdir(parents=True, exist_ok=True)
    readings: dict[str, int] = {}
    for label, carrier in variants.items():
        claims_path = scratch / f"claims.{label}.json"
        report_path = scratch / f"report.{label}.json"
        claims_path.write_text(
            json.dumps(carrier, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own judge call
            [
                sys.executable,
                JUDGE,
                "--inventory",
                INVENTORY,
                "--claims",
                str(claims_path),
                "--output",
                str(report_path),
            ],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        readings[f"accepted_{label}"] = int(report["summary"]["offline_acceptance_count"])
        readings[f"reviews_{label}"] = int(report["summary"]["primary_review_count"])
        readings[f"not_accepted_{label}"] = int(
            _accepted_map(report).get(IDENTITY) != OFFLINE_ACCEPTED
        )
        readings[f"population_{label}"] = int(report["summary"]["identity_count"])
    for path in list(scratch.glob("claims.*.json")) + list(scratch.glob("report.*.json")):
        path.unlink()
    ok = (
        readings["accepted_control"] == control_count
        and readings["accepted_no_claims"] == control_count - 1
        and readings["accepted_no_review"] == control_count - 1
        and readings["not_accepted_control"] == 0
        and readings["not_accepted_no_claims"] == 1
        and readings["not_accepted_no_review"] == 1
        and readings["reviews_no_review"] == readings["reviews_control"] - 1
        and readings["population_control"] == readings["population_no_claims"] == 350
        and readings["population_no_review"] == 350
    )
    return _verdict(
        "C_phase_floor",
        f"judge_control={readings['accepted_control']}"
        f" judge_without_claims={readings['accepted_no_claims']}"
        f" judge_without_review={readings['accepted_no_review']}"
        f" reviews={readings['reviews_control']}->{readings['reviews_no_review']}"
        f" row_accepted(control/no_claims/no_review)="
        f"{1 - readings['not_accepted_control']}/"
        f"{1 - readings['not_accepted_no_claims']}/"
        f"{1 - readings['not_accepted_no_review']}"
        f" populations={readings['population_control']}"
        f"/{readings['population_no_claims']}/{readings['population_no_review']}",
        ok,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the three arms and exit 0 only when every one of them passes."""
    parser = argparse.ArgumentParser(description="plane-1 delta and phase-floor census")
    parser.add_argument(
        "--scratch-dir",
        default=".w21-scratch/counterfact",
        help="where arm C writes its temporary claims and report files",
    )
    args = parser.parse_args(argv)
    scratch = REPO_ROOT / args.scratch_dir
    results = [arm_delta_census(), arm_pin_census(), arm_phase_floor(scratch)]
    if scratch.is_dir():
        shutil.rmtree(scratch)
    passed = sum(1 for ok in results if ok)
    failed = len(results) - passed
    print(f"plane1_arms={len(results)} plane1_passed={passed} plane1_failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
