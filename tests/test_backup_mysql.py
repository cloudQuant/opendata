"""Runtime tests for the logical backup script using a fake mysqldump."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY = Path(__file__).resolve().parents[1]


def _fake_project(root: Path) -> tuple[Path, Path, Path]:
    """Copy the operational script and install a dump executable recorder."""
    script_dir = root / "scripts" / "ops"
    script_dir.mkdir(parents=True)
    script = script_dir / "backup_mysql.sh"
    shutil.copy2(REPOSITORY / "scripts" / "ops" / "backup_mysql.sh", script)
    shutil.copy2(
        REPOSITORY / "scripts" / "ops" / "backup_minute_snapshot.py",
        script_dir / "backup_minute_snapshot.py",
    )

    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    fake_dump = fake_bin / "mysqldump"
    fake_dump.write_text(
        """#!/bin/bash
set -eu
defaults_file=
database=
for argument in "$@"; do
  case "$argument" in
    --defaults-extra-file=*) defaults_file="${argument#*=}" ;;
  esac
  database="$argument"
done
printf '%s\\n' "$database" >> "$FAKE_CALLS"
printf '%s\\n' "$*" >> "$FAKE_ARGS"
cat "$defaults_file" >> "$FAKE_DEFAULTS"
printf '\\n--next-connection--\\n' >> "$FAKE_DEFAULTS"
printf 'CREATE TABLE sample (id INT);\\n'
if [[ "${FAKE_FAIL_DATABASE:-}" == "$database" ]]; then
  exit 7
fi
""",
        encoding="utf-8",
    )
    fake_dump.chmod(0o755)
    return script, fake_bin, root / "backups"


def _environment(
    root: Path,
    fake_bin: Path,
    backup_dir: Path,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return isolated backup environment values for the fake project."""
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_RETENTION_DAYS": "30",
            "FAKE_CALLS": str(root / "calls.txt"),
            "FAKE_ARGS": str(root / "args.txt"),
            "FAKE_DEFAULTS": str(root / "defaults.txt"),
        }
    )
    for name in (
        "BACKUP_ENV_FILE",
        "BACKUP_MINUTE_ARCHIVE_DIR",
        "BACKUP_PYTHON",
        "BACKUP_RETENTION_DAYS",
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
        "DATA_MYSQL_HOST",
        "DATA_MYSQL_PORT",
        "DATA_MYSQL_USER",
        "DATA_MYSQL_PASSWORD",
        "DATA_MYSQL_DATABASE",
        "FAKE_FAIL_DATABASE",
    ):
        env.pop(name, None)
    env["BACKUP_RETENTION_DAYS"] = "30"
    if extra:
        env.update(extra)
    return env


def _run_backup(script: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the script with the host Bash 3.2 runtime and capture safe output."""
    # S603 is intentional: this runs the repository-owned script from a tmp_path project.
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(script)],
        env=dict(env),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


class TestBackupMySQL:
    """Each test uses temporary paths and fake database tooling only."""

    def test_dotenv_fills_missing_values_but_environment_wins_and_credentials_are_split(
        self,
        tmp_path: Path,
    ) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        (tmp_path / ".env").write_text(
            "MYSQL_USER=dotenv_metadata\n"
            "MYSQL_PASSWORD=dotenv-metadata-secret\n"
            "MYSQL_HOST=metadata-db\n"
            "MYSQL_DATABASE=metadata_db\n"
            "DATA_MYSQL_USER=warehouse_user\n"
            'DATA_MYSQL_PASSWORD="warehouse#secret"\n'
            "DATA_MYSQL_HOST=warehouse-db\n"
            "DATA_MYSQL_DATABASE=warehouse_db\n",
            encoding="utf-8",
        )
        main_user = "explicit_metadata_user"
        main_password = 'main"password\\tail'
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "MYSQL_USER": main_user,
                "MYSQL_PASSWORD": main_password,
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 0, result.stderr
        assert main_password not in result.stdout + result.stderr
        assert main_password not in (tmp_path / "args.txt").read_text(encoding="utf-8")
        defaults = (tmp_path / "defaults.txt").read_text(encoding="utf-8")
        escaped_main_password = main_password.replace("\\", "\\\\").replace('"', '\\"')
        assert f'user="{main_user}"' in defaults
        assert f'password="{escaped_main_password}"' in defaults
        assert 'host="metadata-db"' in defaults
        assert 'user="warehouse_user"' in defaults
        assert 'password="warehouse#secret"' in defaults
        assert 'host="warehouse-db"' in defaults
        assert (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines() == [
            "metadata_db",
            "warehouse_db",
        ]
        backups = sorted(backup_dir.glob("*.sql.gz"))
        assert [path.name.split("_", maxsplit=1)[0] for path in backups] == [
            "metadata",
            "warehouse",
        ]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)
        gzip_binary = shutil.which("gzip")
        assert gzip_binary is not None
        for artifact in backups:
            checked = subprocess.run(  # noqa: S603
                [gzip_binary, "-t", str(artifact)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert checked.returncode == 0

    def test_missing_default_env_and_explicit_empty_env_file_are_safe(
        self,
        tmp_path: Path,
    ) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        (tmp_path / ".env").write_text(
            "MYSQL_PASSWORD=must-not-be-read\n",
            encoding="utf-8",
        )
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "explicit_metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "explicit_warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 0, result.stderr
        assert "must-not-be-read" not in (tmp_path / "defaults.txt").read_text(encoding="utf-8")
        assert "explicit_metadata_password" not in result.stdout + result.stderr
        assert len(list(backup_dir.glob("*.sql.gz"))) == 2

    def test_missing_implicit_dotenv_keeps_exported_environment_values(
        self, tmp_path: Path
    ) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 0, result.stderr
        assert len(list(backup_dir.glob("*.sql.gz"))) == 2

    def test_failed_warehouse_dump_publishes_no_partial_backup_pair(self, tmp_path: Path) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        private_tmp = tmp_path / "private-tmp"
        private_tmp.mkdir()
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "TMPDIR": str(private_tmp),
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
                "FAKE_FAIL_DATABASE": "opendata_data",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 2
        assert "mysqldump failed for warehouse database" in result.stderr
        assert list(backup_dir.glob("*.sql.gz")) == []
        assert list(backup_dir.iterdir()) == []
        assert list(private_tmp.iterdir()) == []

    def test_pre_dump_temp_failure_cleans_credentials_with_empty_arrays_under_bash_32(
        self,
        tmp_path: Path,
    ) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        private_tmp = tmp_path / "private-tmp"
        private_tmp.mkdir()
        real_mktemp = shutil.which("mktemp")
        assert real_mktemp is not None
        fake_mktemp = fake_bin / "mktemp"
        fake_mktemp.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "$BACKUP_DIR"/.metadata_* ]]; then exit 44; fi\n'
            'exec "$REAL_MKTEMP" "$@"\n',
            encoding="utf-8",
        )
        fake_mktemp.chmod(0o755)
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "TMPDIR": str(private_tmp),
                "REAL_MKTEMP": real_mktemp,
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 44
        assert "unbound variable" not in result.stderr
        assert list(private_tmp.iterdir()) == []
        assert list(backup_dir.iterdir()) == []

    def test_retention_must_be_positive_before_output_directory_creation(
        self, tmp_path: Path
    ) -> None:
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "BACKUP_RETENTION_DAYS": "0",
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 1
        assert "must be a positive whole number" in result.stderr
        assert not backup_dir.exists()

    def test_paired_snapshot_publishes_verified_database_and_minute_payloads(
        self, tmp_path: Path
    ) -> None:
        """The shell trigger publishes both DB dumps and archive files together."""
        import gzip
        import json
        import tarfile

        script, fake_bin, backup_dir = _fake_project(tmp_path)
        archive_root = tmp_path / "data" / "minute_archive"
        source = archive_root / "stock_daily" / "AAPL" / "2026" / "06" / "sample.parquet"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"verified-minute-parquet-payload")
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "BACKUP_MINUTE_ARCHIVE_DIR": str(archive_root),
                "BACKUP_PYTHON": sys.executable,
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 0, result.stderr
        snapshots = list(backup_dir.glob("snapshot_*"))
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert {path.name for path in snapshot.iterdir()} == {
            "manifest.json",
            "minute_archive.tar.gz",
            next(path.name for path in snapshot.glob("metadata_*.sql.gz")),
            next(path.name for path in snapshot.glob("warehouse_*.sql.gz")),
        }
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["format"] == "opendata-paired-backup-v1"
        assert [entry["path"] for entry in manifest["minute_archive_files"]] == [
            "stock_daily/AAPL/2026/06/sample.parquet"
        ]
        with tarfile.open(snapshot / "minute_archive.tar.gz", "r:gz") as archive:
            members = archive.getmembers()
            assert [member.name for member in members] == [
                "stock_daily/AAPL/2026/06/sample.parquet"
            ]
            extracted = archive.extractfile(members[0])
            assert extracted is not None
            assert extracted.read() == source.read_bytes()
        for name in manifest["payloads"]:
            payload = snapshot / name
            assert manifest["payloads"][name]["size"] == payload.stat().st_size
            assert manifest["payloads"][name]["sha256"] == sha256(payload.read_bytes()).hexdigest()
        for label in ("metadata", "warehouse"):
            dumps = list(snapshot.glob(f"{label}_*.sql.gz"))
            assert len(dumps) == 1
            with gzip.open(dumps[0], "rb") as dump:
                assert b"CREATE TABLE sample" in dump.read()
        assert not any(".snapshot.lock" in member.name for member in members)
        assert stat.S_IMODE((archive_root / ".snapshot.lock").stat().st_mode) == 0o600

        env["FAKE_FAIL_DATABASE"] = "opendata_data"
        failed = _run_backup(script, env)
        assert failed.returncode == 2
        assert len(list(backup_dir.glob("snapshot_*"))) == 1
        assert not list(backup_dir.glob(".snapshot-staging-*"))

    def test_db_only_retention_does_not_edit_paired_snapshot_payloads(self, tmp_path: Path) -> None:
        """Legacy retention remains top-level and cannot invalidate paired backups."""
        script, fake_bin, backup_dir = _fake_project(tmp_path)
        paired = backup_dir / "snapshot_20260930T120000Z_012345abcdef"
        paired.mkdir(parents=True)
        nested_dump = paired / "metadata_20200101T000000Z_123.sql.gz"
        nested_dump.write_bytes(b"part of a paired snapshot")
        stale_top_level = backup_dir / "metadata_20200101T000000Z_123.sql.gz"
        stale_top_level.write_bytes(b"legacy database-only dump")
        old_time = time.time() - 90 * 86400
        os.utime(nested_dump, (old_time, old_time))
        os.utime(stale_top_level, (old_time, old_time))
        env = _environment(
            tmp_path,
            fake_bin,
            backup_dir,
            {
                "BACKUP_ENV_FILE": "",
                "MYSQL_USER": "metadata_user",
                "MYSQL_PASSWORD": "metadata_password",
                "DATA_MYSQL_USER": "warehouse_user",
                "DATA_MYSQL_PASSWORD": "warehouse_password",
            },
        )

        result = _run_backup(script, env)

        assert result.returncode == 0, result.stderr
        assert nested_dump.read_bytes() == b"part of a paired snapshot"
        assert not stale_top_level.exists()
