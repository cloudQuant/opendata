"""C75 #37: split the ambiguous ``providers/federal_reserve/`` substring into its real roots.

A count of "strings mentioning ``providers/federal_reserve/``" is worth nothing on its own, because
the same substring lands on three different things: a cite into the *installed* OpenBB extension
(which this repo cannot resolve), a cite into this repo's own ``opendata/data/...`` package, and
prose that quotes the unresolvable shape while talking about it. Splitting the bucket is what makes
the before/after movement readable as a fact instead of a number.

Usage:
    python3 docs/evidence/C75/census-cite-buckets.py            # current census
    python3 docs/evidence/C75/census-cite-buckets.py A.json B.json  # two readings, with deltas
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
PLAN_DIR = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
CENSUS = REPO / PLAN_DIR / "census-sec-tmx-fed-gov-finra.json"
ROOT = "providers/federal_reserve/"
INSTALL = f"{ROOT}openbb_federal_reserve/"
REPO_SHAPED = f"opendata/data/{ROOT}"
NEEDS = ("published_value_rescale", "client_side_sort", "client_side_column_drop")


def rows_of(path: Path) -> list[dict[str, Any]]:
    """Load a census file and return its rows, accepting both the bare-list and wrapped shapes."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: Any = data["rows"] if isinstance(data, dict) and "rows" in data else data
    if not isinstance(rows, list):
        raise AssertionError(f"{path}: census rows are a {type(rows).__name__}, not a list")
    return [row for row in rows if isinstance(row, dict)]


def strings(value: object) -> list[str]:
    """Every string leaf below ``value``, in walk order."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(strings(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(strings(child))
    return found


def buckets(rows: list[dict[str, Any]]) -> Counter[str]:
    """Count the three root shapes apart, plus the substring total they are read against."""
    counts: Counter[str] = Counter()
    for row in rows:
        for text in strings(row):
            if ROOT in text:
                counts["total"] += 1
                if INSTALL in text:
                    counts["install"] += 1
                elif REPO_SHAPED in text:
                    counts["repo"] += 1
                else:
                    counts["prose"] += 1
    return counts


def need_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    """Mentions per need label inside this census file, for the labels #35 moved."""
    counts: Counter[str] = Counter()
    for row in rows:
        for need in row.get("needs_engine_capability", []):
            counts[str(need)] += 1
    return counts


def report(path: Path) -> tuple[Counter[str], Counter[str], list[str], int]:
    """Read one census file into the faces the README quotes."""
    rows = rows_of(path)
    true_ids = sorted(str(row["task_id"]) for row in rows if row.get("expressible_today"))
    return buckets(rows), need_counts(rows), true_ids, len(rows)


def face(path: Path) -> list[str]:
    """Render one reading."""
    counts, needs, true_ids, row_count = report(path)
    lines = [
        f"{path}",
        f"   rows in this file = {row_count}, expressible_today true = {len(true_ids)}: {true_ids}",
        f"   substring `{ROOT}` = {counts['total']} string values, split apart: "
        f"install-root={counts['install']} repo-root={counts['repo']} prose={counts['prose']}",
        "   need mentions in this file: " + ", ".join(f"{label}={needs[label]}" for label in NEEDS),
    ]
    return lines


def main(argv: list[str]) -> int:
    """Print the reading, and the before/after delta when two files are given."""
    paths = [Path(arg) for arg in argv] or [CENSUS]
    for path in paths:
        for line in face(path):
            print(line)
    if len(paths) == 2:
        before_counts, before_needs, before_true, before_rows = report(paths[0])
        after_counts, after_needs, after_true, after_rows = report(paths[1])
        print("delta (after - before):")
        print(f"   rows {before_rows} -> {after_rows}")
        print(f"   true rows {len(before_true)} -> {len(after_true)}: {after_true}")
        for key in ("total", "install", "repo", "prose"):
            print(f"   {key}-root strings {before_counts[key]} -> {after_counts[key]}")
        for label in NEEDS:
            print(f"   {label} {before_needs[label]} -> {after_needs[label]}")
        if before_rows != after_rows:
            print("ABORT: the row denominator moved")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
