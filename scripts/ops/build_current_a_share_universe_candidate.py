"""Build an offline current A-share universe candidate from captured evidence.

This strict command reads one manifest, verifies every referenced file's
SHA-256 and size, parses only supported captured catalog layouts, and writes a
deterministic JSON candidate snapshot. It performs no network access, reads no
``.env`` file, writes no database, and publishes no API resource. The output is
a review candidate, not an authoritative universe or a delisted lifecycle.

Exit codes: 0 on success, 2 on any validation or evidence failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendata.pipeline.current_a_share_universe_candidate import (
    UniverseCandidateError,
    build_universe_candidate,
    load_manifest,
    render_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    """Create the strict command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic, evidence-bound current A-share universe candidate "
            "without network, database, or API publication."
        )
    )
    parser.add_argument("--manifest", required=True, help="Absolute path to the evidence manifest")
    parser.add_argument("--output", required=True, help="Absolute path for the candidate JSON")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing regular output file (symlinks are always rejected)",
    )
    return parser


def _write_output(path_text: str, text: str, *, force: bool) -> None:
    """Write output atomically after rejecting symlinks and unsafe paths."""
    if not path_text or "\x00" in path_text:
        raise UniverseCandidateError("output_path_invalid")
    path = Path(path_text)
    if not path.is_absolute():
        raise UniverseCandidateError("output_path_not_absolute")
    absolute = os.path.abspath(path_text)
    if absolute != os.path.realpath(path_text):
        raise UniverseCandidateError("output_path_symlink_rejected")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise UniverseCandidateError("output_parent_invalid")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise UniverseCandidateError("output_path_symlink_rejected")
        if not force:
            raise UniverseCandidateError("output_exists")
    data = text.encode("utf-8")
    temporary = parent / f".{path.name}.tmp-{os.getpid()}"
    if temporary.exists() or temporary.is_symlink():
        raise UniverseCandidateError("output_temporary_exists")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        with suppress(OSError):
            temporary.unlink()
        raise UniverseCandidateError("output_write_failed") from None


def main(argv: list[str] | None = None) -> int:
    """Run the candidate build and report a compact result.

    Args:
        argv: Optional argument vector for tests; defaults to process arguments.

    Returns:
        Process exit code: 0 on success, 2 on a validation or evidence failure.
    """
    arguments = _parser().parse_args(argv)
    try:
        manifest, manifest_sha256 = load_manifest(arguments.manifest)
        snapshot = build_universe_candidate(manifest, manifest_sha256=manifest_sha256)
        text = render_snapshot(snapshot)
        _write_output(arguments.output, text, force=arguments.force)
    except UniverseCandidateError as error:
        print(f"REFUSED {error.code}", file=sys.stderr)
        return 2
    summary = {
        "status": "CANDIDATE_WRITTEN",
        "output": os.path.abspath(arguments.output),
        "manifest_sha256": manifest_sha256,
        "records_total": snapshot.counts["records_total"],
        "candidates_total": snapshot.counts["candidates_total"],
        "conflicts_total": snapshot.counts["conflicts_total"],
        "pagination_status": snapshot.pagination["status"],
        "bse_supported": snapshot.bse_supported,
        "universe_census_attested": False,
        "delisted_lifecycle_included": False,
        "publication": "NOT_PERFORMED",
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
