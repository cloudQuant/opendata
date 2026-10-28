"""Validation for explicitly selected local SQLite database paths."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatabaseFileIdentity:
    """Best-effort filesystem identity captured for a reviewed SQLite file."""

    device: int
    inode: int


def inspect_existing_database_path(value: str | Path) -> tuple[Path, DatabaseFileIdentity]:
    """Require an absolute, canonical path to an existing regular file.

    Returns:
        The canonical path and its device/inode identity.

    Raises:
        RuntimeError: If the path is ambiguous, missing, linked, or not a file.
    """
    if isinstance(value, Path):
        candidate = value
    elif isinstance(value, str) and value.strip():
        candidate = Path(value)
    else:
        raise RuntimeError("source gate database path must be an explicit absolute path")

    if not candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise RuntimeError("source gate database path must be absolute and canonical")

    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except OSError as exc:
        raise RuntimeError("source gate database file must already exist") from exc

    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("source gate database path must name a regular, non-symlink file")
    if resolved != candidate:
        raise RuntimeError("source gate database path must not traverse symbolic links")
    return resolved, DatabaseFileIdentity(device=metadata.st_dev, inode=metadata.st_ino)


def validate_existing_database_path(value: str | Path) -> Path:
    """Validate an explicit path and return its canonical absolute form."""
    path, _identity = inspect_existing_database_path(value)
    return path


def assert_database_file_identity(value: str | Path, expected: DatabaseFileIdentity) -> None:
    """Reject a target path that no longer names the initially reviewed file.

    Device/inode checks narrow replacement races around opening and migrating.
    They are not an OS-level handle pin and do not prevent a privileged process
    from swapping a path away and back between checks.
    """
    _path, actual = inspect_existing_database_path(value)
    if actual != expected:
        raise RuntimeError("source gate database file identity changed during migration")


__all__ = [
    "DatabaseFileIdentity",
    "assert_database_file_identity",
    "inspect_existing_database_path",
    "validate_existing_database_path",
]
