"""C103: re-pin the two test literals that hash the batch5 snapshot payload after the re-base.

``tests/test_provider_domains.py`` (``BATCH5_SNAPSHOT_SHA256``) and ``tests/test_pipeline_jobs.py``
(inline in ``test_the_registered_census_reaches_the_ods_layer``) each assert that the sha256 of
``tests/fixtures/registry_batch5_snapshot.json`` equals a typed hex string. Flipping 9
``scenario_present`` rows necessarily moves that digest, so both literals are re-based to the digest
computed from the file on disk -- with the per-source pin block and ``candidate_sha256`` left alone,
because the fixture write under test does not touch either.

Refusal arms: a payload literal that is absent or ambiguous (each carrier must hold exactly one
64-hex the fixture does not itself record), an edit whose scope escapes that hex window,
and carriers
that disagree on which digest is pinned. A second run refuses with 0 candidates, because the new
digest is excluded from the search.

Usage: python docs/evidence/C103/batch5-scenario-census/probes/payload_literal_repin.py [--apply]
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404
import sys
from pathlib import Path

CARRIERS = ("tests/test_provider_domains.py", "tests/test_pipeline_jobs.py")
SNAPSHOT_REL = "tests/fixtures/registry_batch5_snapshot.json"
HEX = re.compile(r"\b[0-9a-f]{64}\b")


def repo_root() -> Path:
    """Resolve the repository root by walking up to the nearest ``pytest.ini``."""
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        if (ancestor / "pytest.ini").is_file():
            return ancestor
    raise SystemExit(f"no pytest.ini found above {current}")


REPO = repo_root()
APPLY = "--apply" in sys.argv


def git_head_bytes(rel: str) -> bytes:
    """Return the committed bytes of ``rel`` at HEAD, refusing the run when HEAD lacks it."""
    proc = subprocess.run(  # nosec B603 B607  # noqa: S603
        ["git", "show", f"HEAD:{rel}"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        check=False,  # nosec B404
    )
    if proc.returncode:
        raise SystemExit(f"git show HEAD:{rel} failed: {proc.stderr.decode()[:200]}")
    return proc.stdout


def main() -> int:
    """Re-pin the payload-digest literals in the two test carriers, refusing on ties."""
    snapshot_bytes = (REPO / SNAPSHOT_REL).read_bytes()
    new_sha = hashlib.sha256(snapshot_bytes).hexdigest()
    print(f"face: snapshot_bytes={len(snapshot_bytes)}")
    print(f"face: digest_recomputed_from_disk={new_sha}")
    head_sha = hashlib.sha256(git_head_bytes(SNAPSHOT_REL)).hexdigest()
    print(f"face: digest_at_git_head={head_sha}")
    print(f"face: fixture_dirty_vs_head={head_sha != new_sha}")

    staged: dict[str, tuple[bytes, bytes, list[str]]] = {}
    snapshot_text = snapshot_bytes.decode()
    for rel in CARRIERS:
        path = REPO / rel
        original = path.read_bytes()
        text = original.decode()
        # Every other 64-hex in these carriers is a value the fixture itself records (a per-source
        # digest, candidate_sha256, a pin-chain sha). The payload digest is the one literal the
        # fixture cannot vouch for, so resolve it by exclusion instead of by a proximity guess.
        sites = [m for m in HEX.finditer(text) if m.group() != new_sha]
        payload_sites = [m for m in sites if m.group() not in snapshot_text]
        distinct = sorted({m.group() for m in payload_sites})
        print(
            f"face: {rel} hex_literals_not_recorded_by_the_fixture={len(payload_sites)} "
            f"distinct={distinct}"
        )
        if len(payload_sites) != 1:
            print(
                f"verdict: REFUSED - {rel} has {len(payload_sites)} candidate payload literals; "
                "refusing to guess which one hashes the snapshot"
            )
            return 1
        (match,) = payload_sites
        stale = match.group()
        start, end = match.span()
        rewritten = text[:start] + new_sha + text[end:]
        re_written_bytes = rewritten.encode()
        # Byte-scope control: the edit must be in-place, same length, same line count, and the only
        # differing characters must be inside the 64-hex window. The window is judged on CHARACTER
        # offsets from the decoded text: byte offsets drift past non-ASCII characters, and that
        # misreporting (71 non-ASCII chars ahead of the literal in test_pipeline_jobs.py, 0 in
        # test_provider_domains.py) is what first refused this edit as out-of-scope.
        differing_positions = [
            i for i, (a, b) in enumerate(zip(text, rewritten, strict=True)) if a != b
        ]
        in_window = all(start <= i < end for i in differing_positions)
        non_ascii_before_literal = sum(1 for ch in text[:start] if ord(ch) > 127)
        print(
            f"face: {rel} bytes_delta={len(re_written_bytes) - len(original)} "
            f"lines_before={original.count(chr(10).encode())} "
            f"lines_after={re_written_bytes.count(chr(10).encode())} "
            f"differing_chars={len(differing_positions)} inside_hex_window={in_window} "
            f"non_ascii_chars_before_literal={non_ascii_before_literal}"
        )
        if len(differing_positions) == 0 or not in_window:
            print(f"verdict: REFUSED - {rel} edit scope is not confined to the hex literal")
            return 1
        staged[rel] = (original, re_written_bytes, [stale])
        print(
            f"face: {rel} stale_literal={stale} "
            f"head_matches_original={git_head_bytes(rel) == original}"
        )
    stale_literals = sorted({stale for _o, _r, (stale,) in staged.values()})
    print(f"face: carriers={len(staged)} stale_literals_across_carriers={stale_literals}")
    if len(stale_literals) != 1:
        print("verdict: REFUSED - the carriers do not all pin the same payload digest")
        return 1
    if not APPLY:
        print("verdict: DRY RUN - nothing written (pass --apply to write)")
        return 0
    for rel, (_original, staged_bytes, _stale) in staged.items():
        (REPO / rel).write_bytes(staged_bytes)
        readback = (REPO / rel).read_bytes()
        print(
            f"face: applied {rel} readback_matches={readback == staged_bytes} bytes={len(readback)}"
        )
    print(f"verdict: APPLIED - {len(CARRIERS)} payload literals re-pinned to {new_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
