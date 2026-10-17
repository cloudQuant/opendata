"""C126 §9 · what decides a status bucket, measured by the repo's own judge on synthetic copies.

The prediction in `face.md` §9 moves two buckets (`INCOMPLETE` 9 → 3, `UNREVIEWED` 83 → 81), so this
probe measures the rule that makes them move instead of asserting it: `iter2_model_evidence.py`
assigns `UNREVIEWED` to an identity with NO claims at all (line 1181-1182) and `INCOMPLETE` to one
whose claims exist but are not all `CANDIDATE` (1183-1193).  Arm B deletes the six target rows'
BLOCKED claims and shows they re-bucket into `UNREVIEWED`; arm C flips one genuinely accepted row's
decision inside the merged document and shows the judge refuses it, because that field is bound by
digest to the signed review file on disk.  Both write synthetic claims documents into a scratch root
and hand them to the judge as `--claims`; nothing under `docs/evidence/C101/` is touched.
"""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
JUDGE = REPO_ROOT / "scripts/quality/iter2_model_evidence.py"
INVENTORY = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
MERGED = REPO_ROOT / "docs/evidence/C101/claims.merged.json"
# the eight identities §9 names, split by the shape the §6 residue census printed
BLOCKED_CLAIM_ROWS = (
    "cboe::FuturesCurve",
    "cboe::IndexSnapshots",
    "econdb::CountryProfile",
    "econdb::ExportDestinations",
    "federal_reserve::FomcDocuments",
    "federal_reserve::YieldCurve",
)
ZERO_CLAIM_ROWS = ("eia::PetroleumStatusReport", "eia::ShortTermEnergyOutlook")


def _identity(row: dict[str, Any]) -> str:
    return f"{row.get('provider')}::{row.get('upstream_model')}"


def _verdict(arm: str, note: str, ok: bool) -> bool:
    print(f"{arm}: {'PASS' if ok else 'FAIL'} {note}")
    return ok


def _run_judge(claims_path: Path, out_dir: Path) -> dict[str, Any]:
    """Invoke the repo's judge — never a re-implementation of its rule."""
    report_path = out_dir / f"{claims_path.stem}.report.json"
    command = [
        sys.executable,
        str(JUDGE),
        "--inventory",
        str(INVENTORY),
        "--claims",
        str(claims_path),
        "--output",
        str(report_path),
    ]
    completed = subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own judge call
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"judge exit={completed.returncode}: {completed.stderr[-400:]}")
    return json.loads(report_path.read_text(encoding="utf-8"))


def _buckets(report: dict[str, Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in report["summary"]["status_counts"].items()}


def _statuses(report: dict[str, Any]) -> dict[str, str]:
    return {str(m["model_identity"]): str(m.get("status")) for m in report["models"]}


def _write(document: dict[str, Any], scratch: Path, name: str) -> Path:
    path = scratch / f"{name}.claims.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _drop_claims(document: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    out = copy.deepcopy(document)
    out["claims"] = [c for c in out["claims"] if _identity(c) not in names]
    return out


def _flip_review(document: dict[str, Any], identity: str, decision: str) -> dict[str, Any]:
    out = copy.deepcopy(document)
    provider, model = identity.split("::", 1)
    for review in out["primary_reviews"]:
        if review.get("provider") == provider and review.get("upstream_model") == model:
            review["decision"] = decision
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the three bucket arms and print one verdict line each."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scratch-dir",
        default=".w21-scratch/bucket",
        help="in-repo scratch root for the synthetic claims documents",
    )
    args = parser.parse_args(argv)
    scratch = REPO_ROOT / args.scratch_dir
    scratch.mkdir(parents=True, exist_ok=True)
    document = json.loads(MERGED.read_text(encoding="utf-8"))
    written: list[Path] = []
    results: list[bool] = []

    # --- A: the committed face must be reproduced by this instrument, same generation -----------
    control = _run_judge(_write(document, scratch, "control"), scratch)
    written.append(scratch / "control.claims.json")
    written.append(scratch / "control.claims.report.json")
    base = _buckets(control)
    control_statuses = _statuses(control)
    results.append(
        _verdict(
            "A_control",
            f"accepted={control['summary']['offline_acceptance_count']} buckets={base}"
            f" population={control['summary']['identity_count']}"
            f" six_target_rows={sorted({control_statuses[n] for n in BLOCKED_CLAIM_ROWS})}"
            f" two_target_rows={sorted({control_statuses[n] for n in ZERO_CLAIM_ROWS})}",
            control["summary"]["offline_acceptance_count"] == 258
            and base == {"OFFLINE_ACCEPTED": 258, "UNREVIEWED": 83, "INCOMPLETE": 9}
            and {control_statuses[n] for n in BLOCKED_CLAIM_ROWS} == {"INCOMPLETE"}
            and {control_statuses[n] for n in ZERO_CLAIM_ROWS} == {"UNREVIEWED"},
        )
    )

    # --- B: claims-presence, not claim status, is what separates the two buckets ----------------
    stripped = _drop_claims(document, BLOCKED_CLAIM_ROWS)
    before = sum(1 for c in document["claims"] if _identity(c) in BLOCKED_CLAIM_ROWS)
    dropped = before - sum(1 for c in stripped["claims"] if _identity(c) in BLOCKED_CLAIM_ROWS)
    report_b = _run_judge(_write(stripped, scratch, "stripped"), scratch)
    written += [scratch / "stripped.claims.json", scratch / "stripped.claims.report.json"]
    after_b = _buckets(report_b)
    statuses_b = _statuses(report_b)
    dropped_statuses = sorted(
        {str(c.get("status")) for c in document["claims"] if _identity(c) in BLOCKED_CLAIM_ROWS}
    )
    control_accepted = int(control["summary"]["offline_acceptance_count"])
    results.append(
        _verdict(
            "B_claims_presence",
            f"blocked_claims_dropped={dropped} (all statuses={dropped_statuses})"
            f" INCOMPLETE={base['INCOMPLETE']}->{after_b['INCOMPLETE']}"
            f" UNREVIEWED={base['UNREVIEWED']}->{after_b['UNREVIEWED']}"
            f" re-bucketed={sorted({statuses_b[n] for n in BLOCKED_CLAIM_ROWS})}"
            f" accepted={control_accepted}->{after_b['OFFLINE_ACCEPTED']}",
            dropped == before
            and before == 30
            and after_b["INCOMPLETE"] == base["INCOMPLETE"] - 6
            and after_b["UNREVIEWED"] == base["UNREVIEWED"] + 6
            and after_b["OFFLINE_ACCEPTED"] == base["OFFLINE_ACCEPTED"]
            and {statuses_b[n] for n in BLOCKED_CLAIM_ROWS} == {"UNREVIEWED"},
        )
    )

    # --- C: the merged document's decision field is bound to a signed review file ----------------
    accepted_one = next(
        name for name, status in control_statuses.items() if status == "OFFLINE_ACCEPTED"
    )
    rejected_doc = _flip_review(document, accepted_one, "REJECT")
    rejected_report = _run_judge(_write(rejected_doc, scratch, "rejected"), scratch)
    written += [scratch / "rejected.claims.json", scratch / "rejected.claims.report.json"]
    statuses_c = _statuses(rejected_report)
    buckets_c = _buckets(rejected_report)
    subject = next(m for m in rejected_report["models"] if m["model_identity"] == accepted_one)
    subject = next(m for m in rejected_report["models"] if m["model_identity"] == accepted_one)
    results.append(
        _verdict(
            "C_review_receipt_binding",
            f"subject={accepted_one}"
            f" status={control_statuses[accepted_one]}->{statuses_c[accepted_one]}"
            f" issues={subject.get('issues')}"
            f" accepted={base['OFFLINE_ACCEPTED']}->{buckets_c['OFFLINE_ACCEPTED']}"
            f" top_invalid_claims={len(rejected_report['invalid_claims'])}",
            statuses_c[accepted_one] == "INCOMPLETE"
            and subject.get("issues") == ["primary_review_receipt_decision_mismatch"]
            and buckets_c["OFFLINE_ACCEPTED"] == base["OFFLINE_ACCEPTED"] - 1
            and rejected_report["invalid_claims"] == [],
        )
    )
    print(
        "C_scope: the arm shows the judge REFUSES an unsigned decision edit — a review object's "
        "receipt.sha256 is the digest of docs/evidence/C101/primary-reviews/<id>.review.json and "
        "the decision in the merged document must match that file. It does NOT show the REJECTED "
        "branch is reachable: getting there means writing the review file itself, which is the "
        "recorder's job, not this probe's."
    )

    print(
        f"§9_reading INCOMPLETE={base['INCOMPLETE']}->{base['INCOMPLETE'] - 6}"
        f" UNREVIEWED={base['UNREVIEWED']}->{base['UNREVIEWED'] - 2}"
        f" if the 6 BLOCKED-claim rows gain five CANDIDATE phases and the 2 claimless rows gain"
        f" claims+reviews; 8 rows accepted -> 266"
    )

    for path in written:
        if path.exists():
            path.unlink()
    passed = sum(1 for ok in results if ok)
    print(
        f"bucket_arms={len(results)} bucket_passed={passed} bucket_failed={len(results) - passed}"
    )
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
