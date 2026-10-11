#!/usr/bin/env python3
"""Split the vocabulary-ready census rows against each package's cite-backed refusals.

The capability census says 14 of 238 rows need only capabilities the engine already ships.
Are those rows implementable today, or does the package that owns the model publish a refusal
the census does not see?

Classes, mutually exclusive, and the script asserts the partition covers every input row:

  SHIPPED      the provider package declares this model in its ``specs.py`` (so the row is done)
  BLOCKED      the package's ``NOT_DECLARABLE`` names this model (a refusal is on file)
  NO_PACKAGE   no ``opendata/data/providers/<provider>/`` package exists at all
  UNREFUSED    a package exists, neither declares nor refuses the row (implement now)

Both ``ast.Assign`` and ``ast.AnnAssign`` are scanned: ``NOT_DECLARABLE: dict[str, str] = {`` is
an annotated assignment, and a probe that only walks ``Assign`` reports an empty refusal table
and prints a fake ``UNREFUSED`` population. No request is made and no engine code is executed.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parents[3]
CENSUS_DIR = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
ROADMAP = CENSUS_DIR / "capability-roadmap.json"
PROVIDERS = REPO / "opendata/data/providers"


def _load_census() -> ModuleType:
    """Import the census instrument the way a dataclass inside it requires."""
    spec = importlib.util.spec_from_file_location(
        "censusmod", REPO / "scripts/quality/model_capability_census.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("the census instrument is not loadable, so this face measures nothing")
    module = importlib.util.module_from_spec(spec)
    # Without this registration ``@dataclass(frozen=True)`` inside the module cannot resolve
    # its own ``__module__`` and the import dies in ``dataclasses._is_type``.
    sys.modules["censusmod"] = module
    spec.loader.exec_module(module)
    return module


def top_level_dicts(path: Path) -> dict[str, dict[str, str]]:
    """Every module-level string dict literal in ``path``, keyed by variable name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            if isinstance(node.value, ast.Dict):
                pairs: dict[str, str] = {}
                for key, value in zip(node.value.keys, node.value.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(value, ast.Constant)
                        and isinstance(key.value, str)
                        and isinstance(value.value, str)
                    ):
                        pairs[key.value] = value.value
                if pairs:
                    out[name] = pairs
    return out


def main() -> int:
    """Print the four-class partition of every row whose needs are all shipped."""
    census = _load_census()
    roadmap = json.loads(ROADMAP.read_text(encoding="utf-8"))
    registry = roadmap["capability_registry"]
    present = {cap for cap, entry in registry.items() if entry.get("present")}
    rows = census.load_rows(CENSUS_DIR)
    ready = [row for row in rows if row.needs and set(row.needs) <= present]

    classes: dict[str, list[str]] = {
        "SHIPPED": [],
        "BLOCKED": [],
        "NO_PACKAGE": [],
        "UNREFUSED": [],
    }
    lines: list[str] = []
    for row in sorted(ready, key=lambda r: (r.provider, r.upstream_model)):
        package = PROVIDERS / row.provider
        specs_file = package / "specs.py"
        declared = False
        refused_by = ""
        if package.is_dir() and specs_file.is_file():
            declared = row.upstream_model in specs_file.read_text(encoding="utf-8")
            for table in top_level_dicts(specs_file).values():
                if row.upstream_model in table:
                    refused_by = table[row.upstream_model]
        if not package.is_dir():
            kind = "NO_PACKAGE"
        elif refused_by:
            kind = "BLOCKED"
        elif declared:
            kind = "SHIPPED"
        else:
            kind = "UNREFUSED"
        classes[kind].append(row.task_id)
        lines.append(f"{kind:11s} {row.task_id:34s} needs={sorted(row.needs)}")
        if refused_by:
            lines.append(f"{'':11s} refusal: {refused_by}")

    total = sum(len(v) for v in classes.values())
    print(f"roadmap generation        : {roadmap['round_id']}")
    print(f"census rows               : {len(rows)}")
    print(f"present capabilities      : {len(present)} -> {sorted(present)}")
    print(f"rows needing only present : {len(ready)}")
    print(f"partition check           : {len(ready)} == {total} -> {len(ready) == total}")
    print()
    for line in lines:
        print(line)
    print()
    for kind, ids in classes.items():
        print(f"{kind:11s} {len(ids):3d}  {sorted(ids)}")
    if total != len(ready):
        print("PARTITION BROKEN -- a row was counted twice or dropped")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
