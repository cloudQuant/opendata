"""Regenerate a round's port-scope inventory from the live tree (design §5.6, FR-4/D9).

``scripts/quality/port_scope.py`` only validates a port-scope inventory, and the single archived
issue of one -- ``docs/evidence/C65/port-scope-manifest.json`` -- was written by an ad-hoc script
that no longer exists. Iteration 2 renamed the port root, so 166 of its 327 rows describe bytes
the current tree no longer has and ``AC-5|02`` reads gap. This is the generator that record never
had, and it splits every field by where its truth lives:

* re-measured here -- the pristine upstream hashes against ``upstream.lock``, the ported hashes
  against ``manifest.json`` and the bytes on disk, the line counts, and the deterministic codemod
  replay (``port_source`` for modules, ``vendor_init_facade`` for the regenerated root facade,
  verbatim bytes for resources) whose rewrite counts fill ``port_report``;
* carried forward verbatim by path -- ``reason``, ``batch``, ``scope_basis``,
  ``ac6_02_existing_fixture_inventory`` and the recorded-fixture fields, because those are
  decisions about the frozen scope and about offline recorded cases, not statements about
  current bytes. ``batch`` is re-derived through ``port_scope._expected_batches`` as a
  cross-check only, and the carried value is what gets written.

Fail-closed, with nothing written when any of it fires: an upstream checkout that is not the
locked commit, an upstream/manifest/disk/C65 path missing for a locked record, a pristine hash
that left the lock, a replayed ``.py`` that is not the file on disk, a carried batch the
derivation rejects, or counts that are not 327 total / 325 python / 2 resources.

CLI::

    python scripts/quality/port_scope_inventory.py -o docs/evidence/C74/port-scope-manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.codemod.port_module import (  # noqa: E402
    _INIT_UPSTREAM_PATH,
    DEFAULT_UPSTREAM_REPO,
    PORTED_ROOT,
    UNLOCKED_METADATA,
    UpstreamLock,
    port_source,
    sha256_bytes,
    sha256_text,
    vendor_init_facade,
    verify_upstream,
)
from scripts.quality import port_scope  # noqa: E402

ROUND = "C74"
AS_OF = "2026-10-09"
LOCK_REL = "upstream.lock"
MANIFEST_REL = "manifest.json"
CARRIED_FROM_REL = Path("docs/evidence/C65/port-scope-manifest.json")
CARRIED_ROW_KEYS = frozenset(
    {"batch", "reason", "existing_recorded_cases", "existing_recorded_public_functions"}
)
CARRIED_TOP_LEVEL_KEYS = frozenset({"scope_basis", "ac6_02_existing_fixture_inventory"})
REPLAY_MATCH = "PASS"
REPLAY_DIFFERS = "DIFFERS: replayed bytes are not the bytes on disk"
_PORTED_SUFFIX = ".py"
_ROOT_MODULE = "<root>"


class InventoryError(RuntimeError):
    """A live-tree, lock, or carried-forward precondition did not hold."""


@dataclass(frozen=True)
class Replay:
    """One deterministic replay of a ported file from the pinned upstream source."""

    upstream_sha: str
    replayed_sha: str
    import_rewrites: int
    string_rewrites: int
    manual_edits: bool
    #: Only the root facade carries this: how many upstream export statements the facade writer
    #: re-homed. It is a different quantity from ``import_rewrites`` and is never added to it.
    facade_export_count: int = 0


@dataclass(frozen=True)
class Row:
    """Reconciled facts for one locked path: re-measured bytes plus carried scope claims."""

    path: str
    kind: str
    module: str
    batch: list[Any]
    reason: str
    upstream_path: str
    manual_edits: bool
    lock_sha: str
    upstream_sha: str
    manifest_sha: str
    disk_sha: str
    manifest_section: str
    manifest_upstream_path_matches_lock: bool
    manifest_manual_edits_matches_lock: bool | None
    snapshot_lines: int | None
    current_lines: int | None
    import_rewrites: int
    string_rewrites: int
    report_manual_edits_matches_lock: bool
    replay_status: str
    existing_recorded_cases: list[Any]
    existing_recorded_public_functions: list[Any]
    facade_export_count: int = 0

    @property
    def is_root_facade(self) -> bool:
        """Whether this row is the vendored root's lazy facade rather than a ported module."""
        return self.upstream_path == _INIT_UPSTREAM_PATH

    @property
    def upstream_hash_current(self) -> bool:
        """Whether the checkout's pristine source still hashes to the locked value."""
        return self.upstream_sha == self.lock_sha

    def to_json(self) -> dict[str, object]:
        """Render the row in the archived key order.

        No per-row "path present" key: this generator aborts before writing anything when a locked
        path is absent from the manifest, the ported tree or the replay, so such a flag could only
        ever render as true. Presence and upstream-path agreement are stated once, at the face where
        they can be false: ``lock_manifest_path_set_equal`` and ``port_report_path_set_equal_lock``.
        """
        return {
            "path": self.path,
            "module": self.module,
            "batch": self.batch,
            "reason": self.reason,
            "kind": self.kind,
            "upstream_path": self.upstream_path,
            "manual_edits": self.manual_edits,
            "sha256": {
                "upstream_lock": self.lock_sha,
                "upstream_checkout_current": self.upstream_sha,
                "upstream_lock_matches_current": self.upstream_hash_current,
                "manifest_ported_snapshot": self.manifest_sha,
                "port_tree_current": self.disk_sha,
                "manifest_matches_current_port_tree": self.manifest_sha == self.disk_sha,
            },
            "manifest": {
                "section": self.manifest_section,
                "upstream_path_matches_lock": self.manifest_upstream_path_matches_lock,
                "manual_edits_matches_lock": self.manifest_manual_edits_matches_lock,
                "snapshot_lines": self.snapshot_lines,
                "current_lines": self.current_lines,
            },
            "port_report": {
                "import_rewrites": self.import_rewrites,
                "string_rewrites": self.string_rewrites,
                "manual_edits_matches_lock": self.report_manual_edits_matches_lock,
                "recorded_replay_status": self.replay_status,
            },
            "facade": (
                {
                    "export_count": self.facade_export_count,
                    "basis": "facade writer re-homed exports, not port_source rewrites",
                }
                if self.is_root_facade
                else None
            ),
            "existing_recorded_cases": self.existing_recorded_cases,
            "existing_recorded_public_functions": self.existing_recorded_public_functions,
        }


def _display(path: Path) -> str:
    """Name an input by its repo-relative path so the archive stays machine-independent."""
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    """Read one JSON object, or fail naming the input it came from."""
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"{label} could not be read: {exc}") from exc
    if not isinstance(loaded, dict):
        raise InventoryError(f"{label} is not a JSON object")
    return {str(key): value for key, value in loaded.items()}


def _read_text(path: Path) -> str:
    """Read UTF-8 source text, naming the file when it is not readable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InventoryError(f"{path} could not be read as UTF-8 text: {exc}") from exc


def _rows_by_path(value: object, label: str) -> dict[str, dict[str, Any]]:
    """Index one JSON record list by its ``path`` field."""
    if not isinstance(value, list):
        raise InventoryError(f"{label} is not a list")
    indexed: dict[str, dict[str, Any]] = {}
    for entry in value:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise InventoryError(f"{label} has a row without a string path")
        indexed[str(entry["path"])] = {str(key): item for key, item in entry.items()}
    return indexed


def _carried_list(row: dict[str, Any], key: str, path: str) -> list[Any]:
    """Take one carried-forward claim from the previous archive unchanged."""
    value = row[key]
    if not isinstance(value, list):
        raise InventoryError(f"{path}: carried {key} is not a list")
    return value


def _disk_paths(ported_root: Path) -> set[str]:
    """List ported payload files, excluding the tree metadata documents no lock can hash."""
    if not ported_root.is_dir():
        raise InventoryError(f"ported tree does not exist: {ported_root}")
    return {
        path.relative_to(ported_root).as_posix()
        for path in ported_root.rglob("*")
        if path.is_file() and path.name not in UNLOCKED_METADATA and "__pycache__" not in path.parts
    }


def _is_python(path: str) -> bool:
    """Whether a locked path is a ported module rather than a verbatim resource copy."""
    return PurePosixPath(path).suffix == _PORTED_SUFFIX


def _module_of(path: str) -> str:
    """Return the first path segment, or the root marker for a top-level file."""
    parts = PurePosixPath(path).parts
    return parts[0] if len(parts) > 1 else _ROOT_MODULE


def _batch_modules() -> frozenset[str]:
    """The B1.1 directory set exactly as ``port_scope._validate_rows`` derives it."""
    return frozenset(port_scope.B1_1_PLAN_MODULE_FILE_COUNTS) | port_scope.B1_1_EXTRA_D9_MODULES


def _check_path_coverage(
    *, lock_paths: set[str], surfaces: tuple[tuple[str, set[str]], ...]
) -> None:
    """Refuse to archive an inventory whose path sets are not the locked ones."""
    for label, paths in surfaces:
        missing = sorted(lock_paths - paths)
        if missing:
            raise InventoryError(f"locked paths missing from {label}: {missing[:4]}")
        extra = sorted(paths - lock_paths)
        if extra:
            raise InventoryError(f"{label} carries paths absent from {LOCK_REL}: {extra[:4]}")
    python = sum(1 for path in lock_paths if _is_python(path))
    resources = len(lock_paths) - python
    expected = (
        port_scope.EXPECTED_TOTAL_FILES,
        port_scope.EXPECTED_PYTHON_FILES,
        port_scope.EXPECTED_RESOURCE_FILES,
    )
    if (len(lock_paths), python, resources) != expected:
        raise InventoryError(
            f"locked paths are {len(lock_paths)} total / {python} python / {resources} "
            f"resources; expected {expected[0]} / {expected[1]} / {expected[2]}"
        )


def _replay(
    path: str,
    lock_row: dict[str, Any],
    upstream_repo: Path,
    ported_root: Path,
    url: str,
    commit: str,
) -> Replay:
    """Reproduce one ported file from the pinned upstream without writing a single byte."""
    upstream_path = str(lock_row["upstream_path"])
    source = upstream_repo / PurePosixPath(upstream_path)
    if not source.is_file():
        raise InventoryError(f"{upstream_path} is missing from the upstream checkout")
    recorded = bool(lock_row.get("manual_edits"))
    if not _is_python(path):
        data = source.read_bytes()
        return Replay(sha256_bytes(data), sha256_bytes(data), 0, 0, recorded)
    text = _read_text(source)
    if upstream_path == _INIT_UPSTREAM_PATH:
        # The root facade is upstream's export table pruned to the ported modules and rendered by
        # the migration's own writer, so its deterministic replay is ``vendor_init_facade``. It
        # performs no port_source rewrite, hence 0 below, and the work it does instead -- re-homing
        # upstream's own export statements -- is carried under its own name.
        facade, facade_report = vendor_init_facade(source, ported_root, url, commit)
        return Replay(
            sha256_text(text),
            sha256_bytes(facade),
            0,
            0,
            recorded,
            int(facade_report["export_count"]),
        )
    ported, result = port_source(text, upstream_path, url, commit)
    return Replay(
        result.upstream_sha256,
        sha256_bytes(ported.encode("utf-8")),
        result.import_rewrites,
        result.string_rewrites,
        result.manual_edits,
    )


def _carried_batch(path: str, kind: str, carried: dict[str, Any]) -> list[Any]:
    """Return the frozen batch labels as written, only after the derivation agrees with them."""
    batch = carried["batch"]
    if not isinstance(batch, list):
        raise InventoryError(f"{path}: carried batch is not a list")
    labels = [str(label) for label in batch]
    derived = sorted(port_scope._expected_batches(path, kind, _batch_modules()))
    if sorted(set(labels)) != derived:
        raise InventoryError(
            f"{path}: carried batch disagrees with the derivation "
            f"(carried={sorted(set(labels))}, derived={derived})"
        )
    return batch


def _snapshot_lines(manifest_row: dict[str, Any], path: str) -> int:
    """Read the line count the vendor manifest froze for one ported module."""
    value = manifest_row.get("lines")
    if isinstance(value, bool) or not isinstance(value, int):
        raise InventoryError(f"{path}: {MANIFEST_REL} records no integer line count")
    return value


def _build_row(
    path: str,
    lock_row: dict[str, Any],
    manifest_row: dict[str, Any],
    carried: dict[str, Any],
    ported_root: Path,
    upstream_repo: Path,
    url: str,
    commit: str,
) -> Row:
    """Reconcile one locked path from re-measured bytes plus carried-forward scope claims."""
    missing = sorted(CARRIED_ROW_KEYS - set(carried))
    if missing:
        raise InventoryError(f"{CARRIED_FROM_REL} row for {path} is missing {missing}")
    reason = carried["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise InventoryError(f"{CARRIED_FROM_REL} row for {path} has no rationale")
    python = _is_python(path)
    replay = _replay(path, lock_row, upstream_repo, ported_root, url, commit)
    upstream_path = str(lock_row["upstream_path"])
    lock_sha = str(lock_row["sha256"])
    if replay.upstream_sha != lock_sha:
        raise InventoryError(
            f"{path} pristine hash left the lock (lock={lock_sha[:12]}, "
            f"checkout={replay.upstream_sha[:12]})"
        )
    disk = ported_root / PurePosixPath(path)
    if not disk.is_file():
        raise InventoryError(f"{path} is missing from the ported tree on disk")
    disk_bytes = disk.read_bytes()
    disk_sha = sha256_bytes(disk_bytes)
    if python and replay.replayed_sha != disk_sha:
        raise InventoryError(f"{path} differs from the deterministic codemod replay (unregistered)")
    manual_edits = lock_row.get("manual_edits")
    return Row(
        path=path,
        kind="python" if python else "resource",
        module=_module_of(path),
        batch=_carried_batch(path, "python" if python else "resource", carried),
        reason=reason,
        upstream_path=upstream_path,
        manual_edits=manual_edits is True,
        lock_sha=lock_sha,
        upstream_sha=replay.upstream_sha,
        manifest_sha=str(manifest_row["sha256"]),
        disk_sha=disk_sha,
        manifest_section="files" if python else "resources",
        manifest_upstream_path_matches_lock=manifest_row.get("upstream_path") == upstream_path,
        manifest_manual_edits_matches_lock=(
            manifest_row.get("manual_edits") == manual_edits if python else None
        ),
        snapshot_lines=_snapshot_lines(manifest_row, path) if python else None,
        current_lines=disk_bytes.decode("utf-8").count("\n") if python else None,
        import_rewrites=replay.import_rewrites,
        string_rewrites=replay.string_rewrites,
        report_manual_edits_matches_lock=replay.manual_edits == bool(manual_edits),
        replay_status=REPLAY_MATCH if replay.replayed_sha == disk_sha else REPLAY_DIFFERS,
        existing_recorded_cases=_carried_list(carried, "existing_recorded_cases", path),
        existing_recorded_public_functions=_carried_list(
            carried, "existing_recorded_public_functions", path
        ),
        facade_export_count=replay.facade_export_count,
    )


def _build_rows(
    lock: UpstreamLock,
    manifest_rows: dict[str, dict[str, Any]],
    carried_rows: dict[str, dict[str, Any]],
    ported_root: Path,
    upstream_repo: Path,
) -> list[Row]:
    """Reconcile every locked path once and return the rows ordered by path."""
    return [
        _build_row(
            path,
            lock.files[path],
            manifest_rows[path],
            carried_rows[path],
            ported_root,
            upstream_repo,
            lock.url,
            lock.commit,
        )
        for path in sorted(lock.files)
    ]


def _reconciliation(
    rows: list[Row],
    lock: UpstreamLock,
    manifest_files: dict[str, dict[str, Any]],
    manifest_resources: dict[str, dict[str, Any]],
    carried_from: Path,
) -> dict[str, object]:
    """Recompute every scope counter from the rows and the records read in this run."""
    lock_paths = set(lock.files)
    report_paths = {row.path for row in rows}
    ported_module_rows = sum(1 for row in rows if row.kind == "python" and not row.is_root_facade)
    return {
        "lock_file_records": len(lock_paths),
        "lock_python_records": sum(1 for row in rows if row.kind == "python"),
        "lock_resource_records": sum(1 for row in rows if row.kind == "resource"),
        "manifest_file_records": len(manifest_files),
        "manifest_resource_records": len(manifest_resources),
        "lock_manifest_path_set_equal": {
            *manifest_files,
            *manifest_resources,
        }
        == lock_paths,
        "lock_manifest_upstream_path_and_manual_metadata_match": all(
            row.manifest_upstream_path_matches_lock
            and row.manifest_manual_edits_matches_lock is not False
            for row in rows
        ),
        "upstream_checkout_sha_match_count": sum(1 for row in rows if row.upstream_hash_current),
        "manifest_snapshot_sha_matches_current_port_count": sum(
            1 for row in rows if row.manifest_sha == row.disk_sha
        ),
        "manifest_python_line_count_matches_current_count": sum(
            1 for row in rows if row.kind == "python" and row.snapshot_lines == row.current_lines
        ),
        "port_report_rows": len(report_paths),
        "port_report_path_set_equal_lock": report_paths == lock_paths,
        "port_report_upstream_path_and_manual_metadata_match_count": sum(
            1 for row in rows if row.report_manual_edits_matches_lock
        ),
        "port_report_recorded_replay_pass_rows": sum(
            1 for row in rows if row.replay_status == REPLAY_MATCH
        ),
        "port_report_import_rewrite_total": sum(row.import_rewrites for row in rows),
        "port_report_string_rewrite_total": sum(row.string_rewrites for row in rows),
        "facade_rows": sum(1 for row in rows if row.is_root_facade),
        "facade_export_rehomed_total": sum(row.facade_export_count for row in rows),
        "manual_edits_true_rows": sum(1 for row in rows if row.manual_edits),
        "method_note": (
            "Manifest files/resources were joined by path, not row order. Upstream and ported "
            "hashes, line counts, rewrite counts and replay verdicts were re-measured in this run "
            "by replaying the deterministic codemod against the pinned upstream checkout; reason, "
            "batch, scope_basis and the AC-6|02 recorded-fixture inventory are carried forward "
            f"verbatim by path from {_display(carried_from)}. The import/string rewrite totals "
            f"cover the {ported_module_rows} modules carried through port_source; the root facade "
            "is written by vendor_init_facade, which performs no port_source rewrite and instead "
            "re-homes upstream's own export statements, counted by facade_export_rehomed_total. "
            "No test suite, gate, manifest or report generator, comparison replay, or network "
            "call was executed."
        ),
    }


def build_inventory(
    *,
    ported_root: Path,
    upstream_repo: Path,
    carried_from: Path,
) -> dict[str, object]:
    """Read the live tree and the previous archive, and return this round's inventory."""
    lock = UpstreamLock.load(ported_root / LOCK_REL)
    verify_upstream(upstream_repo, lock)
    manifest_path = ported_root / MANIFEST_REL
    manifest = _read_json(manifest_path, _display(manifest_path))
    previous = _read_json(carried_from, _display(carried_from))
    missing_top = sorted(CARRIED_TOP_LEVEL_KEYS - set(previous))
    if missing_top:
        raise InventoryError(f"{carried_from} is missing {missing_top}")
    manifest_files = _rows_by_path(manifest.get("files"), f"{manifest_path}#files")
    manifest_resources = _rows_by_path(manifest.get("resources"), f"{manifest_path}#resources")
    joined = dict(manifest_files)
    joined.update(manifest_resources)
    carried_rows = _rows_by_path(previous.get("files"), f"{_display(carried_from)}#files")
    _check_path_coverage(
        lock_paths=set(lock.files),
        surfaces=(
            (_display(carried_from), set(carried_rows)),
            (_display(manifest_path), set(joined)),
            ("the ported tree on disk", _disk_paths(ported_root)),
        ),
    )
    rows = _build_rows(lock, joined, carried_rows, ported_root, upstream_repo)
    return {
        "schema_version": 1,
        "as_of": AS_OF,
        "purpose": (
            f"{ROUND} file-level module/batch/reason/hash inventory and per-path reconciliation; "
            "hashes, line counts, rewrite counts and replay verdicts re-measured from the live "
            "tree, scope and recorded-fixture claims carried forward by path; no test, gate, "
            "manifest or report regeneration, or network action."
        ),
        "upstream_commit": lock.commit,
        "scope_basis": previous["scope_basis"],
        "reconciliation": _reconciliation(
            rows, lock, manifest_files, manifest_resources, carried_from
        ),
        "ac6_02_existing_fixture_inventory": previous["ac6_02_existing_fixture_inventory"],
        "files": [row.to_json() for row in rows],
    }


def _serialize(payload: dict[str, object]) -> str:
    """Serialize the inventory in the archived key order, deterministically."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _summary(reconciliation: object) -> str:
    """Render the row counts this run reconciled, straight out of the recomputed block."""
    counts = reconciliation if isinstance(reconciliation, dict) else {}
    return ", ".join(
        f"{key}={counts.get(key)}"
        for key in ("lock_file_records", "lock_python_records", "lock_resource_records")
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit code 1 and no file written when any fail-closed rule fires."""
    parser = argparse.ArgumentParser(description="Generate a round's port-scope inventory.")
    parser.add_argument(
        "-o", "--output", type=Path, required=True, help="inventory JSON path to write"
    )
    parser.add_argument(
        "--ported-root",
        type=Path,
        default=PORTED_ROOT,
        help="vendored port tree (default: scripts.codemod.port_module.PORTED_ROOT)",
    )
    parser.add_argument(
        "--upstream-repo", type=Path, default=DEFAULT_UPSTREAM_REPO, help="upstream clone"
    )
    parser.add_argument(
        "--carried-from",
        type=Path,
        default=_REPO_ROOT / CARRIED_FROM_REL,
        help="previous round's inventory whose scope claims are carried forward",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite the output when it already exists"
    )
    args = parser.parse_args(argv)
    output: Path = args.output
    if output.exists() and not args.force:
        print(f"FAIL: {output} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    try:
        payload = build_inventory(
            ported_root=args.ported_root,
            upstream_repo=args.upstream_repo,
            carried_from=args.carried_from,
        )
        text = _serialize(payload)
    except (InventoryError, RuntimeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"OK: wrote {output} ({_summary(payload['reconciliation'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
