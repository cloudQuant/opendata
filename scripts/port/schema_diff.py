#!/usr/bin/env python3
"""Diff AFI's data-management models against OpenData's, table by table.

Stage 2 decision input: if OpenData's models already cover AFI's columns, the port binds to
them and no model is moved; the remaining differences become explicit column additions (or
documented drops), not an open-ended rewrite.

Read-only: parses the two model files with ``ast`` and writes JSON.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib

PAIRS = (
    ("ak_data_scripts", "data_scripts"),
    ("ak_data_tables", "data_tables"),
    ("ak_data_interfaces", "data_interfaces"),
    ("ak_interface_categories", "interface_categories"),
    ("ak_interface_parameters", "interface_parameters"),
    ("ak_scheduled_tasks", "scheduled_tasks"),
    ("ak_task_executions", "task_executions"),
)


def columns(path: pathlib.Path) -> dict[str, set[str]]:
    """Parse mapped columns per table from a model module."""
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        table = None
        cols: set[str] = set()
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and any(getattr(t, "id", "") == "__tablename__" for t in stmt.targets)
                and isinstance(stmt.value, ast.Constant)
            ):
                table = stmt.value.value
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and isinstance(stmt.value, ast.Call)
            ):
                fn = getattr(stmt.value.func, "id", None) or getattr(stmt.value.func, "attr", None)
                if fn in ("Column", "mapped_column"):
                    cols.add(stmt.target.id)
        if table:
            found[table] = cols
    return found


def main() -> int:
    """Print the per-table column delta as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--afi-model", required=True)
    parser.add_argument("--opendata-models", required=True)
    parser.add_argument("--out", default="-")
    args = parser.parse_args()

    afi = columns(pathlib.Path(args.afi_model))
    opendata: dict[str, set[str]] = {}
    for path in sorted(pathlib.Path(args.opendata_models).glob("*.py")):
        opendata.update(columns(path))

    tables = []
    for afi_table, od_table in PAIRS:
        a, b = afi.get(afi_table, set()), opendata.get(od_table, set())
        tables.append(
            {
                "afi_table": afi_table,
                "opendata_table": od_table,
                "afi_columns": len(a),
                "opendata_columns": len(b),
                "only_in_afi": sorted(a - b),
                "only_in_opendata": sorted(b - a),
                "verdict": "identical" if a == b else ("afi_subset" if a <= b else "afi_adds"),
            }
        )
    document = {
        "afi_model": args.afi_model,
        "opendata_models": args.opendata_models,
        "afi_only_columns": sum(len(t["only_in_afi"]) for t in tables),
        "opendata_only_columns": sum(len(t["only_in_opendata"]) for t in tables),
        "tables": tables,
    }
    text = json.dumps(document, indent=2, ensure_ascii=False)
    if args.out == "-":
        print(text)
    else:
        pathlib.Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(
            f"afi_only={document['afi_only_columns']} "
            f"opendata_only={document['opendata_only_columns']} -> {args.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
