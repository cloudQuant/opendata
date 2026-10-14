"""Show that the C101 round's ``cast`` sites sit above a live runtime guard.

A ``cast`` tells mypy "trust me"; the only thing that makes that honest is a check that already
refuses the un-trusted shape at runtime. Seven of the round's casts narrow a value read off an
untyped HTTP response, and each of those seven is followed within one statement by a guard that
raises the provider's transport error. This probe reads the lines beneath each cast and requires
that guard, so the round's face cannot be re-read as "a cast silenced mypy and nothing else".

The round's other type edits that this probe deliberately does *not* grade -- five
``no-any-return`` sites and the ``_primary_dealer`` ``cast("Decimal", ...)`` -- name no guard
because none exists adjacent to them, and are graded by the executed tests listed in
``face.md`` instead.

Arm ``--arm`` deletes one guard from an in-memory copy and requires the site to read unguarded:
without it, a predicate that never matches would also print "all guarded".
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

#: ``(file, 1-based line of the cast, expected substring, guard pattern)`` -- the seven
#: guard-backed casts of the C101 type round, anchored on the line numbers the round committed.
CAST_SITES: list[tuple[str, int, str, str]] = [
    ("opendata/data/providers/sec/models/_common.py", 206, 'cast("int"', r"isinstance\(status, bool\)"),
    ("opendata/data/providers/sec/models/_common.py", 209, 'cast("bytes"', r"not isinstance\(body, bytes\)"),
    (
        "opendata/data/providers/congress_gov/models/_client.py",
        158,
        'cast("int"',
        r"type\(status\) is not int",
    ),
    (
        "opendata/data/providers/congress_gov/models/_client.py",
        219,
        'cast("int"',
        r"type\(status\) is not int",
    ),
    (
        "opendata/data/providers/congress_gov/models/_client.py",
        222,
        'cast("bytes"',
        r"not isinstance\(body, bytes\)",
    ),
    (
        "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        352,
        'cast("int"',
        r"type\(status\) is not int",
    ),
    (
        "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        353,
        'cast("bytes"',
        r"type\(body\) is not bytes",
    ),
]

FALSIFIED_FILE, FALSIFIED_LINE = CAST_SITES[0][0], CAST_SITES[0][1]
FALSIFIED_GUARD_LINE = (
    "        if isinstance(status, bool) or not isinstance(status, int):\n"
    '            raise SecProviderError("SEC_TRANSPORT_ERROR")\n'
)


def guarded(
    rel: str, line: int, expect: str, pattern: str, *, drop: str = ""
) -> tuple[bool, list[str]]:
    """Return whether the guard pattern follows the cast line within the next ten lines."""
    lines = (REPO / rel).read_text(encoding="utf-8").replace(drop, "", 1).splitlines()
    if line > len(lines) or expect not in lines[line - 1]:
        return False, [f"line {line} of {rel} no longer reads: {lines[line - 1] if line <= len(lines) else 'EOF'}"]
    tail = [text for text in lines[line : line + 10] if text.strip()]
    return any(re.search(pattern, text) for text in tail), [text.strip() for text in tail][:3]


def main(argv: list[str] | None = None) -> int:
    """Grade each guard-backed cast, or run the falsification arm."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="delete one guard and require a red reading")
    args = parser.parse_args(argv)

    if args.arm:
        rel, line, expect, pattern = CAST_SITES[0]
        with_guard, _ = guarded(rel, line, expect, pattern)
        without, _ = guarded(rel, line, expect, pattern, drop=FALSIFIED_GUARD_LINE)
        bites = with_guard and not without
        print(
            f"arm: {rel}:{line} guard_present={with_guard} guard_deleted={without}"
            f" -> {'PASS' if bites else 'FAIL'}"
        )
        return 0 if bites else 1

    unguarded = 0
    print(f"population: guard_backed_casts={len(CAST_SITES)}")
    for rel, line, expect, pattern in CAST_SITES:
        ok, tail = guarded(rel, line, expect, pattern)
        unguarded += 0 if ok else 1
        print(f"{'GUARDED' if ok else 'UNGUARDED'} {rel}:{line} :: {expect}")
        if not ok:
            print(f"   following lines: {tail}")
    print(f"face: guarded={len(CAST_SITES) - unguarded} unguarded={unguarded}")
    return 0 if unguarded == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
