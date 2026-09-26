"""Recompute the AC-17 item-6 coverage faces from a gate log's own table.

A file's percentage is a combined unit ratio: ``(stmts - miss + branches - partial)
/ (stmts + branches)``. Dividing missed statements by statements alone gives a
different, flattering number, so the face is summed from raw columns here.
"""

from __future__ import annotations

import re
import sys
from typing import NamedTuple

ROW = re.compile(r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+")

FACES = ("opendata/data/", "opendata/pipeline/", "opendata_fuyao/")


class Row(NamedTuple):
    """One line of coverage.py's term report, as printed."""

    filename: str
    stmts: int
    miss: int
    branches: int
    partial: int


def read_rows(path: str) -> list[Row]:
    """Collect distinct report rows, keeping the first reading of each file."""
    seen: dict[str, Row] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = ROW.match(line.rstrip())
            if match is None:
                continue
            row = Row(match.group(1), *(int(group) for group in match.groups()[1:]))
            seen.setdefault(row.filename, row)
    return list(seen.values())


def summarize(rows: list[Row], prefix: str) -> str:
    """Format one face: file count, unit total, covered units, percentage."""
    picked = [row for row in rows if row.filename.startswith(prefix)]
    units = sum(row.stmts + row.branches for row in picked)
    bad = sum(row.miss + row.partial for row in picked)
    if units == 0:
        return f"{prefix:<20} files=0      units=0      covered=0      face=n/a"
    return (
        f"{prefix:<20} files={len(picked):<4} units={units:<7} "
        f"covered={units - bad:<7} face={(units - bad) / units * 100:.2f}%"
    )


def main(paths: list[str]) -> int:
    """Print each archived log's faces so two gate runs can be compared line by line."""
    for path in paths:
        rows = read_rows(path)
        print(f"== {path}  (rows={len(rows)})")
        for prefix in FACES:
            print(f"  {summarize(rows, prefix)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
