#!/usr/bin/env python3
"""C98 staged-blob credential scan: a zero is published only with its byte invariant and controls.

Three claims have to be shown, not asserted, before "no credentials in this commit" is worth a line
of prose:

1. **Coverage** -- every staged blob was read in full. The instrument prints the bytes it scanned
   and the bytes ``git cat-file`` reports for the same blobs, and refuses to call its own result
   clean unless the two totals are identical. A truncated read would otherwise look like a zero.
2. **Sensitivity** -- every family can actually fire. Each pattern gets a planted witness that must
   be found and a near-miss that must not, so a family that silently matches nothing shows up as a
   FAIL line rather than a quiet pass. The families are then fired against a poisoned *copy* of a
   real staged blob, so the positive control runs on real content and not only on synthetic bytes.
3. **Face** -- the reading over the real staged bytes is printed per family, every hit located at
   ``path:line`` and identified by a fingerprint of the matched token. Fingerprints, not the tokens
   themselves, are what the archived transcript carries: a scan that echoes credential-shaped bytes
   into a file it then re-scans has no fixed point, and the second run would report its own first
   run's output as new findings. Every exclusion is attributed to a named literal rule that carries
   its own counterfact -- a rule that swallowed a live-shaped key fails the run.

Nothing is written to the repo. The poisoned blob lives in memory only, and the scan reads the index
through ``git show :<path>`` rather than the working tree, so it judges exactly the bytes that would
be committed.

Run: PYTHONPATH=. python docs/evidence/C98/staged-blob-credential-scan.py
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass

GIT = shutil.which("git")

#: A credential-shaped assignment, used twice: as the generic family's planted witness and as the
#: bytes appended to a copy of a real staged blob. Assembled from parts, so this instrument's own
#: staged bytes never contain a contiguous match.
PLANTED_GENERIC = b"".join((b"api_key = ", b'"', b"sk-abcdefghijklmnopqrstuvwx", b'"'))

#: Family name -> (pattern, planted witness that must fire, near-miss that must not fire).
FAMILIES: tuple[tuple[str, re.Pattern[bytes], bytes, bytes], ...] = (
    (
        "aws-access-key",
        re.compile(rb"AKIA[0-9A-Z]{16}"),
        b"".join((b"AKIA", b"ABCDEFGHIJKLMN", b"OP")),
        b"AKIA123",
    ),
    (
        "github-token",
        re.compile(rb"gh[pousr]_[0-9A-Za-z]{36}"),
        b"ghp_" + b"a" * 36,
        b"ghp_short",
    ),
    (
        "slack-token",
        re.compile(rb"xox[baprs]-[0-9A-Za-z-]{10,}"),
        b"".join((b"xoxb-", b"1234567890", b"-abcdefgh")),
        b"xox-",
    ),
    (
        "google-api-key",
        re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
        b"AIza" + b"Sy" * 17 + b"a",
        b"AIza-short",
    ),
    (
        "volc-access-key",
        re.compile(rb"AKLT[0-9A-Za-z_-]{16,}"),
        b"AKLT" + b"MDEyMzQ1Njc4OTAxMjM0",
        b"AKLT",
    ),
    (
        "private-key-block",
        re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        b"".join((b"-----BEGIN ", b"RSA ", b"PRIVATE KEY", b"-----")),
        b"-----BEGIN CERTIFICATE-----",
    ),
    (
        "jwt",
        re.compile(rb"eyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}"),
        b"".join(
            (
                b"eyJhbGciOiJIUzI1",
                b"NjJ9",
                b".",
                b"eyJzdWIiOiIxMjM0",
                b"NTY3ODkwIn0",
                b".",
                b"dozjgNpyPw",
                b"dQJFIQj",
            )
        ),
        b"eyJhbGciOiJIUzI1",
    ),
    (
        "generic-assignment",
        re.compile(
            rb"(?i)(api[_-]?key|secret|token|passwd|password|access[_-]?key)"
            rb"[\"']?\s*[:=]\s*[\"'][^\"'\s]{20,}[\"']"
        ),
        PLANTED_GENERIC,
        b"".join((b"api_key = ", b'"', b"short", b'"')),
    ),
    (
        "bearer-token",
        re.compile(rb"(?i)bearer\s+[0-9A-Za-z._~+/=-]{20,}"),
        b"".join((b"Authorization: Bearer ", b"dXNlcjpzZWNyZXR0b2", b"tlbnZoZXJl")),
        b"bearer none",
    ),
)

#: Attributed literal exclusions. A rule here only swallows a match whose line contains
#: exactly these bytes. The single entry is the AWS documentation example key that
#: 开发验收记录 quotes in prose
#: about an earlier counterfact arm; it is not a credential and no live key has its bytes.
DOC_EXAMPLES: tuple[bytes, ...] = (b"".join((b"AKIA", b"IOSFODNN", b"7EXAMPLE")),)

#: A live-shaped key the exclusion rules must NOT swallow. Built from parts so this file itself does
#: not read as a credential.
CANARY_KEY = b"".join((b"x ", b"AKI", b"A", b"QZXWERTY1234UIOP", b" y"))


def _fp(token: bytes) -> str:
    """A non-reproducing fingerprint: attribution without re-feeding the scanner."""
    return hashlib.sha1(token).hexdigest()[:12]  # noqa: S324  # nosec B324  # identity, not security


@dataclass(frozen=True)
class Hit:
    """One family match, located."""

    family: str
    path: str
    line: int
    token: bytes


@dataclass(frozen=True)
class Blob:
    """One staged blob: its path, its bytes, and the size git itself reports."""

    path: str
    data: bytes
    reported: int


def git(*args: str) -> bytes:
    """Run one git subcommand with a literal argv and no shell."""
    return subprocess.run(  # noqa: S603  # nosec B603  # literal argv, shell disabled
        [str(GIT), *args],
        check=True,
        capture_output=True,
    ).stdout


def staged_paths() -> list[str]:
    """Return the paths git currently holds in the index."""
    return [p.decode() for p in git("diff", "--cached", "--name-only", "-z").split(b"\0") if p]


def read_blob(path: str) -> Blob:
    """Read the staged bytes of ``path`` and independently ask git how big they are."""
    return Blob(path, git("show", f":{path}"), int(git("cat-file", "-s", f":{path}").strip()))


def find_hits(blobs: list[Blob], family: str, pattern: re.Pattern[bytes]) -> list[Hit]:
    """Collect every match of one family across the given blobs, located at path:line."""
    hits: list[Hit] = []
    for blob in blobs:
        for match in pattern.finditer(blob.data):
            index = blob.data[: match.start()].count(b"\n")
            hits.append(Hit(family, blob.path, index + 1, match.group(0)))
    return hits


def run_controls() -> list[str]:
    """Fire every family against its planted witness and its near-miss."""
    failures: list[str] = []
    for name, pattern, witness, near_miss in FAMILIES:
        found = len(pattern.findall(witness))
        spilled = len(pattern.findall(near_miss))
        ok = found >= 1 and spilled == 0
        print(
            f"[{'OK' if ok else 'FAIL'}] control {name}: witness_hits={found} "
            f"near_miss_hits={spilled}"
        )
        if not ok:
            failures.append(f"control:{name}")
    return failures


def split_hits(hits: list[Hit]) -> tuple[list[Hit], list[Hit]]:
    """Split located hits into doc-example exclusions and live findings."""
    # The rule compares the *matched token*, not the surrounding line, so it can only ever swallow
    # the exact documented bytes and cannot mask a live key that shares a line with them.
    excluded = [h for h in hits if any(rule == h.token for rule in DOC_EXAMPLES)]
    live = [h for h in hits if h not in excluded]
    return excluded, live


def exclusion_counterfact() -> bool:
    """Show the doc-example rule leaves a live-shaped key on the live side."""
    hits = find_hits([Blob("canary", CANARY_KEY, 0)], "aws-access-key", FAMILIES[0][1])
    excluded, live = split_hits(hits)
    print(
        f"[FACE] exclusion_counterfact: canary_hits={len(hits)} canary_excluded={len(excluded)} "
        f"canary_live={len(live)}"
    )
    return len(hits) == 1 and not excluded and len(live) == 1


def main() -> int:
    """Scan the staged blobs, print every face, and return the verdict."""
    if GIT is None:
        print("VERDICT: FAIL -- git executable not found")
        return 1
    blobs = [read_blob(p) for p in staged_paths()]
    scanned = sum(len(b.data) for b in blobs)
    reported = sum(b.reported for b in blobs)
    print(f"[INFO] staged_blobs={len(blobs)} scanned_bytes={scanned} git_reported_bytes={reported}")

    failures = run_controls()
    if scanned != reported:
        failures.append("byte-invariant")
    if not blobs:
        failures.append("empty-population")

    hits = [hit for name, pattern, _, _ in FAMILIES for hit in find_hits(blobs, name, pattern)]
    excluded, live = split_hits(hits)
    print(f"[FACE] hits={len(hits)} excluded={len(excluded)} live={len(live)}")
    for h in excluded:
        print(
            f"[EXCLUDED] {h.family} {h.path}:{h.line} len={len(h.token)} fp={_fp(h.token)} "
            "rule=aws-documentation-example-key"
        )
    for h in live:
        print(f"[LIVE] {h.family} {h.path}:{h.line} len={len(h.token)} fp={_fp(h.token)}")
    if not exclusion_counterfact():
        failures.append("exclusion-counterfact")

    target = blobs[0] if blobs else None
    if target is not None:
        poisoned = [
            Blob(b.path, b.data + b"\n" + PLANTED_GENERIC + b"\n", b.reported)
            if b.path == target.path
            else b
            for b in blobs
        ]
        clean_generic = sum(1 for h in live if h.family == "generic-assignment")
        poison_generic = find_hits(poisoned, "generic-assignment", FAMILIES[7][1])
        print(
            f"[FACE] positive_control_on_real_bytes: family=generic-assignment "
            f"clean_live={clean_generic} poisoned={len(poison_generic)} blob={target.path}"
        )
        if len(poison_generic) != clean_generic + 1:
            failures.append("positive-control")

    if live:
        failures.append("live-credentials")
    if failures:
        print(f"VERDICT: FAIL -- {failures}")
        return 1
    print(
        f"VERDICT: PASS -- every family fires on its witness and stays off its near-miss, "
        f"the staged bytes were read in full (scanned == git-reported over {len(blobs)} blobs), "
        "the exclusion rule leaves a live-shaped key live, and no live-shaped credential is staged."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
