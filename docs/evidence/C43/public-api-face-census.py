#!/usr/bin/env python3
"""Split "everything outside public_api.py's fixed scope list" into populations.

AC-17|07 says **新增**, while the gate member measures a hard-coded tuple of directories. Whether
that tuple reaches every new first-party file is a question about numbers, not about intent, so
this script prints them: how many public callables live outside the list, and which population
each one belongs to (tests, the evidence archive, ported trees the A2 layer excludes, first-party
code the list does reach, and first-party code it does not).

Both rulers are read out of the tools themselves -- ``public_api.A2_SCOPE`` and its
``collect_symbols`` walker for the standard, ``a2_check.resolve_files(None)`` for what counts as a
new or touched file -- so the census cannot drift away from the gate it describes.

Recompute::

    python docs/evidence/C43/public-api-face-census.py
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess  # nosec B404
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from types import ModuleType

REPO: Final = Path(__file__).resolve().parents[3]
GIT: Final = shutil.which("git") or "git"


def load(name: str, rel: str) -> ModuleType:
    """Import one of the repository's own quality scripts by repository-relative path."""
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{rel} cannot be loaded as a module")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: a @dataclass in the target resolves itself through the registry.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def bucket(rel: str, scope: tuple[str, ...], a2_set: set[str], ported: tuple[str, ...]) -> str:
    """Name the population a path belongs to, in the order the standards diverge."""
    if any(rel == entry or rel.startswith(f"{entry}/") for entry in scope):
        return "1 in public_api.py's fixed scope list (what the gate measures)"
    if rel.startswith("tests/"):
        return "2 tests/ -- ruff switches D/ANN off there, a2_check darkens mypy and bandit"
    if rel.startswith("docs/"):
        return "3 docs/ -- reproduction scripts archived as readings, not shipped callables"
    if rel.startswith(ported):
        return "4 root trees a2_check itself excludes (ported / frontend / migrations)"
    if rel in a2_set:
        return "5 first-party, new or touched, OUTSIDE the scope list -- the widened face"
    return "6 first-party A1 legacy, untouched since the A0 baseline"


def main() -> int:
    """Print the population split and how many callables in each miss the standard."""
    pub = load("census_pubapi", "scripts/quality/public_api.py")
    a2 = load("census_a2check", "scripts/quality/a2_check.py")
    scope: tuple[str, ...] = tuple(str(entry) for entry in pub.A2_SCOPE)
    a2_files: list[str] = sorted(a2.resolve_files(None) or [])
    a2_set = set(a2_files)
    ported: tuple[str, ...] = tuple(f"{name}/" for name in sorted(a2.EXCLUDED_ROOT_DIRS))
    tracked = subprocess.run(  # noqa: S603  # nosec B603
        [GIT, "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        shell=False,
        check=True,
    ).stdout.splitlines()

    universe = sorted(set(tracked) | a2_set)
    files: dict[str, int] = defaultdict(int)
    callables: dict[str, int] = defaultdict(int)
    below: dict[str, int] = defaultdict(int)
    offenders: dict[str, list[str]] = defaultdict(list)
    for rel in universe:
        if not rel.endswith(".py"):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        key = bucket(rel, scope, a2_set, ported)
        symbols = pub.collect_symbols(path.read_text(encoding="utf-8"), rel)
        failing = [symbol for symbol in symbols if symbol.problems()]
        files[key] += 1
        callables[key] += len(symbols)
        below[key] += len(failing)
        offenders[key].extend(f"  {rel}:{symbol.line} {symbol.qualname}" for symbol in failing)

    print(f"# public-API populations over {len(universe)} tracked-or-A2 paths")
    print(f"{'population':<72} {'files':>6} {'callables':>10} {'below':>7}")
    for key in sorted(files):
        print(f"{key:<72} {files[key]:>6} {callables[key]:>10} {below[key]:>7}")
    print("\n# the widened population, file by file (public callables / below standard):")
    wide = "5 first-party, new or touched, OUTSIDE the scope list -- the widened face"
    for rel in sorted(name for name in universe if bucket(name, scope, a2_set, ported) == wide):
        path = REPO / rel
        if not path.is_file():
            continue
        symbols = pub.collect_symbols(path.read_text(encoding="utf-8"), rel)
        bad = sum(1 for symbol in symbols if symbol.problems())
        print(f"  {rel:<64} {len(symbols):>4} {bad:>6}")
    for key in sorted(offenders):
        if below[key]:
            print(f"\n# below standard in {key} (first 12):")
            print("\n".join(sorted(offenders[key])[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
