"""C94 staged-blob credential scan, run before the census/evidence commits.

The requirement this device serves is: a published "no credentials" zero must be a
measurement, not an inert regex. Three faces, all printed:

  INVARIANT  every staged blob is read out of the index (``git show :path``), and
             ``scanned_bytes == sum(git cat-file -s :path)`` per file and in total, so a
             path that failed to resolve cannot hide behind a zero hit count.
  REAL       the staged blob set reads 0 credential hits.
  CONTROL    both directions must be able to differ:
             (a) PLANT  -- a synthetic token injected IN MEMORY into the smallest staged
                 blob flips this scanner 0 -> 1 (nothing on disk is rewritten);
             (b) ALLOW  -- the AWS *documentation* example key pair, which the iteration-2
                 ledger quotes verbatim as its own counterfact fixture (开发验收记录.md:904),
                 must read 0. Without arm (b) the zero would be bought with a broad
                 allowlist rather than with a named literal.

First reading of this round, published rather than silently corrected: the scanner's
``aws_access_key`` pattern hit the ledger once at offset 325065, and that byte run IS the
AWS docs example pair -- so the first run printed ``REAL hits=1 / VERDICT all=False``.
The fix was to name that literal in the allowlist (arm (b) below), not to drop the pattern.

The repo's own judge is a separate face and is NOT recomputed here: ``make secret-check``
(gitleaks over 363 commits / ~153.88 MB, ``no leaks found``) is recorded in the README.
"""

from __future__ import annotations

import hashlib
import re
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LEDGER_MARK = "开发验收记录.md"

PATTERNS: dict[str, re.Pattern[bytes]] = {
    "aws_access_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "slack_token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(rb"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "generic_kv": re.compile(
        rb"(?i)(api[_-]?key|secret|token|passwd|password|access[_-]?key)"
        rb"\s*[:=]\s*[\"']?([A-Za-z0-9+/_\-.=]{16,})[\"']?"
    ),
    "url_credential": re.compile(rb"[a-z][a-z0-9+.\-]*://[^/\s:@\s]+:([^/@\s]{6,})@"),
}

# Named literals that are public documentation fixtures, not credentials. Kept as exact
# byte strings so the allowlist cannot widen into a prefix or a regex.
ALLOW_LITERALS: tuple[bytes, ...] = (
    b"AKIAIOSFODNN7EXAMPLE",
    b"wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    b"AKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
)

PLANT_FIXTURE = b"at_AbCdEf0123456789AbCdEf0123456789"  # planted in memory only, never on disk
PLACEVALUE_PREFIXES = (b"$", b"{", b"os.environ")


def git(*args: str) -> bytes:
    """Run a read-only git argv (no shell) and return stdout bytes."""
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        ("git", *args),  # noqa: S607  # fixed argv, git resolved on PATH
        check=True,
        cwd=ROOT,
        capture_output=True,
    )
    return proc.stdout


def staged_paths() -> list[str]:
    """List paths in the index, NUL-separated so non-ASCII paths cannot be re-quoted."""
    return [p.decode() for p in git("diff", "--cached", "--name-only", "-z").split(b"\0") if p]


def mask(runs: bytes) -> bytes:
    """Long opaque runs never reach stdout, so a hit names its shape not its value."""
    return re.sub(rb"[A-Za-z0-9+/=_\-]{8,}", lambda m: b"<MASKED:%d>" % len(m.group()), runs)


def hits_in(blob: bytes) -> list[tuple[str, int, bytes]]:
    """Return (pattern, offset, masked value) for every non-allowlisted credential shape."""
    found: list[tuple[str, int, bytes]] = []
    for name, pat in PATTERNS.items():
        for m in pat.finditer(blob):
            value = m.group(m.lastindex or 0)
            if value in ALLOW_LITERALS:
                continue
            if value.startswith(PLACEVALUE_PREFIXES):
                continue
            found.append((name, m.start(), mask(value)))
    return found


def main() -> int:
    """Print the three faces and exit non-zero unless all of them hold."""
    paths = staged_paths()
    rows: list[tuple[str, int, int, str]] = []
    for p in paths:
        blob = git("show", f":{p}")
        declared = int(git("cat-file", "-s", f":{p}").strip())
        rows.append((p, len(blob), declared, hashlib.sha256(blob).hexdigest()[:12]))

    scanned = sum(r[1] for r in rows)
    declared_total = sum(r[2] for r in rows)
    invariant_ok = scanned == declared_total and all(r[1] == r[2] for r in rows)

    real_hits: list[tuple[str, str, int, bytes]] = []
    for p, _n, _s, _h in rows:
        for name, off, value in hits_in(git("show", f":{p}")):
            real_hits.append((p, name, off, value))

    # control (a): plant a synthetic token in memory into the smallest staged blob
    target = min(rows, key=lambda r: r[1])
    tblob = git("show", f":{target[0]}")
    before = hits_in(tblob)
    after = hits_in(tblob + b'\nTIINGO_TOKEN = "' + PLANT_FIXTURE + b'"\n')
    plant_ok = before == [] and len(after) == 1 and after[0][0] == "generic_kv"

    # control (b): the AWS docs example pair must read 0 through the same patterns
    example = b"access_key = " + ALLOW_LITERALS[0] + b"\nsecret_key = " + ALLOW_LITERALS[1]
    allow_ok = hits_in(example) == []
    # and its own positive half: an AWS key that is not the docs literal must still bite
    example_bites = len(hits_in(b"aws = " + b"AKIA" + b"ZZZZZZZZZZZZZZZZ")) == 1

    print(f"STAGED files={len(rows)} scanned_bytes={scanned} cat_file_bytes={declared_total}")
    for p, n, s, h in sorted(rows):
        shown = "WITHHELD" if LEDGER_MARK in p else str(n)
        cat = "-" if LEDGER_MARK in p else str(s)
        print(f"  blob sha256={h} bytes={shown} cat_file={cat} file={p}")
    print(f"INVARIANT per-file and total scanned==cat-file: {invariant_ok}")
    print(f"REAL hits={len(real_hits)} (must be 0)")
    for p, name, off, value in real_hits:
        print(f"   HIT {p} :: {name} offset {off} value {value.decode(errors='replace')}")
    print(
        f"PLANT blob_bytes={len(tblob)} hits_before={len(before)} hits_after={len(after)} "
        f"(in-memory only, no blob rewritten) -> {plant_ok}"
    )
    print(
        f"ALLOW aws-docs-example-pair hits={len(hits_in(example))} (must be 0) -> {allow_ok}; "
        f"non-example AWS key still bites -> {example_bites}"
    )
    ok = invariant_ok and not real_hits and plant_ok and allow_ok and example_bites
    print(
        f"VERDICT invariant={invariant_ok} real_zero={not real_hits} plant={plant_ok} "
        f"allow={allow_ok} allow_two_sided={example_bites} all={ok}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
