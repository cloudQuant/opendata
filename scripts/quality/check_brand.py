#!/usr/bin/env python3
"""Brand-residue check (acceptance AC-1).

Two independent assertions over the whole repository:

1. **Brand tokens** — no ``akshare_web`` / ``akshare_user`` / ``akshare_data`` and
   friends anywhere outside the historical planning documents. These are the
   pre-rename identifiers and must be zero.
2. **Package references** — no ``from app.`` / ``import app.`` leftovers from the
   ``app/`` -> ``opendata/`` rename.

The bare word ``akshare`` is *not* a brand token: it is the upstream library and
data source name, and must keep appearing in attribution files and in the
integration layer until milestone A2/B5 removes the import. Occurrences of it
are reported as context, not as a failure.

Build artefacts, the vendored tree and the historical plan under
``docs/迭代计划/`` are out of scope.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

BRAND_TOKENS = (
    "akshare_web",
    "akshare-web",
    "akshare_user",
    "akshare_data",
    "akshare_mysql",
    "akshare_warehouse",
    "akshare_redis",
    "akshare_backend",
    "akshare_frontend",
    "akshare_certbot",
    "akshare_network",
    "akshare_root_pass",
    "akshare_pass",
)

APP_REFERENCE_PATTERN = re.compile(r"^\s*(from app[.\s]|import app[.\s])")

IGNORED_DIR_PARTS = frozenset(
    {
        ".git",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        "coverage",
        "node_modules",
        "dist",
        "__pycache__",
        "logs",
        ".venv",
        "venv",
        ".benchmarks",
    }
)

IGNORED_DIR_SUFFIXES = (".egg-info",)

# The historical plan quotes the pre-rename names by design. Evidence logs record
# verbatim command output, which necessarily contains the token names they check.
IGNORED_PATHS = ("docs/迭代计划", "docs/evidence")

# These documents name the predecessor platform when explaining provenance
# ("this repo was refactored from a copy of ..."). That is a deliberate
# historical reference, not a stale brand claim.
PROVENANCE_FILES = (
    "README.md",
    "CODE_QUALITY.md",
)

# These tools have to name the identifiers they search for or rewrite, so the
# tokens legitimately appear in their own source.
WHITELISTED_FILES = (
    "scripts/codemod/rename_app_to_opendata.py",
    "scripts/quality/check_brand.py",
)


@dataclass(frozen=True)
class Hit:
    """A single offending line.

    Attributes:
        file: Repo-relative POSIX path.
        line: 1-based line number.
        text: Stripped line content.
    """

    file: str
    line: int
    text: str


def _is_skipped(rel: Path, *, allow_provenance: bool = False) -> bool:
    if any(part in IGNORED_DIR_PARTS for part in rel.parts):
        return True
    if any(part.endswith(IGNORED_DIR_SUFFIXES) for part in rel.parts):
        return True
    rel_posix = rel.as_posix()
    if rel_posix in WHITELISTED_FILES:
        return True
    if not allow_provenance and rel_posix in PROVENANCE_FILES:
        return True
    return any(rel_posix.startswith(prefix) for prefix in IGNORED_PATHS)


def _iter_text_files(*, allow_provenance: bool = False) -> list[Path]:
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if _is_skipped(rel, allow_provenance=allow_provenance):
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files.append(path)
    return files


def scan_token(token: str) -> list[Hit]:
    """Return every line containing ``token`` (case-insensitive)."""
    hits: list[Hit] = []
    lowered = token.lower()
    for path in _iter_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if lowered in line.lower():
                hits.append(Hit(rel, number, line.strip()))
    return hits


def scan_app_references() -> list[Hit]:
    """Return every ``from app.`` / ``import app.`` leftover."""
    hits: list[Hit] = []
    for path in _iter_text_files(allow_provenance=True):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if APP_REFERENCE_PATTERN.search(line):
                hits.append(Hit(rel, number, line.strip()))
    return hits


def run() -> int:
    """Run both assertions and report the result."""
    failures = 0

    print("== brand tokens ==")
    for token in BRAND_TOKENS:
        hits = scan_token(token)
        if hits:
            failures += len(hits)
            print(f"FAIL: {len(hits)} occurrence(s) of {token!r}:")
            for hit in hits[:20]:
                print(f"  {hit.file}:{hit.line}: {hit.text}")
        else:
            print(f"ok   {token}")

    print("== app package references ==")
    app_hits = scan_app_references()
    if app_hits:
        failures += len(app_hits)
        print(f"FAIL: {len(app_hits)} leftover 'app' reference(s):")
        for hit in app_hits[:20]:
            print(f"  {hit.file}:{hit.line}: {hit.text}")
    else:
        print("ok   no 'from app.' / 'import app.' leftovers")

    print("== context: upstream 'akshare' occurrences (not a failure) ==")
    for hit in scan_token("akshare")[:5]:
        print(f"  {hit.file}:{hit.line}")

    if failures:
        print(f"FAIL: {failures} brand/rename problem(s) found.", file=sys.stderr)
        return 1
    print("OK: no brand residue and no rename leftovers.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the brand-residue check from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
