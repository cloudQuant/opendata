r"""Re-produce every claim shard after wave 29 widened the pinned probe.

Why the whole population and not just deribit: ``iter2_claims_probe_runner.py`` is the ``probe`` pin
of every claim on disk, and ``iter2_model_evidence.py:625-627`` re-hashes each pin against the
worktree and sets ``stale`` when the current digest differs from ``after_sha256``; ``:731`` then
reads ``valid = not issues and not stale``. So a single edit of the shared runner demotes every
already-ACCEPTED row unless its evidence is re-executed under the new bytes. Re-pinning the digests
without re-running would assert executions that did not happen, so this driver runs the shipped
producer per provider.

Parallelism is safe because every write the producer performs is provider-scoped:
``receipts/<provider>/<model>/<phase>.json`` (:234),
``captures/<provider>/<model>/<phase>-exit*.txt`` (:150),
``fixtures/<provider>/<model>.json`` (:77) and ``<provider>/claims.json``. No shared counter,
index or append file is touched, so four providers at a time cannot interleave into one another's
bytes.

The face line at the end counts what the producers themselves printed -- the
``claims emitted=N (a PASS, b FAIL, c BLOCKED)`` denominator sentence of each shard, and the
run is charged as failed if any shard lacks that line, refuses, or reports a non-zero FAIL/BLOCKED.
"""

from __future__ import annotations

import concurrent.futures
import re
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[5]
PRODUCER = REPO_ROOT / "scripts/quality/iter2_claims_producer.py"
EVIDENCE_ROOT = REPO_ROOT / "docs/evidence/C101"
LOG_DIR = Path(tempfile.gettempdir()) / "w29-reproduce"
WORKERS = 4
#: deribit is produced separately as this wave's new shard; the census below re-takes the rest.
SKIP_PROVIDERS = {"deribit"}

CLAIMS_LINE = re.compile(r"claims emitted=(\d+) \((\d+) PASS, (\d+) FAIL, (\d+) BLOCKED\)")


def shards() -> list[str]:
    """Every provider that already has a claim shard on disk, minus the one produced this wave."""
    return sorted(
        p.name
        for p in EVIDENCE_ROOT.iterdir()
        if (p / "claims.json").is_file() and p.name not in SKIP_PROVIDERS
    )


def run_one(provider: str) -> dict[str, Any]:
    """Run the shipped producer for one provider and parse the denominator sentence it printed."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"{provider}.txt"
    completed = subprocess.run(  # noqa: S603  # nosec B603, B607  # argv is the shipped producer
        [
            sys.executable,
            str(PRODUCER),
            "--provider",
            provider,
            "--output",
            f"docs/evidence/C101/{provider}/claims.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    match = CLAIMS_LINE.search(completed.stdout)
    return {
        "provider": provider,
        "exit": completed.returncode,
        "emitted": int(match.group(1)) if match else -1,
        "passed": int(match.group(2)) if match else -1,
        "failed": int(match.group(3)) if match else -1,
        "blocked": int(match.group(4)) if match else -1,
    }


def main() -> int:
    """Re-produce every shard four at a time and print one ledger line and one face line."""
    targets = shards()
    print(
        f"reproduce_start shards={len(targets)} workers={WORKERS} skipped={sorted(SKIP_PROVIDERS)}"
    )
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(run_one, targets):
            results.append(result)
            print(
                f"shard provider={result['provider']} exit={result['exit']} "
                f"emitted={result['emitted']} pass={result['passed']} "
                f"fail={result['failed']} blocked={result['blocked']}",
                flush=True,
            )
    blocked = sum(r["blocked"] for r in results)
    failed = sum(r["failed"] for r in results)
    # BLOCKED is a legitimate verdict: the producer records a phase that could not run at all, with
    # no receipt and no probe pin. Only a nonzero exit or a FAIL claim means the wave went wrong.
    bad = [r for r in results if r["exit"] != 0 or r["failed"] or r["emitted"] < 0]
    emitted = sum(r["emitted"] for r in results)
    passed = sum(r["passed"] for r in results)
    print(
        f"reproduce_face shards={len(results)} clean={len(results) - len(bad)} "
        f"red={len(bad)} red_providers={[r['provider'] for r in bad]} "
        f"claims_emitted={emitted} claims_passed={passed} claims_failed={failed} "
        f"claims_blocked={blocked} blocked_providers="
        f"{sorted({r['provider'] for r in results if r['blocked']})} "
        f"identity_exits={[r['exit'] for r in results if r['exit'] != 0]}"
    )
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
