"""Build a private, unassigned review worklist from a verified AFI204 plan."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from opendata.migration_plan import (  # noqa: E402  # direct script entrypoint
    MigrationPlanError,
    load_json_input,
)
from opendata.migration_worklist import (  # noqa: E402  # direct script entrypoint
    MigrationWorklistError,
    build_migration_worklist,
    write_migration_worklist_new_file,
)


def _parser() -> argparse.ArgumentParser:
    """Create a CLI that requires explicit source snapshots and output path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--runtime-inventory", type=Path, required=True)
    parser.add_argument("--static-catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Verify the plan against source snapshots and create a private new file."""
    args = _parser().parse_args(argv)
    try:
        plan, _plan_hash = load_json_input(args.plan, label="migration_plan")
        runtime, _runtime_hash = load_json_input(args.runtime_inventory, label="runtime_inventory")
        catalog, _catalog_hash = load_json_input(args.static_catalog, label="static_catalog")
        worklist = build_migration_worklist(plan, runtime, catalog)
        summary = worklist.get("summary")
        if not isinstance(summary, Mapping):
            raise MigrationWorklistError("worklist_summary_invalid")
        protected_roots: list[Path] = [REPOSITORY_ROOT]
        protected_roots.extend(
            Path(raw_root)
            for raw_root in (runtime.get("source_root"), catalog.get("opendata_root"))
            if isinstance(raw_root, str)
        )
        write_migration_worklist_new_file(
            args.output,
            worklist,
            input_paths=[args.plan, args.runtime_inventory, args.static_catalog],
            protected_roots=protected_roots,
        )
    except (MigrationPlanError, MigrationWorklistError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except OSError:
        print(json.dumps({"error": "input_or_output_io_error"}), file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "output_created": True,
                "queue_status": worklist["queue_status"],
                "summary": summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
