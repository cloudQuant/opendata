#!/usr/bin/env python3
"""Reconcile stock-daily gaps from an explicit, bounded offline evidence bundle.

The command reads only ``manifest.json`` and the hash-named regular files under
``artifacts/`` in the caller-selected evidence directory. It never reads an
environment file, database, provider, or clock; it cannot create a backfill or
dispatch work. A valid hash proves byte consistency only, not source authenticity,
rights, a complete A-share census, or that historical observations existed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from opendata.data.instrument_reference_snapshot import (  # noqa: E402
    MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES,
)
from opendata.pipeline.stock_daily_gap_evidence_bundle import (  # noqa: E402
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    MAX_MANIFEST_BYTES,
    MAX_SERIES,
    MAX_TOTAL_ARTIFACT_BYTES,
    StockDailyGapEvidenceError,
    load_stock_daily_gap_evidence_bundle,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_KIND: Final[str] = "instrument_registry"
_MANIFEST_NAME: Final[str] = "manifest.json"
_ARTIFACT_DIRECTORY_NAME: Final[str] = "artifacts"
_NONBLOCK_FLAG: Final[int | None] = getattr(os, "O_NONBLOCK", None)
_DIRECTORY_FLAG: Final[int | None] = getattr(os, "O_DIRECTORY", None)
_NOFOLLOW_FLAG: Final[int | None] = getattr(os, "O_NOFOLLOW", None)
_CHUNK_SIZE: Final[int] = 64 * 1024


class _EvidenceInputError(ValueError):
    """Raised for a path, size, or manifest boundary failure."""


class _ArgumentParseError(ValueError):
    """Raised without argparse's variable usage text for invalid CLI arguments."""


class _JsonArgumentParser(argparse.ArgumentParser):
    """Keep invalid argument output in the same deterministic JSON contract."""

    def error(self, message: str) -> NoReturn:
        """Suppress argparse text and route failures to the stable error document."""
        del message
        raise _ArgumentParseError("invalid command-line arguments")


def build_parser() -> argparse.ArgumentParser:
    """Build the single-root CLI; evidence filenames are fixed by the schema."""
    parser = _JsonArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-dir",
        required=True,
        help="absolute directory containing manifest.json and artifacts/<sha256>.json",
    )
    return parser


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate keys while reading only the manifest's artifact pins."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """Reject JSON NaN and Infinity extensions."""
    raise ValueError(f"invalid JSON constant: {value}")


def _open_absolute_directory(path_text: str) -> int:
    """Open every directory component without following a symlink."""
    directory_flags = _safe_open_flags(directory=True)
    path = Path(path_text)
    if not path.is_absolute() or "\x00" in path_text:
        raise _EvidenceInputError("evidence directory must be an absolute path")
    parts = path.parts
    if any(part in {".", "..", ""} for part in parts[1:]):
        raise _EvidenceInputError("evidence directory contains an unsafe path component")

    current_fd = os.open(path.anchor, directory_flags)
    try:
        for component in parts[1:]:
            next_fd = os.open(
                component,
                directory_flags,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
    except OSError:
        os.close(current_fd)
        raise
    return current_fd


def _read_regular_file_at(directory_fd: int, name: str, *, limit: int) -> bytes:
    """Read one bounded regular file by a fixed basename, rejecting links."""
    file_flags = _safe_open_flags(directory=False)
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise _EvidenceInputError("unsafe evidence filename")
    file_fd = os.open(name, file_flags, dir_fd=directory_fd)
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise _EvidenceInputError("evidence input must be a single-link regular file")
        if before.st_size > limit:
            raise _EvidenceInputError("evidence input exceeds its byte limit")

        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(file_fd, min(_CHUNK_SIZE, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > limit:
            raise _EvidenceInputError("evidence input exceeds its byte limit")

        after = os.fstat(file_fd)
        stable_fields_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        stable_fields_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if stable_fields_before != stable_fields_after or len(content) != after.st_size:
            raise _EvidenceInputError("evidence input changed while it was being read")
        return bytes(content)
    finally:
        os.close(file_fd)


def _safe_open_flags(*, directory: bool) -> int:
    """Build open flags only when path, symlink, and FIFO protections exist."""
    if _NOFOLLOW_FLAG is None or _NONBLOCK_FLAG is None or _DIRECTORY_FLAG is None:
        raise _EvidenceInputError("this platform cannot enforce safe nonblocking reads")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW_FLAG | _NONBLOCK_FLAG
    if directory:
        flags |= _DIRECTORY_FLAG
    return flags


def _manifest_artifact_pins(manifest: bytes) -> tuple[tuple[str, str, str], ...]:
    """Extract only bounded ref, kind, and digest fields needed for safe reads."""
    try:
        value = json.loads(
            manifest.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise _EvidenceInputError("manifest is not strict UTF-8 JSON") from None
    if not isinstance(value, dict):
        raise _EvidenceInputError("manifest root must be an object")
    if set(value) != {"schema_version", "registry_ref", "artifacts", "series"}:
        raise _EvidenceInputError("manifest has missing or unsupported fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _EvidenceInputError("manifest schema_version must be integer 1")
    registry_ref = value["registry_ref"]
    if (
        not isinstance(registry_ref, str)
        or not registry_ref
        or registry_ref != registry_ref.strip()
        or len(registry_ref) > 512
        or any(ord(character) < 32 for character in registry_ref)
    ):
        raise _EvidenceInputError("manifest registry_ref is invalid")
    rows = value.get("artifacts")
    if not isinstance(rows, list) or len(rows) > MAX_ARTIFACTS:
        raise _EvidenceInputError("manifest artifact list exceeds its schema limit")
    series = value["series"]
    if not isinstance(series, list) or not series or len(series) > MAX_SERIES:
        raise _EvidenceInputError("manifest series list exceeds its schema limit")

    pins: list[tuple[str, str, str]] = []
    seen_refs: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"evidence_ref", "kind", "sha256"}:
            raise _EvidenceInputError("manifest artifact descriptor has an invalid shape")
        reference = row["evidence_ref"]
        kind = row["kind"]
        digest = row["sha256"]
        if (
            not isinstance(reference, str)
            or not reference
            or reference != reference.strip()
            or len(reference) > 512
            or any(ord(character) < 32 for character in reference)
            or reference in seen_refs
        ):
            raise _EvidenceInputError("manifest artifact references must be unique strings")
        if (
            not isinstance(kind, str)
            or not kind
            or kind != kind.strip()
            or len(kind) > 512
            or any(ord(character) < 32 for character in kind)
        ):
            raise _EvidenceInputError("manifest artifact kind is invalid")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise _EvidenceInputError("manifest artifact digest is not lowercase SHA-256")
        seen_refs.add(reference)
        pins.append((reference, kind, digest))
    registry_pins = [pin for pin in pins if pin[0] == registry_ref and pin[1] == _REGISTRY_KIND]
    registry_count = sum(kind == _REGISTRY_KIND for _, kind, _ in pins)
    if len(registry_pins) != 1 or registry_count != 1:
        raise _EvidenceInputError("manifest must pin exactly one registry snapshot")
    return tuple(pins)


def _load_evidence(directory_fd: int) -> tuple[bytes, dict[str, bytes]]:
    """Read fixed manifest and hash-named artifacts from a private directory FD."""
    manifest = _read_regular_file_at(directory_fd, _MANIFEST_NAME, limit=MAX_MANIFEST_BYTES)
    pins = _manifest_artifact_pins(manifest)
    artifact_directory_fd = os.open(
        _ARTIFACT_DIRECTORY_NAME,
        _safe_open_flags(directory=True),
        dir_fd=directory_fd,
    )
    try:
        artifacts: dict[str, bytes] = {}
        total_bytes = 0
        for reference, kind, digest in sorted(pins, key=lambda item: (item[2], item[0])):
            limit = (
                MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES
                if kind == _REGISTRY_KIND
                else MAX_ARTIFACT_BYTES
            )
            raw = _read_regular_file_at(artifact_directory_fd, f"{digest}.json", limit=limit)
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL_ARTIFACT_BYTES:
                raise _EvidenceInputError("total artifact bytes exceed the bundle limit")
            artifacts[reference] = raw
        return manifest, artifacts
    finally:
        os.close(artifact_directory_fd)


def _emit(value: object) -> None:
    """Write one deterministic compact JSON document to stdout."""
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    )


def _blocked(code: str) -> int:
    """Emit a stable, path-free blocked result."""
    _emit({"error": {"code": code}, "schema_version": 1, "status": "blocked"})
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only local reconciliation and return a stable exit status."""
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except _ArgumentParseError:
        return _blocked("invalid_arguments")
    directory_fd: int | None = None
    try:
        directory_fd = _open_absolute_directory(args.evidence_dir)
        manifest, artifacts = _load_evidence(directory_fd)
    except (OSError, _EvidenceInputError):
        return _blocked("evidence_unavailable_or_unsafe")
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    try:
        bundle = load_stock_daily_gap_evidence_bundle(manifest, artifacts=artifacts)
    except (StockDailyGapEvidenceError, TypeError, ValueError):
        return _blocked("invalid_evidence_bundle")

    sys.stdout.write(bundle.to_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
