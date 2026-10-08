"""Tests for atomic paired database and minute-file backup snapshots."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tarfile
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from scripts.ops.backup_minute_snapshot import (
    SnapshotError,
    create_paired_snapshot,
    prune_snapshots,
)

if TYPE_CHECKING:
    from pathlib import Path


def _fake_backup_script(path: Path, *, fail: bool = False) -> Path:
    """Create a DB-only script that writes deterministic fake gzip dumps."""
    failure = "exit 17" if fail else "exit 0"
    path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "printf 'metadata dump\\n' | gzip -n > "
        '"$BACKUP_DIR/metadata_20260930T120000Z_123.sql.gz"\n'
        "printf 'warehouse dump\\n' | gzip -n > "
        '"$BACKUP_DIR/warehouse_20260930T120000Z_123.sql.gz"\n'
        f"{failure}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _create_snapshot(
    tmp_path: Path,
    *,
    minute_files: dict[str, bytes] | None = None,
) -> tuple[Path, Path, Path]:
    """Publish a paired snapshot using only a temporary directory and fake dumps."""
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    archive_root = data_root / "minute_archive"
    archive_root.mkdir(exist_ok=True)
    for relative, contents in (minute_files or {}).items():
        target = archive_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    backup_root = tmp_path / "backups"
    script = _fake_backup_script(tmp_path / "fake-backup.sh")
    snapshot = create_paired_snapshot(script, archive_root, backup_root, 30)
    return snapshot, archive_root, backup_root


def test_snapshot_manifest_hashes_and_tar_restore_match_source_bytes(tmp_path: Path) -> None:
    """A published manifest binds both SQL dumps and every archived file."""
    source_bytes = b"parquet bytes with a stable checksum"
    snapshot, archive_root, _backup_root = _create_snapshot(
        tmp_path,
        minute_files={"stock_daily/AAPL/2026/06/part.parquet": source_bytes},
    )

    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "opendata-paired-backup-v1"
    assert set(manifest["payloads"]) == {
        "metadata_20260930T120000Z_123.sql.gz",
        "warehouse_20260930T120000Z_123.sql.gz",
        "minute_archive.tar.gz",
    }
    assert manifest["minute_archive_files"] == [
        {
            "path": "stock_daily/AAPL/2026/06/part.parquet",
            "size": len(source_bytes),
            "sha256": sha256(source_bytes).hexdigest(),
        }
    ]
    for payload_name, record in manifest["payloads"].items():
        payload = snapshot / payload_name
        assert record == {
            "size": payload.stat().st_size,
            "sha256": sha256(payload.read_bytes()).hexdigest(),
        }
    with tarfile.open(snapshot / "minute_archive.tar.gz", "r:gz") as archive:
        member = archive.getmember("stock_daily/AAPL/2026/06/part.parquet")
        restored = archive.extractfile(member)
        assert restored is not None
        assert restored.read() == source_bytes
        assert all(
            ".snapshot.lock" not in entry.name and ".locks/" not in entry.name for entry in archive
        )
    assert (archive_root / ".snapshot.lock").is_file()


def test_new_archive_root_and_snapshot_lock_match_parent_owner(tmp_path: Path) -> None:
    """A root-created lock remains accessible to the archive owner later."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    archive_root = data_root / "minute_archive"
    backup_root = tmp_path / "backups"
    script = _fake_backup_script(tmp_path / "fake-backup.sh")

    snapshot = create_paired_snapshot(script, archive_root, backup_root, 30)

    parent_stat = data_root.stat()
    archive_stat = archive_root.stat()
    lock_stat = (archive_root / ".snapshot.lock").stat()
    assert snapshot.is_dir()
    assert (archive_stat.st_uid, archive_stat.st_gid) == (parent_stat.st_uid, parent_stat.st_gid)
    assert (lock_stat.st_uid, lock_stat.st_gid) == (archive_stat.st_uid, archive_stat.st_gid)
    assert stat.S_IMODE(lock_stat.st_mode) == 0o600


def test_existing_snapshot_lock_owner_and_mode_are_preserved(tmp_path: Path) -> None:
    """A root backup never changes access metadata on an existing lock file."""
    archive_root = tmp_path / "data" / "minute_archive"
    archive_root.mkdir(parents=True)
    lock_path = archive_root / ".snapshot.lock"
    lock_path.write_bytes(b"")
    lock_path.chmod(0o640)
    before = lock_path.stat()
    script = _fake_backup_script(tmp_path / "fake-backup.sh")

    create_paired_snapshot(script, archive_root, tmp_path / "backups", 30)

    after = lock_path.stat()
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert stat.S_IMODE(after.st_mode) == 0o640


def test_failed_database_pair_leaves_no_snapshot_or_staging_directory(tmp_path: Path) -> None:
    """A DB failure cannot publish a directory that looks like a complete pair."""
    archive_root = tmp_path / "data" / "minute_archive"
    archive_root.mkdir(parents=True)
    backup_root = tmp_path / "backups"
    script = _fake_backup_script(tmp_path / "failing-backup.sh", fail=True)

    with pytest.raises(SnapshotError, match="exit code 17"):
        create_paired_snapshot(script, archive_root, backup_root, 30)

    assert not list(backup_root.glob("snapshot_*"))
    assert not list(backup_root.glob(".snapshot-staging-*"))


def test_symlink_in_archive_fails_closed_without_publishing(tmp_path: Path) -> None:
    """The file snapshot refuses symlink entries rather than dereferencing them."""
    archive_root = tmp_path / "data" / "minute_archive"
    archive_root.mkdir(parents=True)
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"outside")
    (archive_root / "escape.parquet").symlink_to(outside)
    backup_root = tmp_path / "backups"
    script = _fake_backup_script(tmp_path / "fake-backup.sh")

    with pytest.raises(SnapshotError, match="symlink"):
        create_paired_snapshot(script, archive_root, backup_root, 30)

    assert not list(backup_root.glob("snapshot_*"))
    assert outside.read_bytes() == b"outside"


def test_symlink_under_excluded_lock_directory_still_fails_closed(tmp_path: Path) -> None:
    """Excluded synchronization files do not bypass source-tree validation."""
    archive_root = tmp_path / "data" / "minute_archive"
    lock_dir = archive_root / ".locks"
    lock_dir.mkdir(parents=True)
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"outside")
    (lock_dir / "escape.lock").symlink_to(outside)
    backup_root = tmp_path / "backups"
    script = _fake_backup_script(tmp_path / "fake-backup.sh")

    with pytest.raises(SnapshotError, match="symlink"):
        create_paired_snapshot(script, archive_root, backup_root, 30)

    assert not list(backup_root.glob("snapshot_*"))


def test_backup_root_inside_archive_is_rejected_before_writing(tmp_path: Path) -> None:
    """Snapshot output must not become another input file in the archive."""
    archive_root = tmp_path / "data" / "minute_archive"
    archive_root.mkdir(parents=True)
    backup_root = archive_root / "backups"
    script = _fake_backup_script(tmp_path / "fake-backup.sh")

    with pytest.raises(SnapshotError, match="must not be inside"):
        create_paired_snapshot(script, archive_root, backup_root, 30)

    assert not backup_root.exists()
    assert not (archive_root / ".snapshot.lock").exists()


def test_retention_removes_only_old_complete_snapshots(tmp_path: Path) -> None:
    """Snapshot retention skips incomplete and unrelated directories."""
    snapshot, _archive_root, backup_root = _create_snapshot(tmp_path)
    old_timestamp = 1_600_000_000
    os.utime(snapshot, (old_timestamp, old_timestamp))
    corrupt = backup_root / "snapshot_20000101T000000Z_aaaaaaaaaaaa"
    shutil.copytree(snapshot, corrupt)
    corrupt_payload = next(corrupt.glob("metadata_*.sql.gz"))
    corrupt_payload.write_bytes(corrupt_payload.read_bytes() + b"changed")
    os.utime(corrupt, (old_timestamp, old_timestamp))
    incomplete = backup_root / "snapshot_20200101T000000Z_aaaaaaaaaaaa"
    incomplete.mkdir()
    (incomplete / "metadata_20200101T000000Z_123.sql.gz").write_bytes(b"partial")
    unrelated = backup_root / "preserve-me"
    unrelated.mkdir()

    deleted, errors = prune_snapshots(
        backup_root,
        30,
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert deleted == 1
    assert errors == ()
    assert not snapshot.exists()
    assert corrupt.is_dir()
    assert incomplete.is_dir()
    assert unrelated.is_dir()
