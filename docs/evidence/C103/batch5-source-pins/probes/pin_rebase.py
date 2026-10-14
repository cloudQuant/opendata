"""C103: re-base the two stale batch5 ``source_hashes_sha256`` entries and the payload pins.

The snapshot is a fence over 31 provider source files.  Two of them moved only in typing
annotations (``cast(...)`` additions from the mypy repair), so the fence is stale while the pinned
identity surface is unchanged.  Per the standing ruling, digests may be re-derived from live
artifacts -- but every value written here is derived by hashing the live file, never typed.

Refuses to write when the census finds nothing stale, so a re-run cannot silently "re-pin" a
digest that has already moved.

Usage: python docs/evidence/C103/batch5-source-pins/probes/pin_rebase.py [--apply]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SNAPSHOT_REL = "tests/fixtures/registry_batch5_snapshot.json"
TEST_LITERALS = (
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
)
HEX = re.compile(r"^[0-9a-f]{64}$")


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()
APPLY = "--apply" in sys.argv


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def census(pins: dict[str, str]) -> tuple[list[tuple[str, str, str]], int, int]:
    stale, matched, missing = [], 0, 0
    for rel, expected in pins.items():
        if not HEX.fullmatch(expected):
            raise SystemExit(f"pinned value for {rel} is not a lowercase sha256 hex: {expected!r}")
        source = REPO / rel
        if not source.is_file():
            missing += 1
            print(f"face: MISSING pinned source {rel}")
            continue
        live = sha(source)
        if live == expected:
            matched += 1
        else:
            stale.append((rel, expected, live))
    return stale, matched, missing


def main() -> int:
    snapshot_path = REPO / SNAPSHOT_REL
    original_bytes = snapshot_path.read_bytes()
    old_payload_sha = hashlib.sha256(original_bytes).hexdigest()
    snapshot = json.loads(original_bytes)
    pins = snapshot["source_hashes_sha256"]

    print(f"face: repo_root={REPO}")
    print(f"face: argv={sys.argv[1:]}")
    print(f"face: pin_entries={len(pins)}")
    print(f"face: snapshot_payload_sha_before={old_payload_sha}")
    print(f"face: candidate_sha256_before={snapshot['candidate_sha256']}")

    stale, matched, missing = census(pins)
    print(f"face: matched={matched} stale={len(stale)} missing={missing}")
    for rel, expected, live in stale:
        print(f"face: stale[{rel}] before={expected}")
        print(f"face: stale[{rel}] live ={live}")
        diff = subprocess_text(["git", "diff", "--numstat", "HEAD", "--", rel])
        print(f"face: stale[{rel}] vs_HEAD_numstat={diff.splitlines()[0] if diff else 'no diff'}")

    if not stale:
        print("verdict: REFUSED - no stale pin to re-base, refusing to re-pin a moving digest")
        return 1
    if missing:
        print("verdict: REFUSED - a pinned source file is absent, re-basing would erase the fence")
        return 1

    staged = original_bytes.decode()
    for rel, expected, live in stale:
        occurrences = staged.count(expected)
        if occurrences != 1:
            print(f"verdict: REFUSED - {expected[:12]} appears {occurrences}x in the snapshot, not 1x")
            return 1
        staged = staged.replace(expected, live, 1)
    staged_bytes = staged.encode()

    new_payload_sha = hashlib.sha256(staged_bytes).hexdigest()
    reloaded = json.loads(staged_bytes)
    print(f"face: snapshot_payload_sha_after={new_payload_sha}")
    print(f"face: candidate_sha256_after={reloaded['candidate_sha256']}")
    print(f"face: candidate_unchanged={reloaded['candidate_sha256'] == snapshot['candidate_sha256']}")
    print(f"face: snapshot_id_unchanged={reloaded['snapshot_id'] == snapshot['snapshot_id']}")
    print(
        "face: snapshot_status_unchanged="
        f"{reloaded['snapshot_status'] == snapshot['snapshot_status']}"
    )
    print(f"face: counts_unchanged={reloaded['counts'] == snapshot['counts']}")
    print(
        "face: identities_unchanged="
        f"{reloaded['provider_model_identities'] == snapshot['provider_model_identities']}"
    )
    print(f"face: pin_keys_unchanged={list(reloaded['source_hashes_sha256']) == list(pins)}")
    print(f"face: bytes_delta={len(staged_bytes) - len(original_bytes)}")
    if reloaded["candidate_sha256"] != snapshot["candidate_sha256"]:
        print("verdict: REFUSED - the candidate digest must not move in a re-base")
        return 1
    if reloaded["counts"] != snapshot["counts"] or (
        reloaded["provider_model_identities"] != snapshot["provider_model_identities"]
    ):
        print("verdict: REFUSED - a re-base may not alter the pinned census it guards")
        return 1

    literal_plan: list[tuple[str, int]] = []
    for rel in TEST_LITERALS:
        text = (REPO / rel).read_text()
        literal_plan.append((rel, text.count(old_payload_sha)))
    for rel, occurrences in literal_plan:
        print(f"face: literal[{rel}] occurrences_of_old_payload_sha={occurrences}")
    if any(occurrences != 1 for _, occurrences in literal_plan):
        print("verdict: REFUSED - the payload digest literal is not found exactly once per test file")
        return 1

    if not APPLY:
        print("verdict: DRY RUN - nothing written (pass --apply to write)")
        return 0

    snapshot_path.write_bytes(staged_bytes)
    for rel in TEST_LITERALS:
        path = REPO / rel
        path.write_text(path.read_text().replace(old_payload_sha, new_payload_sha, 1))
    print(f"face: applied snapshot={sha(snapshot_path) == new_payload_sha}")
    for rel in TEST_LITERALS:
        text = (REPO / rel).read_text()
        print(f"face: applied[{rel}] new_literal_present={new_payload_sha in text} stale_literal={old_payload_sha in text}")
    print("verdict: APPLIED - 2 source pins re-based from live bytes and 2 payload literals re-pinned")
    return 0


def subprocess_text(argv: list[str]) -> str:
    import subprocess

    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False).stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
