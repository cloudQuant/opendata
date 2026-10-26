"""Behavioral tests for the bounded host-runtime inventory."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from opendata import host_inventory as inventory

if TYPE_CHECKING:
    from collections.abc import Sequence


def _ok(stdout: str = "") -> inventory.CommandResult:
    """Build a successful fake query response."""
    return inventory.CommandResult("OK", 0, stdout.encode("utf-8"))


class FakeSystemRunner:
    """Return stable systemd and crontab data for behavior tests."""

    def __init__(self, project_root: Path, *, cron: inventory.CommandResult | None = None) -> None:
        self.project_root = project_root
        self.cron = cron or _ok("")
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        timeout: float,
        output_limit: int,
        environment_keys: Sequence[str],
    ) -> inventory.CommandResult:
        del timeout, output_limit, environment_keys
        command = list(argv)
        self.calls.append(command)
        if command[0] == inventory.CRONTAB_EXECUTABLE:
            return self.cron
        if command[2] == "list-units":
            return _ok("opendata-ingest.service loaded inactive dead test service\n")
        if command[2] == "list-unit-files":
            return _ok(
                "opendata-ingest.service disabled enabled\n"
                "opendata-ingest.timer disabled enabled\n"
                "unrelated.service enabled enabled\n"
            )
        if command[2] == "list-timers":
            return _ok("- - - - opendata-ingest.timer opendata-ingest.service\n")
        if command[2] == "show" and command[3] == "opendata-ingest.service":
            return _ok(
                "Id=opendata-ingest.service\n"
                "FragmentPath=/home/user/.config/systemd/user/opendata-ingest.service\n"
                "ActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n"
                f"WorkingDirectory={self.project_root}\n"
                "ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 "
                f"{self.project_root}/scripts/job.py --password=unit-secret ; ignore_errors=no }}\n"
            )
        if command[2] == "show" and command[3] == "opendata-ingest.timer":
            return _ok(
                "Id=opendata-ingest.timer\n"
                "FragmentPath=/home/user/.config/systemd/user/opendata-ingest.timer\n"
                "ActiveState=inactive\nSubState=dead\nUnitFileState=disabled\n"
                "Unit=opendata-ingest.service\nExecStart=\n"
            )
        return _ok(
            "Id=unrelated.service\n"
            "FragmentPath=/usr/lib/systemd/user/unrelated.service\n"
            "ActiveState=active\nSubState=running\nUnitFileState=enabled\n"
            "WorkingDirectory=/tmp\nExecStart={ path=/bin/true ; argv[]=/bin/true }}\n"
        )


def _make_project(root: Path, name: str) -> Path:
    """Create a tiny project tree with one config and one checkpoint candidate."""
    project = root / name
    (project / "config").mkdir(parents=True)
    (project / "config" / "runtime.yaml").write_text("schedule: daily\n", encoding="utf-8")
    (project / ".env").write_text("API_KEY=must-not-be-read-or-reported\n", encoding="utf-8")
    (project / "runtime.checkpoint").write_text("opaque checkpoint payload\n", encoding="utf-8")
    (project / "scripts").mkdir()
    (project / "scripts" / "job.py").write_text("print('job')\n", encoding="utf-8")
    return project


def _proc_fixture(
    proc_root: Path,
    pid: int,
    uid: int,
    argv: bytes,
    *,
    cwd: Path,
    exe: Path,
) -> Path:
    """Create one procfs-shaped fixture without launching a process."""
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    (proc / "status").write_text(
        f"Name:\ttest\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="ascii"
    )
    (proc / "cmdline").write_bytes(argv)
    (proc / "cwd").symlink_to(cwd, target_is_directory=True)
    (proc / "exe").symlink_to(exe)
    return proc


def test_systemd_distinguishes_installed_inactive_timer_and_redacts_exec(
    tmp_path: Path,
) -> None:
    """Inactive installed timers remain visible through their related service."""
    project = _make_project(tmp_path, "opendata")
    runner = FakeSystemRunner(project)
    report = inventory._systemd_inventory({"opendata": project}, runner)
    encoded = json.dumps(report)

    assert report["status"] == "OK"
    assert report["related_unit_count"] == 2
    service = next(row for row in report["records"] if row["unit"].endswith(".service"))
    timer = next(row for row in report["records"] if row["unit"].endswith(".timer"))
    assert service["installed"] is True
    assert service["active"] is False
    assert service["exec"]["script_paths"] == [{"project": "opendata", "path": "scripts/job.py"}]
    assert timer["related_via_service"] == "opendata-ingest.service"
    assert "unit-secret" not in encoded
    assert "/home/user" not in encoded
    assert any(call[2] == "list-timers" and "--all" in call for call in runner.calls)


def test_systemd_failures_remain_unknown(tmp_path: Path) -> None:
    """A failed systemctl query cannot be interpreted as zero installed jobs."""

    def failed_runner(
        argv: Sequence[str], timeout: float, limit: int, env_keys: Sequence[str]
    ) -> inventory.CommandResult:
        del timeout, limit, env_keys
        if argv[2] == "list-unit-files":
            return inventory.CommandResult("UNKNOWN", 1, error_code="permission_denied")
        return _ok("")

    report = inventory._systemd_inventory({"afi": tmp_path}, failed_runner)
    assert report["status"] == "UNKNOWN"
    assert report["installed_units_seen"] == 0
    assert report["errors"]["installed"] == "permission_denied"


def test_crontab_reports_only_hashes_schedule_and_script_path(tmp_path: Path) -> None:
    """Related cron commands are hashed and their literal arguments are omitted."""
    project = _make_project(tmp_path, "afi")
    command = f"20 3 * * 1 /usr/bin/python {project}/scripts/job.py --api-key=cron-secret"
    runner = FakeSystemRunner(project, cron=_ok(f"# ignored\n{command}\n"))
    report = inventory._cron_inventory({"afi": project}, runner)
    encoded = json.dumps(report)

    assert report["status"] == "OK"
    assert report["related_entry_count"] == 1
    record = report["records"][0]
    assert record["schedule"] == "20 3 * * 1"
    assert record["script_paths"] == [{"project": "afi", "path": "scripts/job.py"}]
    assert (
        record["command_sha256"] == hashlib.sha256(command.split(None, 5)[5].encode()).hexdigest()
    )
    assert "cron-secret" not in encoded
    assert command not in encoded


def test_crontab_absence_is_distinct_from_query_failure(tmp_path: Path) -> None:
    """Only the explicit no-crontab response becomes a known empty inventory."""
    no_cron = inventory.CommandResult("UNKNOWN", 1, error_code="no_crontab")
    denied = inventory.CommandResult("UNKNOWN", 1, error_code="permission_denied")
    project = _make_project(tmp_path, "afi")
    assert inventory._cron_inventory({"afi": project}, lambda *args: no_cron)["status"] == "OK"
    denied_report = inventory._cron_inventory({"afi": project}, lambda *args: denied)
    assert denied_report["status"] == "UNKNOWN"
    assert denied_report["related_entry_count"] is None


def test_proc_inventory_matches_same_uid_and_never_serializes_secret_arguments(
    tmp_path: Path,
) -> None:
    """Project script and cwd matches are retained as safe argument shapes only."""
    project = _make_project(tmp_path, "afi")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    uid = os.getuid()
    command = f"/usr/bin/python3\0{project}/scripts/job.py\0--api-key\0proc-secret\0".encode()
    _proc_fixture(
        proc_root,
        121,
        uid,
        command,
        cwd=project,
        exe=Path("/usr/bin/python3"),
    )
    _proc_fixture(
        proc_root,
        122,
        uid + 1,
        b"/usr/bin/python3\0ignored.py\0",
        cwd=project.parent,
        exe=Path("/usr/bin/python3"),
    )
    report = inventory._proc_inventory({"afi": project}, proc_root=proc_root, current_uid=uid)
    encoded = json.dumps(report)

    assert report["status"] == "OK"
    assert report["same_uid_processes_examined"] == 1
    assert report["proc_entries_examined"] == 2
    assert report["related_process_count"] == 1
    assert report["records"][0]["process_id"] == 121
    assert report["records"][0]["command_sha256"] == hashlib.sha256(command).hexdigest()
    assert report["records"][0]["script_paths"] == [{"project": "afi", "path": "scripts/job.py"}]
    assert report["records"][0]["classification"] == "project_related_unclassified"
    assert "proc-secret" not in encoded
    assert "api-key" not in encoded


def test_proc_inventory_marks_owned_test_database_as_non_migration(tmp_path: Path) -> None:
    """The specifically owned isolated MySQL test server is not a migration job."""
    project = _make_project(tmp_path, "opendata")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    uid = os.getuid()
    command = b"/usr/sbin/mysqld\0--datadir=/tmp/od204-owned-mysql-unit/data\0"
    _proc_fixture(
        proc_root,
        123,
        uid,
        command,
        cwd=tmp_path,
        exe=Path("/usr/sbin/mysqld"),
    )
    report = inventory._proc_inventory({"opendata": project}, proc_root=proc_root, current_uid=uid)
    row = report["records"][0]
    assert row["project_roles"] == ["owned_test_infrastructure"]
    assert row["classification"] == "owned_test_infrastructure"
    assert "od204-owned-mysql-unit" not in json.dumps(report)


def test_proc_inventory_reports_unknown_when_process_metadata_is_unreadable(
    tmp_path: Path,
) -> None:
    """An unreadable proc status is counted as partial rather than silently skipped."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    (proc_root / "777").mkdir()
    report = inventory._proc_inventory({"afi": tmp_path}, proc_root=proc_root, current_uid=1)
    assert report["status"] == "PARTIAL"
    assert report["related_process_count"] == 0
    assert report["error_counts"]["process_raced"] == 1


def test_project_walk_hashes_only_allowlisted_config_and_lists_checkpoint_without_reading(
    tmp_path: Path,
) -> None:
    """Config content is represented only by hashes; checkpoint payload stays unread."""
    afi = _make_project(tmp_path, "afi")
    opendata = _make_project(tmp_path, "opendata")
    roots = {"afi": afi, "opendata": opendata}

    configs = inventory._config_inventory(roots)
    checkpoints = inventory._checkpoint_inventory(roots)
    encoded = json.dumps({"configs": configs, "checkpoints": checkpoints})
    expected_hash = hashlib.sha256(b"schedule: daily\n").hexdigest()

    assert configs["status"] == "OK"
    assert any(
        row["project"] == "afi"
        and row["path"] == "config/runtime.yaml"
        and row["sha256"] == expected_hash
        for row in configs["records"]
    )
    assert not any(row["path"] == ".env" for row in configs["records"])
    assert checkpoints["candidate_count"] == 2
    assert "opaque checkpoint payload" not in encoded
    assert "must-not-be-read-or-reported" not in encoded


def test_project_walk_and_inventory_limits_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Traversal caps produce partial evidence, and absent roots do not imply zero."""
    project = _make_project(tmp_path, "afi")
    monkeypatch.setattr(inventory, "MAX_WALK_ENTRIES", 0)
    _, _, capped = inventory._walk_project({"afi": project}, max_entries=1)
    assert capped is True
    report = inventory._checkpoint_inventory({"afi": project})
    assert report["status"] == "PARTIAL"
    absent = inventory._config_inventory({"afi": tmp_path / "missing"})
    assert absent["status"] == "PARTIAL"
    assert absent["error_counts"]["project_root_unavailable"] == 1


def test_command_allowlist_rejects_mutating_or_shell_commands() -> None:
    """Only exact systemd inspection and crontab-list operations are executable."""
    assert inventory._allowed_query_argv([inventory.CRONTAB_EXECUTABLE, "-l"])
    assert inventory._allowed_query_argv(
        [
            inventory.SYSTEMCTL_EXECUTABLE,
            "--user",
            "list-timers",
            "--all",
            "--no-legend",
            "--no-pager",
        ]
    )
    assert not inventory._allowed_query_argv(
        [inventory.SYSTEMCTL_EXECUTABLE, "--user", "stop", "x.service"]
    )
    assert not inventory._allowed_query_argv(["sh", "-c", "systemctl --user list-units"])
    assert (
        inventory._run_bounded_command(
            [inventory.SYSTEMCTL_EXECUTABLE, "--user", "daemon-reload"]
        ).error_code
        == "invalid_command"
    )
    assert not inventory._allowed_query_argv(["crontab", "-l"])
    assert not inventory._allowed_query_argv(["systemctl", "--user", "list-units"])
    assert (
        inventory._run_bounded_command(
            [inventory.SYSTEMCTL_EXECUTABLE, "--user", "list-units"], 0
        ).error_code
        == "invalid_bounds"
    )


def test_query_execution_ignores_a_spoofed_path_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist uses fixed absolute executables even when PATH has a fake crontab."""
    fake_crontab = tmp_path / "crontab"
    fake_crontab.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_crontab.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    captured: dict[str, Any] = {}

    def capture_popen(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["environment"] = kwargs["env"]
        raise FileNotFoundError

    monkeypatch.setattr(inventory.subprocess, "Popen", capture_popen)
    result = inventory._run_bounded_command([inventory.CRONTAB_EXECUTABLE, "-l"])

    assert result.error_code == "command_unavailable"
    assert captured["argv"] == [inventory.CRONTAB_EXECUTABLE, "-l"]
    assert "PATH" not in captured["environment"]


def test_bounded_runner_reports_timeout_output_and_missing_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host command failures use restricted codes and bounded status only."""
    monkeypatch.setattr(inventory, "_allowed_query_argv", lambda argv: True)

    def query(*source: str) -> inventory.CommandResult:
        return inventory._run_bounded_command([sys.executable, "-c", *source], 0.5, 32)

    success = query("print('ok')")
    assert success.status == "OK"
    assert success.stdout == b"ok\n"
    timeout = query("import time; time.sleep(2)")
    assert timeout.error_code == "timeout"
    too_much_output = inventory._run_bounded_command(
        [sys.executable, "-c", "print('x' * 1000)"], 1, 16
    )
    assert too_much_output.error_code == "output_limit"
    denied = query("import sys; sys.stderr.write('Permission denied'); sys.exit(1)")
    assert denied.error_code == "permission_denied"
    missing = query("import sys; sys.stderr.write('command not found'); sys.exit(127)")
    assert missing.error_code == "command_unavailable"

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        raise FileNotFoundError

    monkeypatch.setattr(inventory.subprocess, "Popen", fake_popen)
    assert query("print('x')").error_code == "command_unavailable"
    assert inventory._decode_output(inventory.CommandResult("UNKNOWN", 1), "x")[1] == "x_unknown"
    assert (
        inventory._decode_output(inventory.CommandResult("OK", 0, b"\xff"), "x")[1]
        == "invalid_utf8"
    )


def test_public_summary_and_private_report_permissions(tmp_path: Path) -> None:
    """Published evidence drops process IDs and private receipts receive mode 0600."""
    report = {
        "scope": {"user_id": 1000, "username_sha256": "abc"},
        "processes": {"records": [{"process_id": 456, "command_sha256": "digest"}]},
    }
    summary = inventory.public_summary(report)
    assert "process_id" not in summary["processes"]["records"][0]
    assert "user_id" not in summary["scope"]
    assert summary["processes"]["source_writer_process_count"] is None
    assert "not a data-worker count" in summary["processes"]["classification_note"]
    private_path = tmp_path / "private.json"
    inventory.write_report(private_path, report, private=True)
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert json.loads(private_path.read_text(encoding="utf-8")) == report


def test_build_inventory_rejects_invalid_scope_and_reports_unknown_sources(tmp_path: Path) -> None:
    """Invalid roots/timestamps fail closed while host access errors remain explicit."""
    with pytest.raises(inventory.HostInventoryError, match="project_roots_must_be_distinct"):
        inventory.build_host_inventory(afi_root=tmp_path, opendata_root=tmp_path)
    with pytest.raises(inventory.HostInventoryError, match="timestamp_must_be_aware"):
        inventory.build_host_inventory(
            afi_root=tmp_path / "afi",
            opendata_root=tmp_path / "opendata",
            observed_at=datetime(2026, 1, 1),
        )

    project_a = _make_project(tmp_path, "afi")
    project_b = _make_project(tmp_path, "opendata")

    def unavailable_runner(
        argv: Sequence[str], timeout: float, limit: int, env_keys: Sequence[str]
    ) -> inventory.CommandResult:
        del timeout, limit, env_keys
        if argv[0] == inventory.CRONTAB_EXECUTABLE:
            return inventory.CommandResult("UNKNOWN", 1, error_code="permission_denied")
        return inventory.CommandResult("UNKNOWN", 1, error_code="user_manager_unavailable")

    report = inventory.build_host_inventory(
        afi_root=project_a,
        opendata_root=project_b,
        proc_root=tmp_path / "missing-proc",
        current_uid=1234,
        runner=unavailable_runner,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert report["systemd_user"]["status"] == "UNKNOWN"
    assert report["crontab"]["status"] == "UNKNOWN"
    assert report["processes"]["status"] == "UNKNOWN"
    assert report["scope"]["environment_values_read"] is False
    assert report["scope"]["sensitive_material_files_read"] is False


def test_cli_rejects_receipt_outside_the_designated_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI path guards run before any host query or file creation."""
    result = inventory.main(
        [
            "--afi-root",
            str(tmp_path / "afi"),
            "--opendata-root",
            str(tmp_path / "opendata"),
            "--output",
            str(tmp_path / "unsafe.json"),
            "--public-output",
            str(tmp_path / "docs.json"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "output_path_not_allowed" in captured.err
    assert not (tmp_path / "unsafe.json").exists()


def test_cli_rejects_public_symlink_without_touching_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A symlink at the authorized report path is rejected before collection or writes."""
    project_a = _make_project(tmp_path, "afi")
    project_b = _make_project(tmp_path, "opendata")
    checkout = tmp_path / "checkout"
    (checkout / "opendata").mkdir(parents=True)
    (checkout / "docs").mkdir()
    (checkout / "scripts" / "ops").mkdir(parents=True)
    module_path = checkout / "opendata" / "host_inventory.py"
    module_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(inventory, "__file__", str(module_path))
    receipt_root = tmp_path / "receipts"
    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", receipt_root)
    target = tmp_path / "source.json"
    target.write_text("keep source", encoding="utf-8")
    report_path = checkout / "docs" / "afi204-host-inventory.json"
    report_path.symlink_to(target)
    monkeypatch.setattr(
        inventory,
        "build_host_inventory",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("collection must not run")),
    )

    status = inventory.main(
        [
            "--afi-root",
            str(project_a),
            "--opendata-root",
            str(project_b),
            "--output",
            str(receipt_root / "host-symlink.json"),
            "--public-output",
            str(report_path),
        ]
    )

    assert status == 2
    assert "output_path_symlink_not_allowed" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "keep source"
    assert report_path.is_symlink()
    assert not (receipt_root / "host-symlink.json").exists()


def test_cli_rejects_receipt_that_is_a_hardlink_to_an_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An output alias to a secret input is rejected without reading or replacing it."""
    project_a = _make_project(tmp_path, "afi")
    project_b = _make_project(tmp_path, "opendata")
    checkout = tmp_path / "checkout"
    (checkout / "opendata").mkdir(parents=True)
    (checkout / "docs").mkdir()
    module_path = checkout / "opendata" / "host_inventory.py"
    module_path.write_text("inventory source", encoding="utf-8")
    monkeypatch.setattr(inventory, "__file__", str(module_path))
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", receipt_root)
    env_path = project_a / ".env"
    env_path.write_text("synthetic-test-secret", encoding="utf-8")
    env_stat = env_path.stat(follow_symlinks=False)
    receipt = receipt_root / "host-secret.json"
    os.link(env_path, receipt)

    status = inventory.main(
        [
            "--afi-root",
            str(project_a),
            "--opendata-root",
            str(project_b),
            "--output",
            str(receipt),
            "--public-output",
            str(checkout / "docs" / "afi204-host-inventory.json"),
        ]
    )

    assert status == 2
    assert "output_path_conflicts_with_input" in capsys.readouterr().err
    after_stat = env_path.stat(follow_symlinks=False)
    assert (after_stat.st_dev, after_stat.st_ino, after_stat.st_size) == (
        env_stat.st_dev,
        env_stat.st_ino,
        env_stat.st_size,
    )
    assert os.path.samefile(receipt, env_path)


def test_main_writes_sanitized_outputs_only_to_authorized_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful CLI output contains the full safe receipt and public summary."""
    project_a = _make_project(tmp_path, "afi")
    project_b = _make_project(tmp_path, "opendata")
    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", tmp_path / "receipts", raising=False)
    monkeypatch.setattr(
        inventory, "__file__", str(tmp_path / "checkout" / "opendata" / "host_inventory.py")
    )
    receipt = tmp_path / "receipts" / "host-test.json"
    public_path = tmp_path / "checkout" / "docs" / "afi204-host-inventory.json"
    monkeypatch.setattr(inventory.os, "getuid", lambda: 1)
    monkeypatch.setattr(
        inventory,
        "_proc_inventory",
        lambda roots, *, proc_root, current_uid: {
            "status": "OK",
            "related_process_count": 0,
            "records": [],
            "error_counts": {},
        },
    )

    def runner(
        argv: Sequence[str], timeout: float, limit: int, env_keys: Sequence[str]
    ) -> inventory.CommandResult:
        del timeout, limit, env_keys
        if argv[0] == inventory.CRONTAB_EXECUTABLE:
            return inventory.CommandResult("UNKNOWN", 1, error_code="no_crontab")
        return _ok("")

    monkeypatch.setattr(inventory, "_run_bounded_command", runner)
    status = inventory.main(
        [
            "--afi-root",
            str(project_a),
            "--opendata-root",
            str(project_b),
            "--output",
            str(receipt),
            "--public-output",
            str(public_path),
        ]
    )
    private_report = json.loads(receipt.read_text(encoding="utf-8"))
    public_report = json.loads(public_path.read_text(encoding="utf-8"))

    assert status == 0
    assert private_report["crontab"]["absence"] == "no_user_crontab"
    assert public_report["processes"]["status"] == "OK"
    assert "user_id" not in public_report["scope"]
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_runner_classifies_read_only_query_stderr_and_start_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private stderr is reduced to the allowlisted error vocabulary."""
    monkeypatch.setattr(inventory, "_allowed_query_argv", lambda argv: True)

    def query(message: str, status: int = 1) -> inventory.CommandResult:
        code = f"import sys; sys.stderr.write({message!r}); sys.exit({status})"
        return inventory._run_bounded_command([sys.executable, "-c", code], 1, 256)

    assert query("Access denied").error_code == "permission_denied"
    assert query("DBus unavailable").error_code == "user_manager_unavailable"
    assert query("Failed to connect to user manager").error_code == "user_manager_unavailable"
    assert query("no crontab for user").error_code == "no_crontab"
    assert query("generic failure").error_code == "nonzero_exit"

    def permission_popen(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        raise PermissionError

    monkeypatch.setattr(inventory.subprocess, "Popen", permission_popen)
    assert (
        inventory._run_bounded_command([inventory.CRONTAB_EXECUTABLE, "-l"]).error_code
        == "permission_denied"
    )

    def broken_popen(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        raise OSError

    monkeypatch.setattr(inventory.subprocess, "Popen", broken_popen)
    assert (
        inventory._run_bounded_command([inventory.CRONTAB_EXECUTABLE, "-l"]).error_code
        == "process_start_failed"
    )


def test_runner_handles_missing_pipes_read_errors_and_wait_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipe, read, and reap failures terminate only the query subprocess."""
    monkeypatch.setattr(inventory, "_allowed_query_argv", lambda argv: True)

    class FakeProcess:
        def __init__(self, *, missing_pipe: bool = False, timeout_first: bool = False) -> None:
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = None if missing_pipe else os.fdopen(stdout_read, "rb")
            self.stderr = None if missing_pipe else os.fdopen(stderr_read, "rb")
            if missing_pipe:
                os.close(stdout_read)
                os.close(stderr_read)
            self.pid = 9_999_999
            self.wait_calls = 0
            self.killed = False
            self.timeout_first = timeout_first

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.timeout_first and self.wait_calls == 1:
                raise inventory.subprocess.TimeoutExpired("query", timeout or 1)
            return -9

    fake = FakeProcess(missing_pipe=True)
    monkeypatch.setattr(inventory.subprocess, "Popen", lambda *args, **kwargs: fake)
    result = inventory._run_bounded_command([inventory.CRONTAB_EXECUTABLE, "-l"])
    assert result.error_code == "query_pipe_unavailable"
    assert fake.killed is True

    fake = FakeProcess(timeout_first=True)
    monkeypatch.setattr(inventory.subprocess, "Popen", lambda *args, **kwargs: fake)
    monkeypatch.setattr(inventory.os, "read", lambda *args: (_ for _ in ()).throw(OSError()))
    result = inventory._run_bounded_command([inventory.CRONTAB_EXECUTABLE, "-l"])
    assert result.error_code == "pipe_read_failed"
    assert fake.killed is True


def test_query_allowlist_rejects_malformed_unit_names_and_options() -> None:
    """Allowlist validates exact systemctl show properties and unit syntax."""
    assert not inventory._allowed_query_argv([])
    assert not inventory._allowed_query_argv([inventory.CRONTAB_EXECUTABLE, "-e"])
    assert not inventory._allowed_query_argv([inventory.SYSTEMCTL_EXECUTABLE, "--user", "show"])
    assert not inventory._allowed_query_argv(
        [inventory.SYSTEMCTL_EXECUTABLE, "--user", "list-units", "--all", "--exec-start"]
    )
    assert not inventory._allowed_query_argv(
        [
            inventory.SYSTEMCTL_EXECUTABLE,
            "--user",
            "show",
            "../../unsafe.service",
            "--no-pager",
            inventory._SYSTEMD_SHOW_PROPERTIES,
        ]
    )
    assert inventory._allowed_query_argv(
        [
            inventory.SYSTEMCTL_EXECUTABLE,
            "--user",
            "show",
            "safe.service",
            "--no-pager",
            inventory._SYSTEMD_SHOW_PROPERTIES,
        ]
    )


def test_proc_helpers_and_truncation_keep_unknown_evidence_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proc parsing bounds bytes and distinguishes malformed or raced entries."""
    assert inventory._proc_status_uid(tmp_path / "missing") == (None, "process_raced")
    status = tmp_path / "status"
    status.write_text("Name: test\n", encoding="ascii")
    assert inventory._proc_status_uid(status) == (None, "proc_status_missing_uid")
    status.write_text("Uid:\tbad\n", encoding="ascii")
    assert inventory._proc_status_uid(status) == (None, "proc_status_invalid")

    command_path = tmp_path / "cmdline"
    command_path.write_bytes(b"1234567890abcdefgh")
    monkeypatch.setattr(inventory, "MAX_PROCESS_COMMAND_BYTES", 10)
    command, truncated, error = inventory._read_cmdline(command_path)
    assert command == b"1234567890"
    assert truncated is True
    assert error is None
    assert inventory._read_cmdline(tmp_path / "absent")[2] == "process_raced"


def test_cron_macro_and_unparsed_project_lines_are_summarized(tmp_path: Path) -> None:
    """Macro schedules and malformed project lines never expose command text."""
    project = _make_project(tmp_path, "afi")
    output = (
        f"@reboot /usr/bin/python {project}/scripts/job.py --token macro-secret\n"
        f"broken {project}/scripts/job.py --token short-secret\n"
    )
    report = inventory._cron_inventory({"afi": project}, lambda *args: _ok(output))
    encoded = json.dumps(report)
    assert report["related_entry_count"] == 2
    assert report["records"][0]["schedule"] == "@reboot"
    assert report["records"][1]["schedule"] == "unparsed"
    assert "macro-secret" not in encoded
    assert "short-secret" not in encoded


def test_systemd_candidate_limit_and_show_failure_are_not_reported_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete unit census remains UNKNOWN and records bounded failures."""
    monkeypatch.setattr(inventory, "MAX_UNIT_CANDIDATES", 1)

    def runner(
        argv: Sequence[str], timeout: float, limit: int, env_keys: Sequence[str]
    ) -> inventory.CommandResult:
        del timeout, limit, env_keys
        if argv[2] == "list-unit-files":
            return _ok("one.service enabled enabled\ntwo.timer disabled enabled\n")
        if argv[2] == "show":
            return inventory.CommandResult("UNKNOWN", 1, error_code="permission_denied")
        return _ok("")

    report = inventory._systemd_inventory({"afi": tmp_path}, runner)
    assert report["status"] == "UNKNOWN"
    assert report["installed_units_seen"] == 2
    assert report["errors"]["unit_candidates"] == "unit_candidate_limit"
    assert report["errors"]["show_one.service"] == "permission_denied"


def test_configuration_inventory_scans_bounded_project_config_roots(
    tmp_path: Path,
) -> None:
    """Known deployment/config roots are included while fixtures and env files are skipped."""
    afi = _make_project(tmp_path, "afi")
    opendata = _make_project(tmp_path, "opendata")
    (afi / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (afi / "docker" / "compose").mkdir(parents=True)
    (afi / "docker" / "compose" / "prod.yml").write_text("services: {}\n", encoding="utf-8")
    (afi / "tests").mkdir()
    (afi / "tests" / "ignored.yaml").write_text("fixture: true\n", encoding="utf-8")
    (opendata / "docker").mkdir()
    (opendata / "docker" / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    (opendata / "opendata" / "data" / "mappings").mkdir(parents=True)
    (opendata / "opendata" / "data" / "mappings" / "domain.yaml").write_text(
        "fields: []\n", encoding="utf-8"
    )
    (opendata / "opendata" / "pipeline").mkdir(parents=True)
    (opendata / "opendata" / "pipeline" / "schedules.yaml").write_text(
        "jobs: []\n", encoding="utf-8"
    )
    report = inventory._config_inventory({"afi": afi, "opendata": opendata})
    paths = {(row["project"], row["path"]) for row in report["records"]}

    assert report["status"] == "OK"
    assert ("afi", "pyproject.toml") in paths
    assert ("afi", "docker/compose/prod.yml") in paths
    assert ("opendata", "docker/compose.yml") in paths
    assert ("opendata", "opendata/data/mappings/domain.yaml") in paths
    assert ("opendata", "opendata/pipeline/schedules.yaml") in paths
    assert ("afi", "tests/ignored.yaml") not in paths
    assert ("afi", ".env") not in paths


def test_configuration_inventory_keeps_read_errors_and_caps_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permission, per-file size, and traversal caps cannot silently pass as complete."""
    afi = _make_project(tmp_path, "afi")
    opendata = _make_project(tmp_path, "opendata")
    config_path = afi / "config" / "runtime.yaml"
    original_open = os.open

    def failing_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: Any) -> int:
        if path == config_path:
            raise PermissionError
        return original_open(path, *args)

    monkeypatch.setattr(inventory.os, "open", failing_open)
    permission_report = inventory._config_inventory({"afi": afi, "opendata": opendata})
    assert permission_report["status"] == "PARTIAL"
    assert permission_report["error_counts"]["permission_denied"] == 1

    monkeypatch.setattr(inventory.os, "open", original_open)
    monkeypatch.setattr(inventory, "MAX_CONFIG_FILE_BYTES", 1)
    size_report = inventory._config_inventory({"afi": afi, "opendata": opendata})
    assert size_report["status"] == "PARTIAL"
    assert size_report["error_counts"]["config_size_limit"] > 0

    monkeypatch.setattr(inventory, "MAX_CONFIG_FILE_BYTES", 100)
    monkeypatch.setattr(inventory, "MAX_WALK_ENTRIES", 0)
    capped_report = inventory._config_inventory({"afi": afi, "opendata": opendata})
    assert capped_report["status"] == "PARTIAL"
    assert any(key.endswith("config_scan_limit") for key in capped_report["error_counts"])


def test_proc_root_permission_failure_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Procfs access denial is reported as unknown instead of an empty census."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    original_iterdir = Path.iterdir

    def denied_iterdir(path: Path) -> Any:
        if path == proc_root:
            raise PermissionError
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)
    report = inventory._proc_inventory({"afi": tmp_path}, proc_root=proc_root, current_uid=1)
    assert report["status"] == "UNKNOWN"
    assert report["related_process_count"] is None
    assert report["error_counts"]["permission_denied"] == 1


def test_proc_process_limit_keeps_census_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process enumeration stops at the cap and records that the census is incomplete."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    for pid in (11, 12):
        proc = proc_root / str(pid)
        proc.mkdir()
        (proc / "status").write_text("Uid:\t999\t999\t999\t999\n", encoding="ascii")
    monkeypatch.setattr(inventory, "MAX_PROCESSES", 1)
    report = inventory._proc_inventory({"afi": tmp_path}, proc_root=proc_root, current_uid=1)
    assert report["status"] == "PARTIAL"
    assert report["proc_entries_examined"] == 1
    assert report["error_counts"]["process_limit"] == 1


def test_main_rejects_unapproved_public_output_before_collecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI refuses to write a public report outside the one authorized docs path."""
    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", tmp_path / "receipts")
    receipt = tmp_path / "receipts" / "host-report.json"
    status = inventory.main(
        [
            "--afi-root",
            str(tmp_path / "afi"),
            "--opendata-root",
            str(tmp_path / "opendata"),
            "--output",
            str(receipt),
            "--public-output",
            str(tmp_path / "other.json"),
        ]
    )
    captured = capsys.readouterr()
    assert status == 2
    assert "public_output_path_not_allowed" in captured.err
    assert not receipt.exists()


def test_path_and_systemd_parsers_drop_outside_paths_and_malformed_rows(tmp_path: Path) -> None:
    """Path/unit parsers retain only in-project script paths and valid unit names."""
    project = _make_project(tmp_path, "afi")
    roots = {"afi": project}
    assert inventory._project_label(str(project), roots) == "afi"
    assert inventory._project_label("/tmp/unrelated", roots) is None
    assert inventory._relative_project_path("/tmp/unrelated.py", roots) is None
    assert inventory._relative_project_path("/path/with/\x00bad.py", roots) is None
    extracted = inventory._extract_project_paths(
        f"/usr/bin/python {project}/scripts/job.py {project}/config/runtime.yaml "
        f"/tmp/outside.py {project}/scripts/job.sh",
        roots,
    )
    assert extracted == [
        {"project": "afi", "path": "scripts/job.py"},
        {"project": "afi", "path": "scripts/job.sh"},
    ]
    assert inventory._parse_unit_names("\n description only\n", (".service",)) == []
    assert inventory._parse_unit_names(
        "safe.service active running\nnot a unit .service\n", (".service",)
    ) == ["safe.service"]
    parsed = inventory._parse_properties("good=value\nmalformed\nlong=" + "x" * 131_073)
    assert parsed == {"good": "value"}
    assert inventory._unit_form("job.service") == "regular"
    assert inventory._unit_form("job@.service") == "template"
    assert inventory._unit_form("job@nightly.service") == "instance"
    assert inventory._unit_origin("generated", "") == "generated"
    assert (
        inventory._unit_origin("static", "/run/user/1000/systemd/generator.early/job.service")
        == "generated"
    )
    assert inventory._unit_origin("transient", "") == "transient"
    assert (
        inventory._unit_origin("disabled", "/home/user/.config/systemd/user/job.service")
        == "unit_file"
    )
    assert inventory._unit_origin("", "") == "unknown"


def test_exec_summary_handles_empty_and_malformed_argv_without_leaking_values(
    tmp_path: Path,
) -> None:
    """Invalid ExecStart quoting degrades to a safe summary, never raw text."""
    project = _make_project(tmp_path, "afi")
    roots = {"afi": project}
    empty = inventory._safe_exec_summary("", roots)
    assert empty["executable_basename"] is None
    malformed = inventory._safe_exec_summary(
        f"argv[]=/usr/bin/python {project}/scripts/job.py --token='hidden-value ;",
        roots,
    )
    encoded = json.dumps(malformed)
    assert malformed["executable_basename"] == "python"
    assert malformed["script_paths"] == [{"project": "afi", "path": "scripts/job.py"}]
    assert "hidden-value" not in encoded
    separated_sensitive = inventory._safe_exec_summary(
        f"argv[]=/usr/bin/python {project}/scripts/job.py --token hidden-value ; "
        "ignore_errors=no }}",
        roots,
    )
    assert "redacted_sensitive_value" in separated_sensitive["argument_kinds"]
    many_args = inventory._safe_exec_summary(
        f"argv[]=/usr/bin/python {project}/scripts/job.py "
        + " ".join(f"--arg{i}" for i in range(65)),
        roots,
    )
    assert many_args["arguments_truncated"] is True


def test_checkpoint_candidate_limit_and_stat_failure_remain_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkpoint enumeration caps and transient stat failures never imply completeness."""
    project = tmp_path / "afi"
    project.mkdir()
    for index in range(3):
        (project / f"job-{index}.checkpoint").write_text("x", encoding="ascii")
    monkeypatch.setattr(inventory, "MAX_CHECKPOINT_FILES", 1)
    limited = inventory._checkpoint_inventory({"afi": project})
    assert limited["status"] == "PARTIAL"
    assert limited["candidate_count"] == 1

    monkeypatch.setattr(inventory, "MAX_CHECKPOINT_FILES", 10)
    original_stat = Path.stat

    def failing_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path.name.endswith(".checkpoint"):
            raise PermissionError
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    failed = inventory._checkpoint_inventory({"afi": project})
    assert failed["status"] == "PARTIAL"
    assert failed["error_counts"]["checkpoint_stat_failed"] == 3


def test_config_file_count_and_total_size_bounds_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration hashes stop at both file-count and aggregate-byte caps."""
    afi = _make_project(tmp_path, "afi")
    opendata = _make_project(tmp_path, "opendata")
    (afi / "config" / "second.yaml").write_text("second: true\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "MAX_CONFIG_FILES", 1)
    capped_files = inventory._config_inventory({"afi": afi, "opendata": opendata})
    assert capped_files["status"] == "PARTIAL"
    assert capped_files["error_counts"]["config_file_limit"] == 1

    monkeypatch.setattr(inventory, "MAX_CONFIG_FILES", 100)
    monkeypatch.setattr(inventory, "MAX_TOTAL_CONFIG_BYTES", 1)
    capped_bytes = inventory._config_inventory({"afi": afi, "opendata": opendata})
    assert capped_bytes["status"] == "PARTIAL"
    assert capped_bytes["error_counts"]["config_size_limit"] > 0


@pytest.mark.timeout(2)
def test_config_inventory_skips_fifo_without_blocking(tmp_path: Path) -> None:
    """A FIFO with a config suffix is reported as non-regular and is never opened."""
    project = _make_project(tmp_path, "afi")
    config_path = project / "config" / "runtime.yaml"
    config_path.unlink()
    os.mkfifo(config_path)

    report = inventory._config_inventory({"afi": project})

    assert report["status"] == "PARTIAL"
    assert report["error_counts"]["non_regular_config"] == 1
    assert report["file_count"] == 0


def test_config_inventory_detects_replaced_and_growing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opened inodes and byte limits are rechecked after path enumeration."""
    project = _make_project(tmp_path, "afi")
    config_path = project / "config" / "runtime.yaml"
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("replacement", encoding="ascii")
    original_open = os.open

    def open_replacement(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: Any
    ) -> int:
        actual_path = replacement if Path(path) == config_path else path
        return original_open(actual_path, *args)

    monkeypatch.setattr(inventory.os, "open", open_replacement)
    replaced = inventory._config_inventory({"afi": project})
    assert replaced["status"] == "PARTIAL"
    assert replaced["error_counts"]["config_file_changed"] == 1

    monkeypatch.setattr(inventory.os, "open", original_open)
    monkeypatch.setattr(inventory, "MAX_CONFIG_FILE_BYTES", 5)
    monkeypatch.setattr(inventory, "MAX_TOTAL_CONFIG_BYTES", 100)
    config_path.write_text("12345", encoding="ascii")

    def grow_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *args: Any
    ) -> int:
        if Path(path) == config_path:
            with config_path.open("a", encoding="ascii") as stream:
                stream.write("more than five bytes")
        return original_open(path, *args)

    monkeypatch.setattr(inventory.os, "open", grow_before_open)
    growing = inventory._config_inventory({"afi": project})
    assert growing["status"] == "PARTIAL"
    assert growing["error_counts"]["config_size_limit"] == 1


def test_root_configuration_enumeration_uses_shared_entry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root-level config candidates are truncated by the same directory budget."""
    project = tmp_path / "afi"
    project.mkdir()
    for index in range(20):
        (project / f"runtime-{index}.toml").write_text("[job]\n", encoding="utf-8")
    monkeypatch.setattr(inventory, "MAX_WALK_ENTRIES", 2)

    files, errors = inventory._walk_configuration_files({"afi": project})

    assert len(files) == 2
    assert errors["afi_config_scan_limit"] == 1


def test_configuration_walk_reports_root_scan_failure_and_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root access failures and a non-directory root remain explicit evidence."""
    project = tmp_path / "afi"
    project.mkdir()
    original_scandir = os.scandir

    def denied_scandir(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> Any:
        if Path(path) == project:
            raise PermissionError
        return original_scandir(path)

    monkeypatch.setattr(inventory.os, "scandir", denied_scandir)
    _, denied_errors = inventory._walk_configuration_files({"afi": project})
    assert denied_errors["permission_denied"] == 1

    monkeypatch.setattr(inventory.os, "scandir", original_scandir)
    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("x", encoding="ascii")
    _, invalid_errors = inventory._walk_configuration_files({"afi": non_directory})
    assert invalid_errors["project_root_unavailable"] == 1


def test_configuration_walk_skips_symlinked_files_and_search_directories(
    tmp_path: Path,
) -> None:
    """Config symlinks are not followed and their omission makes the scan partial."""
    project = _make_project(tmp_path, "afi")
    external = tmp_path / "external"
    external.mkdir()
    (external / "runtime.yaml").write_text("marker: do-not-follow", encoding="utf-8")
    (project / "config" / "linked.yaml").symlink_to(external / "runtime.yaml")
    (project / "docker").symlink_to(external, target_is_directory=True)

    report = inventory._config_inventory({"afi": project})

    assert report["status"] == "PARTIAL"
    assert report["error_counts"]["symlink_config_skipped"] == 1
    assert report["error_counts"]["symlink_config_directory_skipped"] == 1
    assert all(row["path"] != "config/linked.yaml" for row in report["records"])


def test_configuration_walk_visits_only_allowlisted_nested_directories(tmp_path: Path) -> None:
    """Nested AFI/OpenData config roots are visited while pruned caches stay out."""
    afi = _make_project(tmp_path, "afi")
    opendata = _make_project(tmp_path, "opendata")
    expected_paths = {
        ("afi", "config/jobs/nightly/job.toml"),
        ("afi", "src/backend/config/settings.yaml"),
        ("afi", "docker/compose.yaml"),
        ("opendata", "opendata/data/feeds/daily.yaml"),
        ("opendata", "opendata/pipeline/weekly/job.toml"),
    }
    fixtures = [
        (afi / "config" / "jobs" / "nightly" / "job.toml", "[job]\n"),
        (afi / "src" / "backend" / "config" / "settings.yaml", "enabled: true\n"),
        (afi / "docker" / "compose.yaml", "services: {}\n"),
        (afi / "config" / "node_modules" / "ignored.yaml", "ignored: true\n"),
        (opendata / "opendata" / "data" / "feeds" / "daily.yaml", "feed: daily\n"),
        (opendata / "opendata" / "pipeline" / "weekly" / "job.toml", "[job]\n"),
    ]
    for path, content in fixtures:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    report = inventory._config_inventory({"afi": afi, "opendata": opendata})
    found_paths = {(row["project"], row["path"]) for row in report["records"]}

    assert report["status"] == "OK"
    assert expected_paths <= found_paths
    assert ("afi", "config/node_modules/ignored.yaml") not in found_paths


def test_public_summary_handles_missing_optional_sections_and_public_write_mode(
    tmp_path: Path,
) -> None:
    """Partial reports serialize consistently and public evidence remains readable."""
    assert inventory.public_summary({}) == {}
    path = tmp_path / "docs" / "inventory.json"
    inventory.write_report(path, {"status": "OK"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "OK"}


def test_project_scan_caps_are_applied_independently_to_both_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large AFI tree cannot prevent OpenData from receiving its own bounded scan."""
    afi = tmp_path / "afi"
    opendata = tmp_path / "opendata"
    (afi / "nested").mkdir(parents=True)
    (opendata / "nested").mkdir(parents=True)
    (afi / "nested" / "job.checkpoint").write_text("x", encoding="ascii")
    (opendata / "nested" / "job.checkpoint").write_text("x", encoding="ascii")
    monkeypatch.setattr(inventory, "MAX_WALK_ENTRIES", 0)
    report = inventory._checkpoint_inventory({"afi": afi, "opendata": opendata})
    assert report["status"] == "PARTIAL"
    assert "afi_scan_limit" in report["error_counts"]
    assert "opendata_scan_limit" in report["error_counts"]


def test_main_rejects_bad_receipt_name_and_safe_root_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid receipt names and invalid project scopes fail before collection."""
    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", tmp_path / "receipts")
    result = inventory.main(
        [
            "--afi-root",
            str(tmp_path / "afi"),
            "--opendata-root",
            str(tmp_path / "opendata"),
            "--output",
            str(tmp_path / "receipts" / "unsafe.txt"),
            "--public-output",
            str(tmp_path / "unused.json"),
        ]
    )
    assert result == 2
    assert "output_filename_not_allowed" in capsys.readouterr().err

    monkeypatch.setattr(inventory, "PRIVATE_RECEIPT_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(
        inventory, "__file__", str(tmp_path / "repo" / "opendata" / "host_inventory.py")
    )
    expected_docs = tmp_path / "repo" / "docs" / "afi204-host-inventory.json"
    safe_receipt = tmp_path / "receipts" / "host-bad-root.json"
    monkeypatch.setattr(
        inventory,
        "build_host_inventory",
        lambda **kwargs: (_ for _ in ()).throw(inventory.HostInventoryError("afi_root_invalid")),
    )
    result = inventory.main(
        [
            "--afi-root",
            str(tmp_path / "afi"),
            "--opendata-root",
            str(tmp_path / "opendata"),
            "--output",
            str(safe_receipt),
            "--public-output",
            str(expected_docs),
        ]
    )
    assert result == 2
    assert "afi_root_invalid" in capsys.readouterr().err
    assert not safe_receipt.exists()


def test_proc_inventory_reports_readlink_errors_and_cwd_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-project working directory can match without a script argument."""
    project = _make_project(tmp_path, "afi")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    proc = _proc_fixture(
        proc_root,
        818,
        os.getuid(),
        b"/usr/bin/python3\0--verbose\0ordinary-value\0",
        cwd=project,
        exe=Path("/usr/bin/python3"),
    )
    original_readlink = os.readlink

    def partial_readlink(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> str:
        if Path(path) == proc / "exe":
            raise PermissionError
        return original_readlink(path, *args, **kwargs)

    monkeypatch.setattr(inventory.os, "readlink", partial_readlink)
    report = inventory._proc_inventory(
        {"afi": project}, proc_root=proc_root, current_uid=os.getuid()
    )
    assert report["status"] == "PARTIAL"
    assert report["related_process_count"] == 1
    assert report["records"][0]["cwd_project"] == "afi"
    assert "redacted_value" in report["records"][0]["argv_kinds"]
    assert report["error_counts"]["permission_denied"] == 1
