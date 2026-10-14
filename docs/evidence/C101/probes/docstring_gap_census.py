"""List the public callables the repo's own A2 judge finds without a docstring.

``make public-api-quality`` prints one coverage fraction and exits non-zero; that face says
how far from 100% the tree is but not which symbols are missing. This probe calls the same
judge (``public_api.collect_symbols`` over ``public_api.iter_files``) and publishes the
per-file population, so a repair can be graded by "the listed set is empty" rather than by
a percentage moving.

Arm ``--arm`` forges one module with a single undocumented public function and shows the
judge reports exactly that symbol: without it, an empty list here would be indistinguishable
from a judge that stopped finding anything.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.quality import public_api

FORGED_SOURCE = """
def documented() -> int:
    \"\"\"Return one.\"\"\"
    return 1


def undocumented() -> int:
    return 2
"""


def missing_symbols() -> list[public_api.Symbol]:
    """Return every public symbol the judge flags for a missing docstring."""
    found: list[public_api.Symbol] = []
    for path in public_api.iter_files():
        rel = path.relative_to(public_api.REPO_ROOT).as_posix()
        found.extend(
            symbol
            for symbol in public_api.collect_symbols(path.read_text(encoding="utf-8"), rel)
            if not symbol.has_docstring
        )
    return found


def run_arm() -> int:
    """Show the judge still bites on a forged undocumented symbol."""
    forged = public_api.collect_symbols(FORGED_SOURCE, "__arm__/forged.py")
    flagged = [symbol.qualname for symbol in forged if not symbol.has_docstring]
    expected = ["undocumented"]
    ok = flagged == expected
    print(
        f"arm: forged_symbols={len(forged)} flagged={flagged} expected={expected} -> "
        f"{'PASS' if ok else 'FAIL'}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """Print the census, or run the falsification arm."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", action="store_true", help="run the falsification arm only")
    args = parser.parse_args(argv)
    if args.arm:
        return run_arm()

    missing = missing_symbols()
    by_file = Counter(symbol.file for symbol in missing)
    print(
        f"population: files_in_scope={len(public_api.iter_files())} "
        f"undocumented_symbols={len(missing)}"
    )
    for file, count in by_file.most_common():
        print(f"  {file}: {count}")
    for symbol in missing:
        print(f"MISSING {symbol.file}:{symbol.line} {symbol.qualname}")
    return 0 if not missing else 2


if __name__ == "__main__":
    raise SystemExit(main())
