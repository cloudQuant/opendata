#!/usr/bin/env python3
r"""Read what the frontend unit plane actually runs, and what it never ran.

C29 / AC-17 measurement surface (offline, no network, no warehouse access).

Why this file exists: `make gate` closes the frontend plane on the line
``Test Files 8 passed (8) / Tests 79 passed (79)``, and every acceptance row
since A0 repeated that number as if it described the frontend test surface. It
described only what the collector was allowed to reach. This script walks the
disk first and asks vitest second, so a file that exists and is not collected
shows up as a count difference instead of as a passing plane.

Deliberately an independent reading: it re-implements the file walk and does
not import ``scripts/quality/frontend_test_collection.py``, the judgment module
this round adds. Two ways of measuring the same fact, one judgment.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND = REPO_ROOT / "frontend"

#: The shapes vitest's own ``include`` default would match.
TEST_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
#: Never project tests: installed packages and build output.
SKIP_DIRS = frozenset({"node_modules", "dist", "coverage", ".vite"})
#: The playwright plane is a different runner with a live server; it is named
#: here so its absence from the vitest count is a stated boundary, not silence.
E2E_DIR = "e2e"

#: A test call is ``it(...)``/``test(...)`` including the ``.each``/``.skip``
#: dotted forms; ``it.each([...])("name", ...)`` counts its call site once.
_CASE_RE = re.compile(r"(?m)^\s*(?:it|test)(?:\.\w+)*\s*\(")
_EXCLUDE_ARRAY_RE = re.compile(r"(?P<key>exclude):\s*\[(?P<body>[^\]]*)\]", re.S)
_GLOB_RE = re.compile(r"['\"](?P<glob>[^'\"]+)['\"]")
#: Which object an ``exclude`` array belongs to decides what it silences:
#: ``test.exclude`` hides files from the collector, ``coverage.exclude`` only
#: hides them from the coverage denominator. Reading one as the other would
#: over-claim this census, so each glob is labelled by its own array.
_OWNERS = ("test", "coverage")


def walk_on_disk(root: Path) -> list[str]:
    """Return repo-relative posix paths of every test file on disk."""
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".ts", ".tsx"}:
            continue
        if not path.name.endswith(TEST_SUFFIXES):
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in Path(relative).parts):
            continue
        found.append(f"frontend/{relative}")
    return found


def case_counts(files: list[str]) -> dict[str, int]:
    """Count call sites per file — the number a passing line hides."""
    counts: dict[str, int] = {}
    for relative in files:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        counts[relative] = len(_CASE_RE.findall(text))
    return counts


def ask_vitest_collected() -> list[str] | None:
    """Ask vitest which files it collects, or ``None`` when it cannot answer."""
    npx = shutil.which("npx")
    if npx is None:
        return None
    result = subprocess.run(  # noqa: S603  # nosec B603
        [npx, "vitest", "list", "--json"],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        print(f"vitest list failed rc={result.returncode}: {result.stderr.strip()[:400]}")
        return None
    payload: Any = json.loads(result.stdout)
    if not isinstance(payload, list):
        print(f"unexpected vitest list payload: {type(payload).__name__}")
        return None
    files = {str(entry.get("file", "")) for entry in payload if isinstance(entry, dict)}
    collected: list[str] = []
    for entry in sorted(files):
        if not entry:
            continue
        collected.append(_as_repo_path(Path(entry)))
    return collected


def _as_repo_path(absolute: Path) -> str:
    """Render an absolute path the way the disk walk renders its paths."""
    try:
        return f"frontend/{absolute.relative_to(FRONTEND).as_posix()}"
    except ValueError:
        return absolute.as_posix()


def declared_excludes(config: Path) -> list[tuple[str, str]]:
    """Every ``exclude: [...]`` glob with the object that owns it.

    Attribution is by the nearest preceding ``test:`` / ``coverage:`` key, so a
    glob whose owner cannot be found is reported as ``"unknown"`` rather than
    guessed at — the caller treats that as "this census could not read the
    plane", which is the same stance as a judge that refuses to infer.
    """
    text = config.read_text(encoding="utf-8")
    labelled: list[tuple[str, str]] = []
    for array in _EXCLUDE_ARRAY_RE.finditer(text):
        head = text[: array.start()]
        owner = max(
            (key for key in _OWNERS if f"{key}:" in head),
            key=lambda key: head.rindex(f"{key}:"),
            default="unknown",
        )
        globs = [match.group("glob") for match in _GLOB_RE.finditer(array.group("body"))]
        labelled.extend((owner, glob) for glob in globs)
    return labelled


def git_provenance(glob: str) -> str:
    """The commit that added (or removed) a declared exclude glob."""
    git = shutil.which("git")
    if git is None:
        return "git unavailable"
    result = subprocess.run(  # noqa: S603  # nosec B603
        [
            git,
            "log",
            "--format=%h %ad %s",
            "--date=short",
            "-S",
            glob,
            "--",
            "frontend/vite.config.ts",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else "no commit touching this glob"


#: Generated files name every component without using any of them; counting
#: them as references would turn "exported, never imported" into "in use".
GENERATED = frozenset({"src/components.d.ts", "src/auto-imports.d.ts"})
#: The barrel re-exports the whole set, so it is not a use site either.
BARREL = "src/components/common/index.ts"


def component_usage(component: str) -> tuple[list[str], bool]:
    """Return a component's real use sites and whether a barrel exports it.

    Whether the subject is used anywhere decides what a fixed test is worth:
    a test over a component no view imports guards code nothing calls, and
    that belongs in the record as a question rather than as a green line.
    """
    definition = f"{component}.vue"
    use_sites: list[str] = []
    in_barrel = False
    for source in sorted(FRONTEND.rglob("*")):
        if not source.is_file() or not source.name.endswith((".ts", ".vue")):
            continue
        relative = source.relative_to(FRONTEND).as_posix()
        if any(part in SKIP_DIRS for part in Path(relative).parts):
            continue
        if relative in GENERATED or "__tests__" in relative or source.name == definition:
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        if not re.search(rf"\b{re.escape(component)}\b", text):
            continue
        if relative == BARREL:
            in_barrel = True
        else:
            use_sites.append(relative)
    return use_sites, in_barrel


def main() -> int:
    """Print the census; exit 2 when the second reading could not be taken."""
    on_disk = walk_on_disk(FRONTEND)
    collected = ask_vitest_collected()
    if collected is None:
        print("VACUOUS: vitest could not be asked; nothing below is a measurement.")
        return 2

    disk_cases = case_counts(on_disk)
    vitest_cases = case_counts([name for name in collected if name in set(on_disk)])
    never_collected = sorted(set(on_disk) - set(collected))
    collected_off_disk = sorted(set(collected) - set(on_disk))

    print(f"on-disk test files      : {len(on_disk)}")
    print(f"collected by vitest     : {len(collected)}")
    print(f"on-disk call sites      : {sum(disk_cases.values())}")
    print(f"collected call sites    : {sum(vitest_cases.values())}")
    print()
    print("== per-file (on-disk call sites, collected?) ==")
    for relative in on_disk:
        state = "collected" if relative in set(collected) else "NEVER COLLECTED"
        print(f"  {disk_cases[relative]:>3}  {state:<15} {relative}")
    print()
    print("== never collected (this is what the passing line did not cover) ==")
    if not never_collected:
        print("  none")
    for name in never_collected:
        print(f"  {name}")
    src_silence = [name for name in never_collected if "/src/" in name]
    e2e_silence = [name for name in never_collected if f"/{E2E_DIR}/" in name]
    print(f"NEVER_COLLECTED: {len(never_collected)} files total")
    print(
        f"  src plane (a gate blind spot): {len(src_silence)} files / "
        f"{sum(disk_cases[name] for name in src_silence)} call sites"
    )
    print(
        f"  e2e plane (separate runner)  : {len(e2e_silence)} files / "
        f"{sum(disk_cases[name] for name in e2e_silence)} call sites"
    )
    if collected_off_disk:
        print()
        print("== collected but outside the walk (a boundary to name, not fix) ==")
        for relative in collected_off_disk:
            print(f"  {relative}")
    print()
    print("== declared excludes in frontend/vite.config.ts ==")
    labelled = declared_excludes(FRONTEND / "vite.config.ts")
    for owner, glob in labelled:
        print(f"  {owner:<9} {glob:<35} <- {git_provenance(glob)}")
    collection_rules = [glob for owner, glob in labelled if owner == "test"]
    print(f"declared exclude globs  : {len(labelled)}")
    print(f"  owned by test (collector): {len(collection_rules)} -> {collection_rules}")
    print(
        f"  owned by coverage (denominator only): "
        f"{sum(1 for owner, _ in labelled if owner == 'coverage')}"
    )
    if any(owner == "unknown" for owner, _ in labelled):
        print("VACUOUS: an exclude array could not be attributed to test or coverage.")
        return 2
    print()
    print("== e2e plane (playwright runner, never in `make gate`) ==")
    e2e = [name for name in on_disk if f"{E2E_DIR}/" in name]
    counts = Counter(Path(name).parent.as_posix() for name in e2e)
    for parent, number in sorted(counts.items()):
        print(f"  {number} file(s) under {parent} — separate runner")
    print()
    print("== is the subject of a silenced test used anywhere? ==")
    if not src_silence:
        print("  nothing was silenced, so there is nothing to ask about")
    for relative in src_silence:
        component = Path(relative).name.split(".")[0]
        use_sites, in_barrel = component_usage(component)
        barrel_note = " exported by the barrel" if in_barrel else ""
        print(f"  {component:<14} {len(use_sites)} use site(s){barrel_note}: {use_sites}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
