"""Record iteration-2 primary reviews in the exact shape the evidence judge re-verifies.

The judge in :mod:`scripts.quality.iter2_model_evidence` treats a claim ``PASS`` as a
candidate only: a model row becomes ``OFFLINE_ACCEPTED`` when all five phase claims
validate AND one explicit primary review (a ``primary_model`` or ``human`` receipt whose
``evidence_snapshot`` equals the snapshot derived from those same claims) carries an
``ACCEPT``. This tool produces that second half and refuses to forge it: it will not emit
an ``ACCEPT`` for a row whose claims do not already validate against the files on disk.

Every constant, every validation and the snapshot formula come from the judge itself.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess  # nosec B404  # one run(): argv re-read from a repo-authored claim, no shell
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality import iter2_model_evidence as judge  # noqa: E402

SHELL_METACHARACTERS: Final = ("|", ";", "&", "$", "<", ">", "(", ")", "`")
RECEIPT_RECORD_TYPE: Final = "primary_review"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _index_claims(claims: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, Any]]:
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        provider = claim.get("provider")
        model = claim.get("upstream_model")
        phase = claim.get("phase")
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        if not isinstance(phase, str) or phase not in judge.REQUIRED_PHASES:
            continue
        by_identity.setdefault((provider, model), {}).setdefault(phase, claim)
    return by_identity


def _phase_claims(
    indexed: dict[tuple[str, str], dict[str, Any]], identity: tuple[str, str]
) -> dict[str, object] | None:
    phases = indexed.get(identity, {})
    if set(phases) != set(judge.REQUIRED_PHASES):
        return None
    return {phase: phases[phase] for phase in judge.REQUIRED_PHASES}


def _claims_are_candidate(identity: tuple[str, str], phase_claims: dict[str, object]) -> list[str]:
    """Return the judge's own objections to a row, empty only when every phase is CANDIDATE."""
    problems: list[str] = []
    for phase, claim in phase_claims.items():
        validated = judge._validate_claim(claim, identity=identity, repo_root=judge.REPO_ROOT)
        if validated["stale"]:
            problems.append(f"{phase}:stale")
        elif not validated["valid"]:
            problems.append(f"{phase}:" + ",".join(validated["issues"]))
    return problems


def _snapshot_raw(phase_claims: dict[str, object]) -> dict[str, object]:
    """Mirror the judge's snapshot fields so it can recompute the tuple it expects."""
    phases: dict[str, object] = {}
    for phase, raw_claim in phase_claims.items():
        claim = raw_claim if isinstance(raw_claim, dict) else {}
        raw_receipt = claim.get("receipt")
        receipt = raw_receipt if isinstance(raw_receipt, dict) else {}
        raw_pins = claim.get("pins")
        pins = raw_pins if isinstance(raw_pins, dict) else {}
        phases[phase] = {
            "receipt": {"path": receipt.get("path"), "sha256": receipt.get("sha256")},
            "source_pins": pins.get("source"),
            "contract_pins": pins.get("contract"),
            "contract_version": pins.get("contract_version"),
        }
    return {"phases": phases}


def _reexecute(claim: object) -> dict[str, object]:
    if not isinstance(claim, dict):
        return {"status": "skipped", "reason": "claim_not_object"}
    command = claim.get("actual_command")
    if not isinstance(command, str) or not command.strip():
        return {"status": "skipped", "reason": "actual_command_missing"}
    if any(marker in command for marker in SHELL_METACHARACTERS):
        return {"status": "skipped", "reason": "command_needs_a_shell"}
    argv = shlex.split(command)
    completed = subprocess.run(  # noqa: S603  # nosec B603  # argv re-read from a repo-authored claim
        argv, cwd=judge.REPO_ROOT, capture_output=True, check=False
    )
    recorded = claim.get("exit_code")
    return {
        "status": "matched" if completed.returncode == recorded else "diverged",
        "recorded_exit_code": recorded,
        "reviewer_exit_code": completed.returncode,
        "argv": argv,
    }


def _reviewed_identities(
    args: argparse.Namespace, indexed: dict[tuple[str, str], dict[str, Any]]
) -> list[tuple[str, str]]:
    if args.identity:
        identities: list[tuple[str, str]] = []
        for item in args.identity:
            provider, separator, model = item.partition("::")
            if not separator or not provider or not model:
                raise SystemExit(f"refusing: --identity {item!r} is not provider::upstream_model")
            identities.append((provider, model))
        return identities
    if args.provider:
        return [key for key in sorted(indexed) if key[0] in set(args.provider)]
    return sorted(indexed)


def build_review(
    *,
    identity: tuple[str, str],
    phase_claims: dict[str, object],
    claims_path: Path,
    receipt_dir: Path,
    reviewer_type: str,
    reviewer_id: str,
    reviewer_model: str,
    reviewer_role: str,
    decision: str,
    reexecute: bool,
) -> dict[str, object]:
    """Write one review receipt to disk and return the review object that points at it."""
    provider, model = identity
    reviewed_at = _utc_now()
    claims_bytes = claims_path.read_bytes()
    receipt: dict[str, object] = {
        "receipt_schema_version": judge.CLAIM_RECEIPT_SCHEMA_VERSION,
        "record_type": RECEIPT_RECORD_TYPE,
        "provider": provider,
        "model": model,
        "upstream_model": model,
        "model_identity": f"{provider}::{model}",
        "reviewer_type": reviewer_type,
        "reviewer_id": reviewer_id,
        "reviewer_model": reviewer_model if reviewer_type == "primary_model" else "",
        "reviewer_role": reviewer_role,
        "decision": decision,
        "reviewed_at_utc": reviewed_at,
        "evidence_snapshot": _snapshot_raw(phase_claims),
        "claims_document": {
            "path": claims_path.relative_to(judge.REPO_ROOT).as_posix(),
            "sha256": judge.sha256_bytes(claims_bytes),
        },
        "phases_reviewed": list(judge.REQUIRED_PHASES),
    }
    if reexecute:
        receipt["reexecution"] = {phase: _reexecute(claim) for phase, claim in phase_claims.items()}

    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{provider}__{model}.review.json"
    receipt_text = json.dumps(receipt, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    receipt_path.write_text(receipt_text, encoding="utf-8")
    relative = receipt_path.relative_to(judge.REPO_ROOT).as_posix()

    review: dict[str, object] = {
        "provider": provider,
        "upstream_model": model,
        "reviewer_type": reviewer_type,
        "reviewer_id": reviewer_id,
        "reviewer_model": reviewer_model if reviewer_type == "primary_model" else None,
        "reviewer_role": reviewer_role,
        "decision": decision,
        "reviewed_at_utc": reviewed_at,
        "receipt": {
            "path": relative,
            "sha256": judge.sha256_bytes(receipt_text.encode("utf-8")),
            "model_identity": f"{provider}::{model}",
        },
    }
    if reviewer_type == "human":
        review["reviewer_model"] = None
    return review


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse recorder arguments; a reviewer identity is never defaulted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=judge.DEFAULT_INVENTORY)
    parser.add_argument("--claims", required=True, help="claims JSON produced by the claim runner")
    parser.add_argument("--output", required=True, help="merged claims JSON path")
    parser.add_argument("--receipt-dir", default="docs/evidence/C101/primary-reviews")
    parser.add_argument("--identity", action="append", default=[], help="provider::upstream_model")
    parser.add_argument("--provider", action="append", default=[], help="whole provider")
    parser.add_argument(
        "--decision",
        default="ACCEPT",
        choices=("ACCEPT", "REJECT"),
        help="REJECT is the only honest reading for an unproven row",
    )
    parser.add_argument(
        "--reviewer-type", default="primary_model", choices=("primary_model", "human")
    )
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--reviewer-model", default="")
    parser.add_argument("--reviewer-role", required=True)
    parser.add_argument(
        "--reexecute",
        action="store_true",
        help="re-run each claim's actual_command and record the reviewer's own exit code",
    )
    parser.add_argument(
        "--report", default="", help="run the evidence judge and write the index here"
    )
    parser.add_argument(
        "--allow-unproven",
        action="store_true",
        help="sign a row whose claims do not validate; only meaningful with --decision REJECT",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Sign rows whose five phase claims already validate; refuse the rest loudly."""
    args = parse_args(argv)
    if args.reviewer_type == "primary_model" and not args.reviewer_model.strip():
        print(
            "refusing: --reviewer-model is required for a primary_model reviewer", file=sys.stderr
        )
        return 2
    if args.reviewer_type == "human" and args.reviewer_model.strip():
        print("refusing: a human reviewer must not name a reviewer model", file=sys.stderr)
        return 2

    inventory = judge.load_inventory((judge.REPO_ROOT / args.inventory).resolve())
    claims_path = (judge.REPO_ROOT / args.claims).resolve()
    claims_document = json.loads(claims_path.read_text(encoding="utf-8"))
    raw_claims = claims_document.get("claims")
    if not isinstance(raw_claims, list):
        print("refusing: claims document has no claims array", file=sys.stderr)
        return 2
    indexed = _index_claims([c for c in raw_claims if isinstance(c, dict)])

    output_path = (judge.REPO_ROOT / args.output).resolve()
    merged = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    if not isinstance(merged, dict):
        print("refusing: existing output is not a JSON object", file=sys.stderr)
        return 2
    # The claims face always comes from --claims, never from a previous output copy: an
    # aged claims array would pin digests that no longer match the bytes on disk.
    merged["schema_version"] = judge.SCHEMA_VERSION
    merged["identity_set_sha256"] = inventory["identity_set_sha256"]
    merged["claims"] = raw_claims
    if not isinstance(merged.get("primary_reviews"), list):
        merged["primary_reviews"] = []

    targets = list(_reviewed_identities(args, indexed))
    resigning = set(targets)
    carried = [
        review
        for review in merged["primary_reviews"]
        if not (
            isinstance(review, dict)
            and (review.get("provider"), review.get("upstream_model")) in resigning
        )
    ]
    dropped = len(merged["primary_reviews"]) - len(carried)
    merged["primary_reviews"] = carried
    if dropped:
        print(f"re-signing: dropped {dropped} earlier review(s) for the addressed identities")

    reviews: list[dict[str, object]] = merged["primary_reviews"]
    receipt_dir = (judge.REPO_ROOT / args.receipt_dir).resolve()
    signed: list[str] = []
    refused: list[str] = []
    for identity in targets:
        provider, model = identity
        if len(identity) != 2 or not provider or not model:
            refused.append(f"{identity}:malformed_identity")
            continue
        phase_claims = _phase_claims(indexed, identity)
        if phase_claims is None:
            refused.append(f"{provider}::{model}:claims_incomplete")
            continue
        problems = _claims_are_candidate(identity, phase_claims)
        if problems and not (args.allow_unproven or args.decision == "REJECT"):
            refused.append(f"{provider}::{model}:" + "; ".join(problems))
            continue
        if problems and args.decision == "ACCEPT":
            refused.append(f"{provider}::{model}:" + "; ".join(problems))
            continue
        reviews = [
            r
            for r in reviews
            if not (r.get("provider") == provider and r.get("upstream_model") == model)
        ]
        reviews.append(
            build_review(
                identity=(provider, model),
                phase_claims=phase_claims,
                claims_path=claims_path,
                receipt_dir=receipt_dir,
                reviewer_type=args.reviewer_type,
                reviewer_id=args.reviewer_id,
                reviewer_model=args.reviewer_model,
                reviewer_role=args.reviewer_role,
                decision=args.decision,
                reexecute=args.reexecute,
            )
        )
        signed.append(f"{provider}::{model}")

    merged["primary_reviews"] = reviews
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )

    exit_code = 1 if args.identity and refused else 0
    for line in signed:
        print(f"REVIEW_SIGNED: {line}")
    for line in refused:
        print(f"REVIEW_REFUSED: {line}")
    print(
        f"recorder_exit={exit_code} signed={len(signed)} refused={len(refused)} "
        f"reviews_total={len(reviews)} output={output_path}"
    )

    if args.report:
        report_code = judge.main(
            [
                "--inventory",
                args.inventory,
                "--claims",
                str(output_path),
                "--output",
                args.report,
            ]
        )
        return report_code if report_code != 0 else exit_code
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
