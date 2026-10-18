r"""Re-sign every reviewed shard after wave 29's full re-production.

Why signing is part of the same payment: the recorder pins the claims document it signed
(``iter2_primary_review_recorder.py:163`` stores ``sha256_bytes(claims_bytes)`` in each review
receipt), and it drops/replaces the earlier reviews for the addressed identities (:280). Since
``reproduce_all_shards.py`` rewrote every ``claims.json`` with a new ``probe`` pin, leaving the
review receipts alone would make the signed digest point at bytes that no longer exist.

Each provider is signed whole (``--provider``) with the same argv the last waves used, and nothing
here re-executes the claims (no ``--reexecute``, no ``--allow-unproven``): the recorder's own job is
to record that a reviewer read these claims, while the execution evidence came from the producers.
The face line counts the recorder's own ``signed=``/``refused=`` per shard and sums them, so a shard
that silently signed nothing is charged as red rather than reading as a smaller denominator.

Writes are again provider-scoped (``<provider>/claims.reviewed.json`` plus
``primary-reviews/<identity>.json``), so four shards run concurrently.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
RECORDER = REPO_ROOT / "scripts/quality/iter2_primary_review_recorder.py"
EVIDENCE_ROOT = REPO_ROOT / "docs/evidence/C101"
LOG_DIR = Path(tempfile.gettempdir()) / "w29-resign"
WORKERS = 4

FACE_LINE = re.compile(r"recorder_exit=(-?\d+) signed=(\d+) refused=(\d+) reviews_total=(\d+)")


def shards() -> list[str]:
    """Every provider with a reviewed shard, plus deribit, this wave's new shard."""
    existing = sorted(
        p.name for p in EVIDENCE_ROOT.iterdir() if (p / "claims.reviewed.json").is_file()
    )
    claims_only = [p.name for p in EVIDENCE_ROOT.iterdir() if (p / "claims.json").is_file()]
    return sorted(set(existing) | {p for p in claims_only if p not in existing})


def run_one(provider: str) -> dict[str, Any]:
    """Sign one shard with the shipped recorder and parse the face line it printed."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(  # noqa: S603  # nosec B603, B607  # argv is the shipped recorder
        [
            sys.executable,
            str(RECORDER),
            "--claims",
            f"docs/evidence/C101/{provider}/claims.json",
            "--output",
            f"docs/evidence/C101/{provider}/claims.reviewed.json",
            "--provider",
            provider,
            "--decision",
            "ACCEPT",
            "--reviewer-type",
            "primary_model",
            "--reviewer-id",
            "iter2-acceptance-reviewer",
            "--reviewer-model",
            "qoder-cli-agent",
            "--reviewer-role",
            "acceptance_reviewer",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    (LOG_DIR / f"{provider}.txt").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    match = FACE_LINE.search(completed.stdout)
    return {
        "provider": provider,
        "exit": completed.returncode,
        "signed": int(match.group(2)) if match else -1,
        "refused": int(match.group(3)) if match else -1,
        "reviews_total": int(match.group(4)) if match else -1,
    }


def expected_signed(provider: str) -> int:
    """Identities whose five phase claims all read PASS -- the shard's signable population.

    ``refused`` is not a defect signal here: a BLOCKED claim has no receipt to review, so the
    recorder refuses it by design and still exits 0 (``iter2_primary_review_recorder.py:296,329``).
    Red is the recorder signing a different number than the claims on disk predict.
    """
    raw = json.loads((EVIDENCE_ROOT / provider / "claims.json").read_text(encoding="utf-8"))
    states: dict[str, list[str]] = {}
    for claim in raw["claims"]:
        states.setdefault(str(claim["upstream_model"]), []).append(str(claim["status"]))
    return sum(1 for v in states.values() if len(v) == 5 and set(v) == {"PASS"})


def main() -> int:
    """Sign every shard four at a time and print one ledger line and one face line."""
    targets = shards()
    expected = {p: expected_signed(p) for p in targets}
    expected_total = sum(expected.values())
    print(f"resign_start shards={len(targets)} workers={WORKERS} expected_signed={expected_total}")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(run_one, targets):
            results.append(result)
            print(
                f"sign provider={result['provider']} exit={result['exit']} "
                f"signed={result['signed']} refused={result['refused']} "
                f"expected={expected[result['provider']]} "
                f"reviews_total={result['reviews_total']}",
                flush=True,
            )
    bad = [
        r
        for r in results
        if r["exit"] != 0 or r["signed"] < 0 or r["signed"] != expected[r["provider"]]
    ]
    print(
        f"resign_face shards={len(results)} clean={len(results) - len(bad)} red={len(bad)} "
        f"red_providers={[r['provider'] for r in bad]} "
        f"signed_total={sum(r['signed'] for r in results)} "
        f"expected_total={sum(expected.values())} "
        f"refused_total={sum(r['refused'] for r in results)} "
        f"reviews_total_max={max((r['reviews_total'] for r in results), default=-1)} "
        f"per_shard_signed={ {r['provider']: r['signed'] for r in results} }"
    )
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
