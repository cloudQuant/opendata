#!/usr/bin/env python3
"""C75 audit #3: where each census row's upstream facts can actually be re-read.

Why this face exists
    A census row asserts three kinds of thing -- an endpoint, a response format, and a set of
    column names. Each is only a fact if some carrier can be re-read and disagrees or agrees.
    Two carriers exist in this environment: the repository tree, and the *installed* OpenBB
    provider packages under an absolute site-packages root. A row whose cited package is in
    neither place carries an assertion, not a fact, and no test in this repo can refute it.

    That is not a hypothesis. The fed H.15 rows were found this round to cite a capture root that
    does not exist here (docs/evidence/C75/README.md §2.3), and this script generalizes the same
    reading over all 150 rows: it buckets every row by whether its cited ``openbb_*`` package is
    installed, and reports the rows that cite only in-repo files.

Usage
    python3 docs/evidence/C75/census-carrier-census.py

    Read-only: prints faces and exits 1 only when the row denominator moves. The installed-package
    list is taken from the absolute root printed in the header, so the reading states its own
    yardstick instead of asking the reader to trust a path.
"""

from __future__ import annotations

import json
import re
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).resolve().parents[3]
PLAN_DIR: Final = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
EXPECTED_CENSUS_ROWS: Final = 150
#: The yardstick this reading is taken against, spelled out rather than inferred from sys.path.
#: Recorded from the machine the C75 census was judged on; ``sysconfig`` is the fallback, and the
#: resolved root plus its provenance are printed in the header so the reading states its own scale.
SITE_PACKAGES: Final = Path("/Users/yunjinqi/opt/anaconda3/lib/python3.11/site-packages")
#: ``providers/<pkg>/openbb_<pkg>/...`` -- the provider package name is the second segment.
ORACLE_CITE = re.compile(r"providers/[a-z_0-9]+/(openbb_[a-z_0-9]+)")
REPO_CITE = re.compile(r"opendata/data/providers/")
#: Rows whose judgment must be found by this script, so a broken regex cannot print a false zero.
CONTROLS: Final = {
    "OBB2-oecd-CompositeLeadingIndicator": "oracle_installed",
    "OBB2-sec-CashFlowStatement": "repo_only",
    "OBB2-cboe-IndexSearch": "oracle_missing",
}


def strings(value: object) -> Iterator[str]:
    """Yield every string inside a nested JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def census_rows() -> list[dict[str, object]]:
    """Every census row, excluding the audit artifact that carries no rows."""
    rows: list[dict[str, object]] = []
    for path in sorted((REPO_ROOT / PLAN_DIR).glob("census-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            rows.extend(data["rows"])
    return rows


def resolve_yardstick() -> tuple[Path, str, set[str]]:
    """The site-packages root actually used, how it was found, and the ``openbb_*`` dirs under it.

    An empty root is NOT read as "no oracle is installed": every row would then land in
    ``oracle_missing`` and the buckets would print a confident, entirely fabricated distribution.
    ``main`` aborts on that instead of reporting it.
    """
    for root, provenance in (
        (SITE_PACKAGES, "recorded"),
        (Path(sysconfig.get_paths()["purelib"]), "sysconfig-fallback"),
    ):
        if root.is_dir():
            found = {
                path.name
                for path in root.glob("openbb_*")
                if path.is_dir() and not path.name.endswith("dist-info")
            }
            return root, provenance, found
    return SITE_PACKAGES, "absent", set()


def classify(row: dict[str, object], installed: set[str]) -> tuple[str, set[str]]:
    """Which carrier face this row's evidence cites, and the oracle names it names."""
    cites = list(strings(row.get("evidence")))
    oracles = {match.group(1) for cite in cites if (match := ORACLE_CITE.search(cite))}
    if oracles:
        if all(name in installed for name in oracles):
            return "oracle_installed", oracles
        return "oracle_missing", oracles
    if any(REPO_CITE.search(cite) for cite in cites):
        return "repo_only", oracles
    return "no_cite", oracles


def main(argv: list[str]) -> int:
    """Print the buckets, the per-provider table, and the controls guarding against a false zero."""
    root, provenance, installed = resolve_yardstick()
    rows = census_rows()
    print(f"site-packages root: {root} (found via {provenance}, exists={root.is_dir()})")
    print(f"installed openbb_* packages: {len(installed)}")
    if not installed:
        print("ABORT: the yardstick carries no openbb_* package, so every row would be bucketed")
        print("       oracle_missing. A NO_READING about the environment, not a census face.")
        return 1
    if len(rows) != EXPECTED_CENSUS_ROWS:
        print(f"ABORT: census rows = {len(rows)}, expected {EXPECTED_CENSUS_ROWS}")
        return 1

    buckets: dict[str, int] = {
        "oracle_installed": 0,
        "oracle_missing": 0,
        "repo_only": 0,
        "no_cite": 0,
    }
    per_provider: dict[str, dict[str, int]] = {}
    cited_missing: set[str] = set()
    cited_installed: set[str] = set()
    row_faces: dict[str, str] = {}
    for row in rows:
        face, oracles = classify(row, installed)
        buckets[face] += 1
        row_faces[str(row.get("task_id"))] = face
        record = per_provider.setdefault(
            str(row.get("provider")), {"rows": 0, **dict.fromkeys(buckets, 0)}
        )
        record["rows"] += 1
        record[face] += 1
        if face == "oracle_missing":
            cited_missing |= {name for name in oracles if name not in installed}
        elif face == "oracle_installed":
            cited_installed |= oracles

    print(f"\nbuckets over {len(rows)} rows (must sum to the denominator):")
    for name in ("oracle_installed", "oracle_missing", "repo_only", "no_cite"):
        print(f"  {name:<17} {buckets[name]:4d}")
    total = sum(buckets.values())
    print(f"  {'sum':<17} {total:4d}  {'ok' if total == len(rows) else 'MISMATCH'}")
    print(f"\ncited and installed : {sorted(cited_installed)}")
    print(f"cited and MISSING   : {sorted(cited_missing)}")

    print("\nper provider (rows / installed / missing / repo-only / no-cite):")
    for provider in sorted(per_provider):
        counts = per_provider[provider]
        print(
            f"  {provider:<17} {counts['rows']:3d} {counts['oracle_installed']:3d} "
            f"{counts['oracle_missing']:3d} {counts['repo_only']:3d} {counts['no_cite']:3d}"
        )

    failed = [
        f"{task_id}: expected {want}, read {row_faces.get(task_id, '(absent)')}"
        for task_id, want in CONTROLS.items()
        if row_faces.get(task_id) != want
    ]
    print("\ncontrols (a bucket face is only trustworthy if a known row lands where it was read):")
    for task_id, want in CONTROLS.items():
        print(f"  {task_id:<40} wants {want:<17} reads {row_faces.get(task_id, '(absent)')}")
    if failed:
        print("CONTROL FAIL -- the classifier is wrong, so every bucket above is unproven:")
        for line in failed:
            print(f"  {line}")
        return 1
    print(f"CONTROLS OK: {len(CONTROLS)}/{len(CONTROLS)} named rows landed in their read bucket")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
