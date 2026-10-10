#!/usr/bin/env python3
"""Show that the C64 roster deltas are the carrier's, not the census regex's.

``AC-8|08`` reads ``roster_table_delta=3`` and ``roster_rows_delta=101`` out of
``docs/evidence/C64/warehouse-readonly-inventory.txt``: the inventory header declares ``tables 12
estimated rows 12461018`` while the archived body lists 9 tables summing to 12460917 rows. Those
two numbers have a second possible cause -- the census might simply fail to spell 3 of the 12 body
lines -- and a delta that is really a parse miss could never be repaired by re-running the
inventory. This instrument resolves which cause holds by counting, with the probe module's own
``C86_WAREHOUSE_TABLE``, every body line that starts a tuple and how many of them the census
matches, then prints the unmatched ones verbatim.

Unmatched body lines is the arm that bites: it is the number the probe's delta would have to equal
for the defect to be mine rather than the carrier's, so it is printed beside the delta instead of
assumed away. The row sum is recomputed from the matched tuples only, so the rows delta stays on the
same population as the tables delta. A zero on that arm is only worth printing next to the non-zero
it could have reported, so ``--self-check`` recounts the same carrier with one invented table line
whose name the census's character class cannot spell (a hyphen) and requires exactly that line to
come back unmatched.

Run: python3 docs/evidence/C86/roster-census-coverage.py
     python3 docs/evidence/C86/roster-census-coverage.py --self-check
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Final

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.quality.acceptance_item_probe import (  # noqa: E402
    C86_WAREHOUSE_REL,
    C86_WAREHOUSE_TABLE,
    first_capture,
)

ROSTER: Final = REPO_ROOT / C86_WAREHOUSE_REL
#: A table name the census cannot spell: ``-`` is outside ``\\w+``.
TAMPERED_LINE: Final = "('ods_hyphen-name', 7, 8, 0)"


def rows_of(text: str) -> dict[str, int]:
    """Return the census-visible table -> row-count map."""
    return {name: int(rows) for name, rows in C86_WAREHOUSE_TABLE.findall(text)}


def report(text: str, label: str) -> int:
    """Print the census-coverage face of ``text`` and return its unmatched body-line count."""
    body = [line for line in text.splitlines() if line.startswith("(")]
    matched = list(C86_WAREHOUSE_TABLE.finditer(text))
    listed = rows_of(text)
    unmatched = len(body) - len(matched)
    declared_tables = int(first_capture(text, r"^tables (\d+) "))
    declared_rows = int(first_capture(text, r"estimated rows (\d+)"))
    print(f"[{label}] carrier={C86_WAREHOUSE_REL}")
    print(
        f"[{label}] body_tuple_lines={len(body)} census_matched={len(matched)} "
        f"unmatched={unmatched}"
    )
    for line in body:
        if C86_WAREHOUSE_TABLE.search(line) is None:
            print(f"[{label}]   UNMATCHED {line}")
    print(
        f"[{label}] declared_tables={declared_tables} matched_names={len(matched)} "
        f"distinct_names={len(listed)} table_delta={declared_tables - len(listed)}"
    )
    print(
        f"[{label}] declared_rows={declared_rows} listed_rows={sum(listed.values())} "
        f"rows_delta={declared_rows - sum(listed.values())}"
    )
    return unmatched


def main() -> int:
    """Report the carrier face, or run the two-sided control on the unmatched arm."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    text = ROSTER.read_text(encoding="utf-8")
    if not args.self_check:
        report(text, "live")
        return 0
    clean = report(text, "clean")
    tampered = report(text + TAMPERED_LINE + "\n", "tampered")
    ok = clean == 0 and tampered == 1
    print(f"CONTROL unmatched clean={clean} tampered={tampered} -> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
