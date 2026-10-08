#!/usr/bin/env python3
"""Publish database dumps and the local minute archive as one verified snapshot."""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import uuid
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, cast

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

_SNAPSHOT_NAME = re.compile(r"^snapshot_(?P<stamp>\d{8}T\d{6}Z)_(?P<token>[0-9a-f]{12})$")
_DB_DUMP_NAME = re.compile(r"^(?P<label>metadata|warehouse)_\d{8}T\d{6}Z_\d+\.sql\.gz$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_NAME = "manifest.json"
_MINUTE_ARCHIVE_NAME = "minute_archive.tar.gz"


class SnapshotError(RuntimeError):
    """Raised when a complete, safe paired snapshot cannot be produced."""


class _HashingReader:
    """Read a tar member while calculating the exact source-byte checksum."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        """Read and account for bytes consumed by ``tarfile``."""
        chunk = self._source.read(size)
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def _resolved_directory(path: Path | str, *, match_parent_owner: bool = False) -> Path:
    """Create a directory and return its canonical path, refusing a symlink leaf.

    When initializing the archive root, copy only the parent directory's owner
    and group onto that newly-created root. Existing directories are untouched.
    """
    configured = Path(path).expanduser()
    if configured.is_symlink():
        raise SnapshotError(f"configured directory must not be a symlink: {configured.name}")
    try:
        if configured.exists():
            if not configured.is_dir():
                raise SnapshotError(f"configured path is not a safe directory: {configured.name}")
        elif match_parent_owner:
            parent = configured.parent.resolve(strict=True)
            parent_stat = parent.stat()
            created = parent / configured.name
            created.mkdir()
            os.chown(created, parent_stat.st_uid, parent_stat.st_gid)
            configured = created
        else:
            configured.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SnapshotError(f"could not create configured directory: {configured.name}") from exc
    if configured.is_symlink() or not configured.is_dir():
        raise SnapshotError(f"configured path is not a safe directory: {configured.name}")
    return configured.resolve(strict=True)


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open one regular file without following a final-component symlink."""
    if path.is_symlink():
        raise SnapshotError(f"refusing symlink: {path.name}")
    safe_flags = flags
    if hasattr(os, "O_NOFOLLOW"):
        safe_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, safe_flags, mode)
    except OSError as exc:
        raise SnapshotError(f"could not safely open file: {path.name}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SnapshotError(f"expected a regular file: {path.name}")
    return descriptor


@contextmanager
def _exclusive_snapshot_lock(archive_root: Path) -> Iterator[None]:
    """Exclude minute readers, writers, and retention during paired snapshot creation."""
    lock_path = archive_root / ".snapshot.lock"
    descriptor, created = _open_lock_file(lock_path)
    locked = False
    try:
        if created:
            root_stat = archive_root.stat()
            descriptor_stat = os.fstat(descriptor)
            if (descriptor_stat.st_uid, descriptor_stat.st_gid) != (
                root_stat.st_uid,
                root_stat.st_gid,
            ):
                os.fchown(descriptor, root_stat.st_uid, root_stat.st_gid)
            os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
    except OSError as exc:
        os.close(descriptor)
        raise SnapshotError("could not lock the minute archive for a consistent snapshot") from exc
    try:
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _open_lock_file(path: Path) -> tuple[int, bool]:
    """Open or create a no-follow regular lock file and report whether new."""
    if path.is_symlink():
        raise SnapshotError(f"refusing symlink lock file: {path.name}")
    base_flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        base_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, base_flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(path, base_flags)
        except OSError as exc:
            raise SnapshotError(f"could not safely open lock file: {path.name}") from exc
        created = False
    except OSError as exc:
        raise SnapshotError(f"could not safely create lock file: {path.name}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SnapshotError(f"expected a regular lock file: {path.name}")
    return descriptor, created


def _relative_name(root: Path, path: Path) -> str:
    """Return a safe POSIX path relative to a verified archive root."""
    try:
        name = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise SnapshotError("minute archive path escaped its configured root") from exc
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise SnapshotError("minute archive contains an unsafe relative path")
    return name


def _archive_files(root: Path) -> list[tuple[str, Path, int]]:
    """List regular archive files, excluding synchronization-only lock files."""
    files: list[tuple[str, Path, int]] = []

    def visit(directory: Path, *, include_files: bool = True) -> None:
        try:
            with os.scandir(directory) as scan:
                names = sorted(entry.name for entry in scan)
        except OSError as exc:
            raise SnapshotError("could not enumerate the minute archive") from exc
        for name in names:
            path = directory / name
            try:
                entry_stat = path.lstat()
            except OSError as exc:
                raise SnapshotError("could not inspect a minute archive entry") from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise SnapshotError(f"minute archive contains a symlink: {name}")
            if directory == root and name == ".locks":
                if not stat.S_ISDIR(entry_stat.st_mode):
                    raise SnapshotError("minute archive .locks path is not a directory")
                visit(path, include_files=False)
                continue
            if directory == root and name == ".snapshot.lock":
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise SnapshotError("minute archive snapshot lock is not a regular file")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(path, include_files=include_files)
            elif stat.S_ISREG(entry_stat.st_mode):
                if include_files:
                    files.append((_relative_name(root, path), path, entry_stat.st_size))
            else:
                raise SnapshotError(f"minute archive contains a special file: {name}")

    visit(root)
    return files


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    """Hash a binary stream without retaining its contents in memory."""
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _hash_file(path: Path) -> tuple[str, int]:
    """Hash one regular file while refusing symlink substitution."""
    descriptor = _open_regular(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as stream:
        return _hash_stream(stream)


def _write_minute_archive(root: Path, destination: Path) -> list[dict[str, object]]:
    """Write and verify a tarball containing every stable regular archive file."""
    inventory = _archive_files(root)
    expected: dict[str, tuple[int, str]] = {}
    descriptor = _open_regular(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz", compresslevel=6) as archive:
                for relative_name, source_path, expected_size in inventory:
                    source_descriptor = _open_regular(source_path, os.O_RDONLY)
                    with os.fdopen(source_descriptor, "rb") as source:
                        before = os.fstat(source.fileno())
                        if before.st_size != expected_size:
                            raise SnapshotError(
                                "minute archive changed while preparing its snapshot"
                            )
                        reader = _HashingReader(source)
                        member = tarfile.TarInfo(relative_name)
                        member.type = tarfile.REGTYPE
                        member.mode = 0o600
                        member.mtime = 0
                        member.size = expected_size
                        archive.addfile(member, reader)
                        after = os.fstat(source.fileno())
                        if (
                            reader.size != expected_size
                            or before.st_size != after.st_size
                            or before.st_mtime_ns != after.st_mtime_ns
                            or before.st_ino != after.st_ino
                        ):
                            raise SnapshotError(
                                "minute archive file changed during snapshot creation"
                            )
                        expected[relative_name] = (reader.size, reader.digest.hexdigest())
            output.flush()
            os.fsync(output.fileno())
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError("could not create a valid minute archive tarball") from exc

    observed: dict[str, tuple[int, str]] = {}
    try:
        with tarfile.open(destination, mode="r:gz") as archive:
            for member in archive.getmembers():
                name = PurePosixPath(member.name)
                if name.is_absolute() or not name.parts or ".." in name.parts:
                    raise SnapshotError("minute tar contains an unsafe member path")
                if not member.isfile() or member.name in observed:
                    raise SnapshotError("minute tar contains a duplicate or non-regular member")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SnapshotError("minute tar member cannot be read")
                with stream:
                    digest, size = _hash_stream(cast("BinaryIO", stream))
                    observed[member.name] = (size, digest)
    except (OSError, tarfile.TarError) as exc:
        raise SnapshotError("minute tar verification failed") from exc
    if observed != expected:
        raise SnapshotError("minute tar content does not match the locked archive files")
    return [
        {"path": name, "size": expected[name][0], "sha256": expected[name][1]}
        for name in sorted(expected)
    ]


def _database_payloads(stage: Path) -> dict[str, dict[str, object]]:
    """Validate and checksum the one metadata and one warehouse SQL dump."""
    dumps: dict[str, list[Path]] = {"metadata": [], "warehouse": []}
    for entry in stage.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise SnapshotError("database backup staging contains an unexpected entry")
        match = _DB_DUMP_NAME.fullmatch(entry.name)
        if match is None:
            raise SnapshotError("database backup staging contains an unexpected file")
        label = match.group("label")
        dumps[label].append(entry)
    if any(len(dumps[label]) != 1 for label in dumps):
        raise SnapshotError("database backup did not produce exactly two named dumps")

    payloads: dict[str, dict[str, object]] = {}
    for label in ("metadata", "warehouse"):
        path = dumps[label][0]
        try:
            with gzip.open(path, "rb") as compressed:
                decompressed_size = sum(
                    len(chunk) for chunk in iter(lambda: compressed.read(1024 * 1024), b"")
                )
        except (OSError, EOFError) as exc:
            raise SnapshotError(f"{label} SQL dump is not a valid gzip file") from exc
        if decompressed_size < 1:
            raise SnapshotError(f"{label} SQL dump is empty")
        digest, size = _hash_file(path)
        payloads[path.name] = {"size": size, "sha256": digest}
    return payloads


def _write_manifest(stage: Path, manifest: Mapping[str, object]) -> None:
    """Create the final manifest as a private, exclusive regular file."""
    manifest_path = stage / _MANIFEST_NAME
    descriptor = _open_regular(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode=0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _fsync_snapshot_contents(stage: Path) -> None:
    """Flush every completed payload and the staging directory before rename."""
    for path in stage.iterdir():
        if path.is_symlink() or not path.is_file():
            raise SnapshotError("snapshot staging contains a non-regular file")
        descriptor = _open_regular(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_directory(stage)


def _validate_relative_manifest_path(value: object) -> str:
    """Validate an archive member path from a snapshot manifest."""
    if not isinstance(value, str):
        raise SnapshotError("snapshot manifest contains a non-string archive path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in value:
        raise SnapshotError("snapshot manifest contains an unsafe archive path")
    return path.as_posix()


def _manifest_files(
    manifest: object,
) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    """Validate manifest structure and return its payload and minute-file entries."""
    if not isinstance(manifest, dict) or manifest.get("format") != "opendata-paired-backup-v1":
        raise SnapshotError("snapshot manifest has an unsupported format")
    raw_payloads = manifest.get("payloads")
    raw_minute_files = manifest.get("minute_archive_files")
    if not isinstance(raw_payloads, dict) or not isinstance(raw_minute_files, list):
        raise SnapshotError("snapshot manifest is incomplete")
    payloads: dict[str, dict[str, object]] = {}
    for name, raw_record in raw_payloads.items():
        safe_name = _validate_relative_manifest_path(name)
        if "/" in safe_name or safe_name == _MANIFEST_NAME:
            raise SnapshotError("snapshot payload name is not a direct child filename")
        if not isinstance(raw_record, dict):
            raise SnapshotError("snapshot payload checksum record is malformed")
        if (
            not isinstance(raw_record.get("size"), int)
            or raw_record["size"] < 0
            or not isinstance(raw_record.get("sha256"), str)
            or not _SHA256.fullmatch(cast("str", raw_record["sha256"]))
        ):
            raise SnapshotError("snapshot payload checksum record is invalid")
        payloads[safe_name] = raw_record
    if len(payloads) != len(raw_payloads):
        raise SnapshotError("snapshot manifest repeats a payload name")

    minute_files: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for record in raw_minute_files:
        if not isinstance(record, dict):
            raise SnapshotError("minute file manifest entry is malformed")
        name = _validate_relative_manifest_path(record.get("path"))
        if (
            name in seen_paths
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("sha256"), str)
            or not _SHA256.fullmatch(cast("str", record["sha256"]))
        ):
            raise SnapshotError("minute file manifest entry is invalid")
        seen_paths.add(name)
        minute_files.append(record)
    return payloads, minute_files


def _validate_snapshot(snapshot: Path, *, require_published_name: bool = True) -> bool:
    """Verify a published snapshot directory and every payload hash."""
    if snapshot.is_symlink() or not snapshot.is_dir():
        return False
    try:
        if require_published_name:
            match = _SNAPSHOT_NAME.fullmatch(snapshot.name)
            if match is None:
                return False
            datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ")
        with os.fdopen(_open_regular(snapshot / _MANIFEST_NAME, os.O_RDONLY), "r") as stream:
            manifest = json.load(stream)
        payloads, minute_files = _manifest_files(manifest)
        if _MINUTE_ARCHIVE_NAME not in payloads:
            return False
        dump_labels: set[str] = set()
        expected_names = set(payloads) | {_MANIFEST_NAME}
        for path in snapshot.iterdir():
            if path.is_symlink() or not path.is_file() or path.name not in expected_names:
                return False
        if {path.name for path in snapshot.iterdir()} != expected_names:
            return False
        for name, record in payloads.items():
            path = snapshot / name
            if not path.is_file() or path.is_symlink():
                return False
            digest, size = _hash_file(path)
            if digest != record["sha256"] or size != record["size"]:
                return False
            if name != _MINUTE_ARCHIVE_NAME:
                dump_match = _DB_DUMP_NAME.fullmatch(name)
                if dump_match is None:
                    return False
                dump_labels.add(dump_match.group("label"))
        if dump_labels != {"metadata", "warehouse"}:
            return False
        _verify_minute_archive(snapshot / _MINUTE_ARCHIVE_NAME, minute_files)
        return True
    except (OSError, ValueError, SnapshotError, tarfile.TarError, json.JSONDecodeError):
        return False


def _verify_minute_archive(path: Path, inventory: Sequence[Mapping[str, object]]) -> None:
    """Check tar members and checksums against the minute-file manifest."""
    expected: dict[str, tuple[int, str]] = {}
    for record in inventory:
        name = _validate_relative_manifest_path(record.get("path"))
        size = record.get("size")
        digest = record.get("sha256")
        if (
            not isinstance(size, int)
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise SnapshotError("minute archive manifest entry is invalid")
        if name in expected:
            raise SnapshotError("minute archive manifest repeats a path")
        expected[name] = (size, digest)

    observed: dict[str, tuple[int, str]] = {}
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            name = _validate_relative_manifest_path(member.name)
            if not member.isfile() or name in observed:
                raise SnapshotError("minute tar contains a duplicate or non-regular member")
            stream = archive.extractfile(member)
            if stream is None:
                raise SnapshotError("minute tar member cannot be read")
            with stream:
                digest, size = _hash_stream(cast("BinaryIO", stream))
                observed[name] = (size, digest)
    if observed != expected:
        raise SnapshotError("minute tar content does not match its manifest")


def _remove_complete_snapshot(snapshot: Path) -> None:
    """Remove only direct regular files from an already verified snapshot."""
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise SnapshotError("refusing to prune a non-directory snapshot")
    for path in snapshot.iterdir():
        if path.is_symlink() or not path.is_file():
            raise SnapshotError("refusing to prune a snapshot containing non-regular entries")
    for path in snapshot.iterdir():
        path.unlink()
    snapshot.rmdir()


def prune_snapshots(
    backup_root: Path | str,
    retention_days: int,
    *,
    now: datetime | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Prune only old, well-named snapshots with complete verified manifests."""
    if retention_days <= 0:
        raise SnapshotError("snapshot retention days must be positive")
    root = _resolved_directory(backup_root)
    current = now or datetime.now(timezone.utc)
    cutoff = current.timestamp() - retention_days * 86400
    deleted = 0
    errors: list[str] = []
    for snapshot in sorted(root.iterdir()):
        match = _SNAPSHOT_NAME.fullmatch(snapshot.name)
        if match is None or snapshot.is_symlink():
            continue
        try:
            try:
                datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ")
            except ValueError:
                continue
            if not _validate_snapshot(snapshot):
                continue
            if snapshot.lstat().st_mtime >= cutoff:
                continue
            _remove_complete_snapshot(snapshot)
            deleted += 1
        except OSError:
            errors.append(snapshot.name)
        except SnapshotError:
            errors.append(snapshot.name)
    return deleted, tuple(errors)


def create_paired_snapshot(
    backup_script: Path | str,
    archive_root: Path | str,
    backup_root: Path | str,
    retention_days: int,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Create and atomically publish one database-plus-minute-file backup."""
    if retention_days <= 0:
        raise SnapshotError("snapshot retention days must be positive")
    configured_archive = Path(archive_root).expanduser()
    configured_backup = Path(backup_root).expanduser()
    archive_guess = configured_archive.resolve(strict=False)
    backup_guess = configured_backup.resolve(strict=False)
    if backup_guess == archive_guess or backup_guess.is_relative_to(archive_guess):
        raise SnapshotError("backup directory must not be inside the minute archive")
    archive = _resolved_directory(configured_archive, match_parent_owner=True)
    backup = _resolved_directory(configured_backup)
    script_path = Path(backup_script)
    if script_path.is_symlink() or not script_path.is_file():
        raise SnapshotError("database backup script is unavailable or unsafe")

    stage: Path | None = None
    published = False
    final_path: Path | None = None
    try:
        with _exclusive_snapshot_lock(archive):
            try:
                stage = Path(tempfile.mkdtemp(prefix=".snapshot-staging-", dir=backup))
            except OSError as exc:
                raise SnapshotError("could not create a private backup staging directory") from exc
            child_environment = dict(os.environ if environment is None else environment)
            child_environment.update(
                {
                    "BACKUP_MINUTE_ARCHIVE_DIR": "",
                    "BACKUP_ENV_FILE": "",
                    "BACKUP_DIR": str(stage),
                    "BACKUP_RETENTION_DAYS": str(retention_days),
                }
            )
            try:
                # The subprocess uses a fixed executable and script argument, without a shell.
                child = subprocess.run(  # noqa: S603
                    ["/bin/bash", str(script_path)],
                    env=child_environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    shell=False,
                )  # nosec B603
            except OSError as exc:
                raise SnapshotError("database dump process could not start") from exc
            if child.returncode != 0:
                raise SnapshotError(
                    f"database dump process failed with exit code {child.returncode}"
                )

            payloads = _database_payloads(stage)
            minute_files = _write_minute_archive(archive, stage / _MINUTE_ARCHIVE_NAME)
            minute_digest, minute_size = _hash_file(stage / _MINUTE_ARCHIVE_NAME)
            payloads[_MINUTE_ARCHIVE_NAME] = {"size": minute_size, "sha256": minute_digest}
            manifest = {
                "format": "opendata-paired-backup-v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payloads": payloads,
                "minute_archive_files": minute_files,
            }
            _write_manifest(stage, manifest)
            if not _validate_snapshot(stage, require_published_name=False):
                raise SnapshotError("staged paired snapshot failed complete-manifest verification")
            _fsync_snapshot_contents(stage)

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            final_path = backup / f"snapshot_{stamp}_{uuid.uuid4().hex[:12]}"
            if final_path.exists() or final_path.is_symlink():
                raise SnapshotError("refusing to overwrite an existing snapshot")
            os.replace(stage, final_path)
            published = True
            _fsync_directory(final_path)
            _fsync_directory(backup)
    finally:
        if stage is not None and not published:
            with suppress(FileNotFoundError):
                shutil.rmtree(stage)

    deleted, prune_errors = prune_snapshots(backup, retention_days)
    if prune_errors:
        print(
            "WARNING: could not prune complete old snapshot(s): " + ", ".join(prune_errors),
            file=sys.stderr,
        )
    if final_path is None:
        raise SnapshotError("snapshot was not published")
    print(f"paired backup snapshot published: {final_path.name} (pruned={deleted})")
    return final_path


def _fsync_directory(path: Path) -> None:
    """Persist a completed directory rename where supported."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the explicit inputs supplied by the validated shell wrapper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-script", required=True, type=Path)
    parser.add_argument("--archive-root", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--retention-days", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Create one paired snapshot and return a shell-compatible status."""
    args = _parse_args(argv)
    try:
        create_paired_snapshot(
            args.backup_script,
            args.archive_root,
            args.backup_dir,
            args.retention_days,
        )
    except (SnapshotError, OSError, tarfile.TarError, ValueError) as exc:
        print(f"ERROR: paired backup failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
