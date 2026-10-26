"""Create an offline, disabled-only migration plan from explicit JSON snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from opendata.migration_plan import (  # noqa: E402  # direct script entrypoint
    MigrationPlanError,
    WarehouseFingerprint,
    build_migration_plan,
    load_json_input,
    render_plan,
    write_plan_new_file,
)


def _parser() -> argparse.ArgumentParser:
    """Create the explicit-input command line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-inventory", type=Path, required=True)
    parser.add_argument("--static-catalog", type=Path, required=True)
    parser.add_argument(
        "--reviewed-mapping",
        type=Path,
        help="Optional version-1 mapping file pinned to canonical hashes of all inputs.",
    )
    parser.add_argument(
        "--target-allowlist",
        type=Path,
        help="Optional version-1 target dataset and warehouse fingerprint allowlist.",
    )
    parser.add_argument(
        "--target-warehouse-fingerprint",
        help="Explicit lowercase SHA-256 fingerprint of the inspected target warehouse schema.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read pinned inputs, write a new report outside source trees, and print counts."""
    args = _parser().parse_args(argv)
    try:
        runtime, _runtime_hash = load_json_input(args.runtime_inventory, label="runtime_inventory")
        catalog, _catalog_hash = load_json_input(args.static_catalog, label="static_catalog")
        mapping = None
        allowlist = None
        input_paths = [args.runtime_inventory, args.static_catalog]
        if args.reviewed_mapping is not None:
            mapping, _mapping_hash = load_json_input(
                args.reviewed_mapping, label="reviewed_mapping"
            )
            input_paths.append(args.reviewed_mapping)
        if args.target_allowlist is not None:
            allowlist, _allowlist_hash = load_json_input(
                args.target_allowlist, label="target_allowlist"
            )
            input_paths.append(args.target_allowlist)
        fingerprint = (
            WarehouseFingerprint(args.target_warehouse_fingerprint)
            if args.target_warehouse_fingerprint is not None
            else None
        )
        plan = build_migration_plan(
            runtime,
            catalog,
            mapping,
            warehouse_fingerprint=fingerprint,
            target_allowlist=allowlist,
        )
        protected_roots: list[Path] = [REPOSITORY_ROOT]
        protected_roots.extend(
            Path(raw_root)
            for raw_root in (runtime.get("source_root"), catalog.get("opendata_root"))
            if isinstance(raw_root, str)
        )
        output_path = write_plan_new_file(
            args.output,
            render_plan(plan),
            input_paths=input_paths,
            protected_roots=protected_roots,
        )
    except MigrationPlanError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"error": "input_or_output_io_error"}), file=sys.stderr)
        return 2

    summary = plan["summary"]
    print(
        json.dumps(
            {
                "output": output_path.as_posix(),
                "plan_sha256": plan["plan_sha256"],
                "plan_status": plan["plan_status"],
                "summary": summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
