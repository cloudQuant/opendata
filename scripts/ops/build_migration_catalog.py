"""Build the iteration-204 legacy-to-OpenData static crosswalk."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from opendata.migration_catalog import (
    DEFAULT_BASELINE_RELATIVE,
    DEFAULT_LEGACY_ROOT,
    DEFAULT_OPENDATA_ROOT,
    DEFAULT_OUTPUT_RELATIVE,
    build_migration_catalog,
    render_json,
    validate_output_path,
)


def _parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--opendata-root", type=Path, default=DEFAULT_OPENDATA_ROOT)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Build and atomically write the static catalog without touching inputs."""
    args = _parser().parse_args(argv)
    legacy_root = args.legacy_root.expanduser().resolve()
    opendata_root = args.opendata_root.expanduser().resolve()
    baseline = (
        args.baseline.expanduser().resolve()
        if args.baseline
        else legacy_root / DEFAULT_BASELINE_RELATIVE
    )
    output = args.output or opendata_root / DEFAULT_OUTPUT_RELATIVE
    catalog = build_migration_catalog(legacy_root, opendata_root, baseline_path=baseline)
    input_files = {
        baseline,
        opendata_root / "opendata/migration_catalog.py",
        opendata_root / "scripts/ops/build_migration_catalog.py",
        *(legacy_root / record.source_path for record in catalog.files),
        *(opendata_root / model.module_path for model in catalog.provider_models),
        *(legacy_root / record.source_path for record in catalog.auxiliary_files),
        *(
            path
            for path in (opendata_root / "opendata/data/providers").rglob("provider.py")
            if path.is_file()
        ),
        *(
            path
            for path in (opendata_root / "opendata/data/providers").rglob("_source.py")
            if path.is_file()
        ),
        *(
            path
            for path in (opendata_root / "opendata/data/providers").rglob("models/**/*.py")
            if path.is_file()
        ),
        opendata_root / "opendata/data/providers/akshare/_vendor/__init__.py",
    }
    resolved_output, output_error = validate_output_path(
        output,
        legacy_root=legacy_root,
        input_files=input_files,
        protected_roots={opendata_root / "opendata/data/providers"},
        overwrite=args.overwrite,
    )
    if output_error or resolved_output is None:
        print(json.dumps({"error": output_error}, ensure_ascii=False), file=sys.stderr)
        return 2
    payload = render_json(catalog)
    fd, temporary_name = tempfile.mkstemp(prefix=".afi204-catalog-", dir=resolved_output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if resolved_output.exists() and not args.overwrite:
            print(json.dumps({"error": "output_exists_requires_overwrite"}), file=sys.stderr)
            return 2
        os.replace(temporary_name, resolved_output)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(
        json.dumps(
            {
                "output": resolved_output.as_posix(),
                "summary": catalog.summary(),
                "errors": len(catalog.errors),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if catalog.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
