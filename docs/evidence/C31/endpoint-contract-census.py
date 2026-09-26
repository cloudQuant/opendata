#!/usr/bin/env python3
r"""Measure the frontend endpoint contract: declared calls vs routes the app mounts.

C31 / AC-11 measurement surface (offline: no browser, no network, no warehouse).

The judgment plane lives in ``tests/test_frontend_endpoint_contract.py`` and compares
router-relative templates with a regex. This script measures the same thing through a
different door on purpose: it compares **segment lists** against the **mounted absolute
paths** read from ``opendata.main.app.routes``, so axios' ``baseURL`` hop is inside the
comparison rather than assumed. Two implementations agreeing is what makes the number a
measurement instead of an artefact of one matcher.

Both columns are printed: ``HEAD`` (this round's before state, read with
``git show HEAD:<path>``) and the working tree (after state). ``CENSUS_RC`` only says the
plane was read (floors on file count, call count and per-module count); the verdict is in
the MISSING / VERB_MISMATCH counters.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Final

REPO = Path(__file__).resolve().parents[3]
API_DIR = REPO / "frontend/src/api"
REQUEST_MODULE = REPO / "frontend/src/utils/request.ts"
GIT: Final = shutil.which("git") or "git"

URL_RE = re.compile(r"""url:\s*['"`](/[^'"`]*)['"`]""")
VERB_RE = re.compile(
    r"""request\s*\.\s*(get|post|put|patch|delete)(?:\s*<[^)]*>)?\s*\(\s*['"`](/[^'"`]*)['"`]""",
    re.S,
)
METHOD_RE = re.compile(r"""method:\s*['"`](\w+)['"`]""")
BASE_RE = re.compile(r"""baseURL:\s*['"`]([^'"`]+)['"`]""")

# Floors on the measurement itself. MIN_PER_FILE closes the hole the global floor cannot
# see: a module whose calls partly move to a syntax this script does not know.
MIN_FILES: Final = 8
MIN_CALLS: Final = 36
MIN_PER_FILE: Final = {
    "auth.ts": 5,
    "catalog.ts": 2,
    "data.ts": 8,
    "scripts.ts": 9,
    "settings.ts": 5,
    "tables.ts": 5,
    "tasks.ts": 5,
    "users.ts": 4,
}


def segments(template: str) -> list[str]:
    """Path template to segment list; ``${...}`` and ``{...}`` both become wildcards."""
    return [re.sub(r"^\$\{[^}]*\}$|^\{[^}]*\}$", "*", part) for part in template.split("/") if part]


def match(one: list[str], other: list[str]) -> bool:
    """Segment-wise equality where ``*`` on either side matches anything."""
    return len(one) == len(other) and all(
        a == "*" or b == "*" or a == b for a, b in zip(one, other, strict=True)
    )


def declared_calls(text: str) -> list[tuple[str, str]]:
    """(METHOD, path) pairs in source order, from both client syntaxes in use."""
    hits: list[tuple[int, str, str]] = [
        (m.start(), m.group(1).upper(), m.group(2)) for m in VERB_RE.finditer(text)
    ]
    for url in URL_RE.finditer(text):
        tail = text[url.end() :]
        nxt = tail.find("url:")
        method = METHOD_RE.search(tail if nxt == -1 else tail[:nxt])
        hits.append((url.start(), (method.group(1).upper() if method else "GET"), url.group(1)))
    hits.sort()
    return [(method, path) for _, method, path in hits]


def mounted_routes() -> list[tuple[str, list[str]]]:
    """(METHOD, segments) for every path the assembled app answers, from ``app.routes``."""
    from opendata.main import app

    out: list[tuple[str, list[str]]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        segs = segments(path)
        methods = sorted(getattr(route, "methods", None) or ())
        out.extend((method, segs) for method in methods if method not in {"HEAD", "OPTIONS"})
    return out


def git_show(rev_path: str) -> str:
    """One ``git show`` that refuses to read empty -- a bare path prints 0 bytes silently."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [GIT, "-C", str(REPO), "show", rev_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        message = f"git show {rev_path} read nothing (rc={result.returncode})"
        raise RuntimeError(message)
    return result.stdout


def head_api_files() -> dict[str, str]:
    """HEAD's frontend api modules, keyed by file name."""
    listing = subprocess.run(  # noqa: S603  # nosec B603
        [GIT, "-C", str(REPO), "ls-tree", "--name-only", "HEAD", "frontend/src/api/"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return {Path(entry).name: git_show(f"HEAD:{entry}") for entry in listing}


def adjudicate(
    label: str,
    calls: dict[str, list[tuple[str, str]]],
    table: list[tuple[str, list[str]]],
    prefix: str,
) -> tuple[int, int]:
    """Judge after prepending baseURL; returns (missing paths, verb mismatches) or (-1, -1)."""
    total = sum(len(v) for v in calls.values())
    print(f"\n===== {label}: {len(calls)} modules / {total} declared calls =====")
    if len(calls) < MIN_FILES or total < MIN_CALLS:
        print(f"  VACUOUS: this column only read {total} calls, verdict not established")
        return -1, -1
    blind = [
        f"{name}={len(calls.get(name, []))}<{bound}"
        for name, bound in MIN_PER_FILE.items()
        if len(calls.get(name, [])) < bound
    ]
    if blind:
        print(f"  VACUOUS: per-module floor broken -> {'; '.join(blind)}")
        return -1, -1
    missing = verb_bad = 0
    for name in sorted(calls):
        for method, path in calls[name]:
            probe = segments(prefix + path)
            hits = [m for m, segs in table if match(probe, segs)]
            if not hits:
                missing += 1
                print(f"  MISSING  {method:6s} {path:38s} <- api/{name}")
            elif method not in hits:
                verb_bad += 1
                verbs = "/".join(sorted(set(hits)))
                print(f"  VERB     {method:6s} {path:38s} <- api/{name} (table: {verbs})")
    print(f"  {label}: MISSING={missing} VERB_MISMATCH={verb_bad}")
    return missing, verb_bad


def print_column(label: str, calls: dict[str, list[tuple[str, str]]]) -> None:
    """Show what the extractor read per module, so an empty read is visible as such."""
    print(f"\n----- {label} per-module readout -----")
    for name in sorted(calls):
        joined = " ".join(f"{m} {p}" for m, p in calls[name])
        print(f"  {name:16s} {len(calls[name]):3d}  {joined}")


def main() -> int:
    """Read both columns, judge both, and print the readings."""
    sys.path.insert(0, str(REPO))
    base_match = BASE_RE.search(REQUEST_MODULE.read_text(encoding="utf-8"))
    if base_match is None:
        print("CENSUS_RC=2  VACUOUS: no literal baseURL in request.ts")
        return 2
    prefix = base_match.group(1)
    print(f"axios baseURL = {prefix} (declared paths get it prepended before comparing)")

    table = mounted_routes()
    print(f"mounted (METHOD, path) pairs in app.routes = {len(table)}")
    if len(table) < 50:
        print("CENSUS_RC=2  VACUOUS: the mount table is empty; 0 missing would mean nothing")
        return 2

    before = {name: declared_calls(text) for name, text in sorted(head_api_files().items())}
    after = {
        path.name: declared_calls(path.read_text(encoding="utf-8"))
        for path in sorted(API_DIR.glob("*.ts"))
    }
    print_column("HEAD (before this round)", before)
    print_column("working tree (after this round)", after)

    readings = (
        adjudicate("HEAD (before)", before, table, prefix),
        adjudicate("working tree (after)", after, table, prefix),
    )
    if any(missing < 0 or verb < 0 for missing, verb in readings):
        print("CENSUS_RC=2  VACUOUS: a column fell below its floor, verdict not established")
        return 2

    columns = (("HEAD", before), ("工作区", after))
    for (label, calls), (missing, verb) in zip(columns, readings, strict=True):
        total = sum(len(v) for v in calls.values())
        print(f"\n读数 {label}: {len(calls)} 文件 / {total} 条 → MISSING={missing} VERB={verb}")

    print("CENSUS_RC=0（只表两栏都量到了，判定看 MISSING / VERB_MISMATCH）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
