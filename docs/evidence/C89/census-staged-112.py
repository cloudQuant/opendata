"""Offline witness for the staged census file docs/evidence/C89/census-staged-112.json.

Read-only: nothing here writes a file, opens a socket, touches a database or runs the engine.
Every number printed is measured this run from disk -- the staged JSON, the shipped census files,
the upstream OpenBB provider tree (read-only clean-room comparator) and the instrument's own
:meth:`model_capability_census.run` face.

Run:  python docs/evidence/C89/census-staged-112.py
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "scripts", "quality"))

import model_capability_census as mcc  # noqa: E402  (path set above)

STAGED = os.path.join(HERE, "census-staged-112.json")
CENSUS_GLOB = os.path.join(REPO, mcc.CENSUS_DIR_REL, mcc.CENSUS_GLOB)
UPSTREAM = "/Users/yunjinqi/Documents/new_projects/openbb/openbb_platform/providers"
CITE_RE = re.compile(r"[\w./-]+\.py:\d+")


def camel(snake: str) -> str:
    """CamelCase an upstream module stem, the spelling the census uses as ``upstream_model``."""
    return "".join(word[:1].upper() + word[1:] for word in snake.split("_"))


def shipped_ids_by_provider() -> dict[str, set[str]]:
    """upstream_model ids already carried by the merged census files (audit file excluded)."""
    out: dict[str, set[str]] = defaultdict(set)
    for path in sorted(glob.glob(CENSUS_GLOB)):
        if os.path.basename(path) == mcc.CENSUS_AUDIT_NAME:
            continue
        with open(path, encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        for row in payload.get("rows", []):
            out[str(row.get("provider", ""))].add(str(row.get("upstream_model", "")))
    return dict(out)


def upstream_ids(provider: str) -> list[str]:
    """Model ids the clean-tree comparator enumerates for one provider (models/*.py stems)."""
    models_dir = os.path.join(UPSTREAM, provider, f"openbb_{provider}", "models")
    if not os.path.isdir(models_dir):
        return []
    stems = sorted(
        os.path.basename(f)[:-3]
        for f in glob.glob(os.path.join(models_dir, "*.py"))
        if os.path.basename(f) != "__init__.py"
    )
    return [camel(s) for s in stems]


def cite_count(rows: list[dict[str, object]]) -> int:
    """Rows whose evidence+notes carry no ``<file>.py:<line>`` citation at all."""
    return sum(
        1 for row in rows if not CITE_RE.search(f"{row.get('evidence', '')} {row.get('notes', '')}")
    )


def print_ids(label: str, ids: list[str]) -> None:
    """Print an identifier list one per line under its count, never as a Python repr.

    A repr puts two long identifiers on one line, and ``generic-api-key`` reads the ``key``
    inside one of them as a key name with the next entry as its value: three census row ids
    printed this way in this witness's own archive became a credential finding at gate member
    3 (the rule captured the id naming the FMP risk-premium model, entropy 3.7841837). One
    identifier per line cannot be read as a pair, so the face keeps every id verbatim without
    offering the scanner a shape to match. That finding's triage is
    ``docs/evidence/C89/member3-secret-triage.py``; the flagged print is deliberately not
    quoted even in a comment, because committing it again re-opens the finding.
    """
    print(f"{label}: {len(ids)}")
    for one in ids:
        print(f"    - {one}")


def main() -> int:
    """Print the staged file's merge-contract, denominator and verdict faces."""
    with open(STAGED, encoding="utf-8") as handle:
        staged = json.load(handle)
    if not isinstance(staged, list):
        print(f"FAIL: {STAGED} is not a JSON array of rows", file=sys.stderr)
        return 1

    label_map = mcc.LABEL_TO_CAPABILITY
    report = mcc.run(round_id="C89-staged")
    present = set(report.statuses["present"])

    print("staged census witness -- docs/evidence/C89/census-staged-112.json")
    print(f"staged file rows: {len(staged)}")
    print(
        f"merged census population this run reads: {report.row_count} rows "
        f"from {len(report.census_files)} files "
        f"(EXPECTED_CENSUS_ROWS={mcc.EXPECTED_CENSUS_ROWS})"
    )

    task_ids = [str(row["task_id"]) for row in staged]
    dupes = sorted(t for t, n in Counter(task_ids).items() if n > 1)
    print_ids("staged task_id duplicates", dupes)

    shapes: Counter[str] = Counter()
    merged_task_ids: set[str] = set()
    for path in sorted(glob.glob(CENSUS_GLOB)):
        if os.path.basename(path) == mcc.CENSUS_AUDIT_NAME:
            continue
        with open(path, encoding="utf-8") as handle:
            payload = json.loads(handle.read())
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            shapes["dict-with-rows"] += 1
            merged_task_ids.update(
                str(row.get("task_id")) for row in payload["rows"] if isinstance(row, dict)
            )
        elif isinstance(payload, list):
            shapes["bare-list"] += 1
        else:
            shapes[type(payload).__name__] += 1
    collisions = sorted(set(task_ids) & merged_task_ids)
    ok_ids = sum(1 for row in staged if isinstance(row.get("task_id"), str) and row["task_id"])
    all_fold = sum(
        1
        for row in staged
        if all(label in label_map for label in row.get("needs_engine_capability", []))
    )
    print("\nmerge contract (measured from disk; this witness writes nothing)")
    print(f"  staged top level: {type(staged).__name__} -- a bare JSON array, exactly as staged")
    print(f"  shipped census file top-level shapes: {dict(shapes)}")
    print(
        f"  mcc.load_rows fails a bare array closed (it requires a payload object with a rows "
        f"list), so the merge round must wrap these {len(staged)} rows or splice them into an "
        f"existing file's rows list"
    )
    print(f"  staged rows with a non-empty task_id string: {ok_ids}/{len(staged)}")
    print(
        f"  staged rows whose labels all fold under LABEL_TO_CAPABILITY: {all_fold}/{len(staged)}"
    )
    print_ids("  staged task_ids already used by a merged census row", collisions)

    print("\nprovider -> staged rows")
    by_provider = Counter(str(row.get("provider", "")) for row in staged)
    for provider, total in sorted(by_provider.items()):
        print(f"  {provider:16s} {total}")

    print("\nraw labels -> canonical fold")
    raw = {str(label) for row in staged for label in row.get("needs_engine_capability", [])}
    unfolded_labels = sorted(label for label in raw if label not in label_map)
    for label in sorted(raw):
        print(f"  {label:34s} -> {label_map.get(label, 'NO FOLD')}")
    print(f"distinct raw labels: {len(raw)}")
    print_ids("  NO FOLD labels", unfolded_labels)
    rows_with_unfolded = sum(
        1
        for row in staged
        if any(label not in label_map for label in row.get("needs_engine_capability", []))
    )
    print(f"rows carrying at least one NO FOLD label: {rows_with_unfolded}")

    def folded(row: dict[str, object]) -> set[str]:
        labels = row.get("needs_engine_capability", [])
        if not isinstance(labels, list):
            return set()
        return {label_map[label] for label in labels if label in label_map}

    ready = [
        row
        for row in staged
        if row.get("expressible_today") is not True
        and folded(row)
        and folded(row) <= present
        and not any(label not in label_map for label in row.get("needs_engine_capability", []))
    ]
    print(f"\npresent canonicals this run: {len(present)}")
    print(
        f"staged rows whose every need is already present (join shipped cover on merge): "
        f"{len(ready)}"
    )
    for row in ready:
        print(f"  - {row['task_id']} needs {', '.join(sorted(folded(row)))}")
    empty_needs = [str(row["task_id"]) for row in staged if not row.get("needs_engine_capability")]
    print_ids("staged rows with an empty need set", empty_needs)

    missing_cites = cite_count(staged)
    print(f"\nstaged rows citing no upstream file:line: {missing_cites}")
    stripped = [dict(row) for row in staged]
    stripped[0]["evidence"] = "no cite here"
    stripped[0]["notes"] = "control arm: the same row with the citation taken away"
    control = cite_count(stripped)
    print(
        f"control arm (one fixture row with its citation stripped): {control} "
        f"-- counter can go non-zero: {control == 1}"
    )

    print("\ndenominator -- upstream model ids vs staged rows (per provider package in this repo)")
    shipped = shipped_ids_by_provider()
    total_upstream = 0
    total_short = 0
    for provider in sorted(by_provider):
        ids = upstream_ids(provider)
        already = shipped.get(provider, set())
        staged_ids = {
            str(row.get("upstream_model", ""))
            for row in staged
            if str(row.get("provider", "")) == provider
        }
        covered = already | staged_ids
        short = [name for name in ids if name not in covered]
        aliased = sorted(staged_ids - set(ids))
        total_upstream += len(ids)
        total_short += len(short)
        print(
            f"  {provider:16s} upstream={len(ids):3d} already_censused={len(already):3d} "
            f"staged={len(staged_ids):3d} still_short={len(short):3d}"
        )
        print_ids(f"  {provider:16s} still_short ids", short)
        print_ids(
            f"  {provider:16s} staged ids naming no upstream module (aliased, measured here)",
            aliased,
        )
    print(f"  TOTAL upstream ids scanned={total_upstream} still_short={total_short}")
    print(
        "  NOTE: an upstream id here is the CamelCase of one openbb_<p>/models/<stem>.py module. "
        "Where OpenBB's model id differs from its module stem (the aliased lists above), this "
        "denominator can only pair them by name, so still_short over-reports by the alias count "
        "and a provider printed as covered is not proof that every upstream model has a row. "
        "still_short is a denominator face meant to stop a short stage reading as "
        "complete, not a claim of completeness."
    )

    print(
        "\ndenominator, every upstream provider package (merged rows + staged rows vs upstream ids)"
    )
    staged_by_provider: dict[str, set[str]] = defaultdict(set)
    for row in staged:
        staged_by_provider[str(row.get("provider", ""))].add(str(row.get("upstream_model", "")))
    packages = [
        name
        for name in sorted(os.listdir(UPSTREAM))
        if name not in {"__init__.py", "README.md", "tests"} and upstream_ids(name)
    ]
    uncovered_total = 0
    for provider in packages:
        ids = upstream_ids(provider)
        already = shipped.get(provider, set())
        st = staged_by_provider.get(provider, set())
        missing = [name for name in ids if name not in (already | st)]
        uncovered_total += len(missing)
        print(
            f"  {provider:16s} upstream={len(ids):3d} merged_ids={len(already):3d} "
            f"staged_ids={len(st):3d} uncovered={len(missing):3d}"
            f"{'   <-- this stage adds rows here' if st else ''}"
        )
        if missing:
            print_ids(f"  {'':16s} uncovered", missing)
    print(
        f"  packages scanned={len(packages)} total uncovered (name-paired, over-reports by "
        f"aliasing)= {uncovered_total}"
    )
    orphan_merged = sum(
        1
        for provider in packages
        for name in shipped.get(provider, set()) - set(upstream_ids(provider))
    )
    print(
        f"  name-pairing loss, measured: merged ids in these packages that match no module stem "
        f"= {orphan_merged} (the census spells some models by their registered id, not the stem)"
    )
    no_package = sorted(
        name
        for name in os.listdir(os.path.join(REPO, mcc.PROVIDER_ROOT_REL))
        if os.path.isdir(os.path.join(REPO, mcc.PROVIDER_ROOT_REL, name))
        and not name.startswith("_")
        and not upstream_ids(name)
    )
    print_ids("  repo provider dirs with no upstream package to census against", no_package)
    repo_dirs = sorted(
        name
        for name in os.listdir(os.path.join(REPO, mcc.PROVIDER_ROOT_REL))
        if os.path.isdir(os.path.join(REPO, mcc.PROVIDER_ROOT_REL, name))
        and not name.startswith("_")
    )
    no_rows = [p for p in repo_dirs if not shipped.get(p) and not staged_by_provider.get(p)]
    print_ids("  repo providers with NO census rows anywhere (merged or staged)", no_rows)
    never_rowed = [p for p in packages if not shipped.get(p) and not staged_by_provider.get(p)]
    print_ids("  upstream packages with zero census rows anywhere (merged or staged)", never_rowed)

    print("\nstaged rows by fold-target status")
    status_of = {
        cap: "present" if cap in present else "absent-or-other" for cap in set(label_map.values())
    }
    tally = Counter(status_of[cap] for row in staged for cap in folded(row))
    for key in sorted(tally):
        print(f"  {key:16s} {tally[key]}")
    blocking = Counter(
        cap for row in staged for cap in folded(row) if status_of.get(cap) != "present"
    )
    print(f"  distinct blocking canonicals named by staged rows: {len(blocking)}")
    for cap, total in blocking.most_common():
        print(f"    - {cap}: {total}")

    ok = missing_cites == 0 and control == 1 and not dupes and not collisions
    print(
        f"\nverdict: {'PASS' if ok else 'FAIL'} "
        f"(cites={missing_cites}, control={control}, duplicate_task_ids={len(dupes)}, "
        f"task_id_collisions={len(collisions)})"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
