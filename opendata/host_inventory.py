"""Collect a bounded, read-only inventory of project-related host jobs.

The collector never reads environment files, unit file bodies, or command output
containing environment values. Process and scheduler commands are summarized in
memory; only hashes, project-relative script paths, and coarse argument shapes
are written to the report. Every OS query is bounded and failures remain
explicitly unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import selectors
import shlex
import signal

# This module executes only fixed read-only query commands through the argv allowlist.
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
MAX_COMMAND_OUTPUT_BYTES = 512_000
MAX_PROCESS_COMMAND_BYTES = 65_536
MAX_PROCESSES = 16_384
MAX_CONFIG_FILES = 400
MAX_CONFIG_FILE_BYTES = 2_097_152
MAX_TOTAL_CONFIG_BYTES = 20_971_520
MAX_CHECKPOINT_FILES = 500
MAX_WALK_ENTRIES = 200_000
MAX_UNIT_CANDIDATES = 256
COMMAND_TIMEOUT_SECONDS = 5.0
_ALLOWED_CONFIG_SUFFIXES = frozenset({".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf"})
_SECRET_NAME_PARTS = (
    "secret",
    "credential",
    "token",
    "private",
    "password",
    "passwd",
    "apikey",
    "api_key",
    "api-key",
    "access_key",
    "access-key",
)
_PRUNED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "htmlcov",
        "coverage",
    }
)
_CHECKPOINT_NAME = re.compile(r"(?:checkpoint|resume|cursor|watermark|offset|progress)", re.I)
_SENSITIVE_ARG = re.compile(r"(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)", re.I)
_SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:@\\-]+\.(?:service|timer)$")
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash", ".js", ".pl", ".rb"})
_OWNED_TEST_MYSQL_PREFIX = "/" + "tmp/od204-owned-mysql-"
_SYSTEMD_SHOW_PROPERTIES = (
    "--property=Id,FragmentPath,ActiveState,SubState,UnitFileState,WorkingDirectory,Unit,ExecStart"
)
PRIVATE_RECEIPT_ROOT = Path(os.sep, "tmp", "od204-receipts")


class HostInventoryError(RuntimeError):
    """An expected bounded host-inventory error with no sensitive detail."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """A subprocess result that retains only bounded stdout and safe status."""

    status: str
    returncode: int | None
    stdout: bytes = b""
    error_code: str | None = None


CommandRunner = Callable[[Sequence[str], float, int, Sequence[str]], CommandResult]


def _digest_bytes(value: bytes) -> str:
    """Return a SHA-256 digest for bytes held only in process memory."""
    return hashlib.sha256(value).hexdigest()


def _allowed_environment(keys: Sequence[str]) -> dict[str, str]:
    """Build the minimal inherited environment needed for read-only host queries."""
    return {key: os.environ[key] for key in keys if key in os.environ}


def _stderr_code(stderr_markers: set[str], returncode: int | None) -> str:
    """Map private stderr observations to a small safe error vocabulary."""
    if "permission" in stderr_markers:
        return "permission_denied"
    if "not_found" in stderr_markers or returncode == 127:
        return "command_unavailable"
    if "dbus" in stderr_markers or "manager" in stderr_markers:
        return "user_manager_unavailable"
    if "no_crontab" in stderr_markers:
        return "no_crontab"
    return "nonzero_exit"


def _run_bounded_command(
    argv: Sequence[str],
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
    output_limit: int = MAX_COMMAND_OUTPUT_BYTES,
    environment_keys: Sequence[str] = (
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ),
) -> CommandResult:
    """Run a fixed-argument query with bounded time/output and no shell."""
    if not _allowed_query_argv(argv):
        return CommandResult("UNKNOWN", None, error_code="invalid_command")
    if timeout_seconds <= 0 or output_limit <= 0:
        return CommandResult("UNKNOWN", None, error_code="invalid_bounds")
    try:
        # S603: argv is restricted to systemctl list/show and crontab -l below.
        process = subprocess.Popen(  # noqa: S603
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # The argv allowlist blocks mutating commands; shell execution stays disabled.
            shell=False,  # nosec B603
            close_fds=True,
            start_new_session=True,
            env=_allowed_environment(environment_keys),
        )
    except FileNotFoundError:
        return CommandResult("UNKNOWN", None, error_code="command_unavailable")
    except PermissionError:
        return CommandResult("UNKNOWN", None, error_code="permission_denied")
    except OSError:
        return CommandResult("UNKNOWN", None, error_code="process_start_failed")

    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        return CommandResult("UNKNOWN", None, error_code="query_pipe_unavailable")
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr_markers: set[str] = set()
    total_bytes = 0
    selector.register(stdout_stream, selectors.EVENT_READ, "stdout")
    selector.register(stderr_stream, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout_seconds
    failure: str | None = None
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.25)):
                try:
                    stream = stdout_stream if key.data == "stdout" else stderr_stream
                    chunk = os.read(stream.fileno(), min(8192, output_limit - total_bytes + 1))
                except OSError:
                    failure = "pipe_read_failed"
                    break
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                total_bytes += len(chunk)
                if total_bytes > output_limit:
                    failure = "output_limit"
                    break
                if key.data == "stdout":
                    stdout.extend(chunk)
                else:
                    lowered = chunk.lower()
                    if b"permission denied" in lowered or b"access denied" in lowered:
                        stderr_markers.add("permission")
                    if b"not found" in lowered or b"no such file" in lowered:
                        stderr_markers.add("not_found")
                    if b"dbus" in lowered:
                        stderr_markers.add("dbus")
                    if b"user manager" in lowered or b"failed to connect" in lowered:
                        stderr_markers.add("manager")
                    if b"no crontab" in lowered:
                        stderr_markers.add("no_crontab")
            if failure is not None:
                break
        if failure is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        try:
            returncode = process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=1.0)
    finally:
        selector.close()
        for stream in (stdout_stream, stderr_stream):
            if not stream.closed:
                stream.close()

    if failure is not None:
        return CommandResult("UNKNOWN", returncode, error_code=failure)
    if returncode != 0:
        return CommandResult(
            "UNKNOWN", returncode, error_code=_stderr_code(stderr_markers, returncode)
        )
    return CommandResult("OK", returncode, bytes(stdout))


def _allowed_query_argv(argv: Sequence[str]) -> bool:
    """Allow only read-only systemd list/show and current-user crontab queries."""
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        return False
    if list(argv) == ["crontab", "-l"]:
        return True
    if len(argv) < 3 or argv[0] != "systemctl" or argv[1] != "--user":
        return False
    operation = argv[2]
    if operation in {"list-units", "list-unit-files", "list-timers"}:
        return all(
            argument
            in {
                "--all",
                "--type=service,timer",
                "--no-legend",
                "--no-pager",
            }
            for argument in argv[3:]
        )
    if operation != "show" or len(argv) != 6:
        return False
    return (
        _SYSTEMD_UNIT_RE.fullmatch(argv[3]) is not None
        and argv[4] == "--no-pager"
        and argv[5] == _SYSTEMD_SHOW_PROPERTIES
    )


def _decode_output(result: CommandResult, label: str) -> tuple[str | None, str | None]:
    """Decode successful UTF-8 query output without exposing failure details."""
    if result.status != "OK":
        return None, result.error_code or f"{label}_unknown"
    try:
        return result.stdout.decode("utf-8", errors="strict"), None
    except UnicodeDecodeError:
        return None, "invalid_utf8"


def _project_label(path_text: str, roots: Mapping[str, Path]) -> str | None:
    """Map an absolute path to a configured repository label without leaking it."""
    try:
        candidate = Path(path_text).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    for label, root in roots.items():
        if candidate.is_relative_to(root):
            return label
    return None


def _relative_project_path(path_text: str, roots: Mapping[str, Path]) -> dict[str, str] | None:
    """Return a project label and relative path for an in-root absolute path."""
    try:
        candidate = Path(path_text).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    for label, root in roots.items():
        if candidate.is_relative_to(root):
            relative = candidate.relative_to(root)
            return {"project": label, "path": relative.as_posix() or "."}
    return None


def _extract_project_paths(text_value: str, roots: Mapping[str, Path]) -> list[dict[str, str]]:
    """Extract only script paths that resolve inside one of the project roots."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    try:
        tokens = shlex.split(text_value, posix=True)
    except ValueError:
        tokens = text_value.split()
    for token in tokens:
        normalized = token.strip("\"'{};,)")
        for root in roots.values():
            root_text = str(root)
            position = normalized.find(root_text)
            if position < 0:
                continue
            path_candidate = normalized[position:]
            if "\\x20" in path_candidate:
                path_candidate = path_candidate.replace("\\x20", " ")
            path_candidate = path_candidate.split(";", maxsplit=1)[0]
            mapped = _relative_project_path(path_candidate, roots)
            if mapped is not None and Path(mapped["path"]).suffix.lower() in _SCRIPT_SUFFIXES:
                found[(mapped["project"], mapped["path"])] = mapped
    return [found[key] for key in sorted(found)]


def _parse_unit_names(text_value: str, suffixes: tuple[str, ...]) -> list[str]:
    """Parse unit names from systemd table output with a strict unit-name bound."""
    names: set[str] = set()
    for line in text_value.splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields:
            if field.endswith(suffixes) and _SYSTEMD_UNIT_RE.fullmatch(field):
                names.add(field)
    return sorted(names)


def _parse_properties(text_value: str) -> dict[str, str]:
    """Parse systemctl show key/value output, retaining values only in memory."""
    properties: dict[str, str] = {}
    for line in text_value.splitlines():
        key, separator, value = line.partition("=")
        if separator and key and len(value) <= 131_072:
            properties[key] = value
    return properties


def _unit_form(unit_name: str) -> str:
    """Classify a systemd service/timer as a regular unit, template, or instance."""
    unit_stem = unit_name.rsplit(".", maxsplit=1)[0]
    if unit_stem.endswith("@"):
        return "template"
    if "@" in unit_stem:
        return "instance"
    return "regular"


def _unit_origin(unit_file_state: str, fragment_path: str) -> str:
    """Distinguish generated/transient units from ordinary unit files."""
    path_parts = Path(fragment_path).parts
    if unit_file_state == "generated" or any(part.startswith("generator") for part in path_parts):
        return "generated"
    if unit_file_state == "transient" or "transient" in path_parts:
        return "transient"
    return "unit_file" if fragment_path else "unknown"


def _safe_exec_summary(exec_value: str, roots: Mapping[str, Path]) -> dict[str, object]:
    """Summarize ExecStart without returning its raw arguments or secrets."""
    command_hash = _digest_bytes(exec_value.encode("utf-8", errors="replace"))
    argv_match = re.search(r"argv\[\]=(.*?)(?:\s+;|\s*\}\s*$|$)", exec_value)
    raw_argv = argv_match.group(1).strip() if argv_match else ""
    try:
        argv = shlex.split(raw_argv, posix=True)
    except ValueError:
        argv = raw_argv.split()
    paths = _extract_project_paths(raw_argv, roots)
    executable = Path(argv[0]).name if argv else None
    kinds: list[str] = []
    prior_sensitive = False
    for argument in argv[1:64]:
        if _extract_project_paths(argument, roots):
            kinds.append("project_script_or_path")
            prior_sensitive = False
        elif argument.startswith("-"):
            option_name = argument.split("=", maxsplit=1)[0]
            kinds.append("sensitive_option" if _SENSITIVE_ARG.search(option_name) else "option")
            prior_sensitive = bool(_SENSITIVE_ARG.search(option_name))
        elif (
            prior_sensitive or "=" in argument and _SENSITIVE_ARG.search(argument.split("=", 1)[0])
        ):
            kinds.append("redacted_sensitive_value")
            prior_sensitive = False
        else:
            kinds.append("redacted_value")
    return {
        "command_sha256": command_hash,
        "executable_basename": executable,
        "argument_count": max(0, len(argv) - 1),
        "argument_kinds": kinds,
        "arguments_truncated": len(argv) > 64,
        "script_paths": paths,
    }


def _systemd_inventory(roots: Mapping[str, Path], runner: CommandRunner) -> dict[str, object]:
    """Inventory related user units, including installed but inactive timers."""
    commands = {
        "loaded": [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ],
        "installed": [
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ],
        "timers": ["systemctl", "--user", "list-timers", "--all", "--no-legend", "--no-pager"],
    }
    outputs: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, argv in commands.items():
        output, error = _decode_output(
            runner(
                argv,
                COMMAND_TIMEOUT_SECONDS,
                MAX_COMMAND_OUTPUT_BYTES,
                ("PATH", "LANG", "LC_ALL", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"),
            ),
            name,
        )
        if error:
            errors[name] = error
        else:
            outputs[name] = output or ""
    loaded_names = _parse_unit_names(outputs.get("loaded", ""), (".service", ".timer"))
    installed_names = _parse_unit_names(outputs.get("installed", ""), (".service", ".timer"))
    timer_names = _parse_unit_names(outputs.get("timers", ""), (".timer",))
    activates = _parse_unit_names(outputs.get("timers", ""), (".service",))
    timer_links: dict[str, str] = {}
    for line in outputs.get("timers", "").splitlines():
        fields = line.split()
        timer = next(
            (
                field
                for field in fields
                if field.endswith(".timer") and _SYSTEMD_UNIT_RE.fullmatch(field)
            ),
            None,
        )
        service = next(
            (
                field
                for field in fields
                if field.endswith(".service") and _SYSTEMD_UNIT_RE.fullmatch(field)
            ),
            None,
        )
        if timer and service:
            timer_links[timer] = service
    unit_names = sorted(set(loaded_names + installed_names + timer_names + activates))
    if len(unit_names) > MAX_UNIT_CANDIDATES:
        errors["unit_candidates"] = "unit_candidate_limit"
        unit_names = unit_names[:MAX_UNIT_CANDIDATES]

    raw_records: list[dict[str, object]] = []
    for unit_name in unit_names:
        argv = [
            "systemctl",
            "--user",
            "show",
            unit_name,
            "--no-pager",
            _SYSTEMD_SHOW_PROPERTIES,
        ]
        output, error = _decode_output(
            runner(
                argv,
                COMMAND_TIMEOUT_SECONDS,
                131_072,
                ("PATH", "LANG", "LC_ALL", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"),
            ),
            f"show_{unit_name}",
        )
        if error:
            errors[f"show_{unit_name}"] = error
            continue
        properties = _parse_properties(output or "")
        related_paths = _extract_project_paths(
            " ".join(
                properties.get(key, "") for key in ("FragmentPath", "WorkingDirectory", "ExecStart")
            ),
            roots,
        )
        fragment_project = _project_label(properties.get("FragmentPath", ""), roots)
        working_project = _project_label(properties.get("WorkingDirectory", ""), roots)
        exec_summary = _safe_exec_summary(properties.get("ExecStart", ""), roots)
        raw_records.append(
            {
                "_related": bool(
                    related_paths or fragment_project is not None or working_project is not None
                ),
                "_unit_target": properties.get("Unit", ""),
                "unit": unit_name,
                "unit_type": unit_name.rsplit(".", maxsplit=1)[-1],
                "unit_form": _unit_form(unit_name),
                "unit_origin": _unit_origin(
                    properties.get("UnitFileState", ""), properties.get("FragmentPath", "")
                ),
                "loaded": unit_name in loaded_names,
                "installed": unit_name in installed_names,
                "active": properties.get("ActiveState") == "active",
                "active_state": properties.get("ActiveState", "UNKNOWN"),
                "sub_state": properties.get("SubState", "UNKNOWN"),
                "installation_state": properties.get("UnitFileState", "UNKNOWN"),
                "appears_in_list_timers": unit_name in timer_names,
                "fragment_project": fragment_project,
                "working_directory_project": working_project,
                "exec": exec_summary,
            }
        )
    related_service_names = {
        str(record["unit"])
        for record in raw_records
        if str(record["unit"]).endswith(".service") and bool(record["_related"])
    }
    records: list[dict[str, object]] = []
    for record in raw_records:
        unit_name = str(record["unit"])
        target = str(record["_unit_target"] or timer_links.get(unit_name, ""))
        inherited_target = target if target in related_service_names else None
        if not bool(record["_related"]) and inherited_target is None:
            continue
        public_record = {key: value for key, value in record.items() if not key.startswith("_")}
        public_record["related_via_service"] = inherited_target
        records.append(public_record)
    return {
        "status": "OK" if not errors else "UNKNOWN",
        "loaded_units_seen": len(loaded_names),
        "installed_units_seen": len(installed_names),
        "timers_seen_by_list_timers_all": len(timer_names),
        "related_unit_count": len(records),
        "active_related_unit_count": sum(bool(item["active"]) for item in records),
        "installed_related_unit_count": sum(bool(item["installed"]) for item in records),
        "timer_service_targets_seen": len(activates),
        "records": records,
        "errors": errors,
    }


def _cron_inventory(roots: Mapping[str, Path], runner: CommandRunner) -> dict[str, object]:
    """Read the current user's crontab without persisting command text."""
    result = runner(
        ["crontab", "-l"],
        COMMAND_TIMEOUT_SECONDS,
        MAX_COMMAND_OUTPUT_BYTES,
        ("PATH", "LANG", "LC_ALL", "HOME", "LOGNAME"),
    )
    output, error = _decode_output(result, "crontab")
    if error:
        if error == "no_crontab":
            return {
                "status": "OK",
                "related_entry_count": 0,
                "records": [],
                "absence": "no_user_crontab",
            }
        return {
            "status": "UNKNOWN",
            "related_entry_count": None,
            "records": [],
            "error_code": error,
        }
    records: list[dict[str, object]] = []
    for line_number, line in enumerate((output or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not any(str(root) in stripped for root in roots.values()):
            continue
        fields = stripped.split()
        if fields and fields[0].startswith("@"):
            schedule = fields[0]
            command = stripped[len(schedule) :].strip()
        elif len(fields) >= 6:
            schedule = " ".join(fields[:5])
            command = stripped.split(None, 5)[5]
        else:
            schedule = "unparsed"
            command = stripped
        paths = _extract_project_paths(command, roots)
        records.append(
            {
                "line_number": line_number,
                "schedule": schedule[:80],
                "line_sha256": _digest_bytes(stripped.encode("utf-8", errors="replace")),
                "command_sha256": _digest_bytes(command.encode("utf-8", errors="replace")),
                "script_paths": paths,
            }
        )
    return {"status": "OK", "related_entry_count": len(records), "records": records}


def _proc_status_uid(status_path: Path) -> tuple[int | None, str | None]:
    """Read only the real UID field from a proc status file."""
    try:
        with status_path.open("rb") as handle:
            for _ in range(128):
                line = handle.readline(4096)
                if not line:
                    return None, "proc_status_missing_uid"
                if line.startswith(b"Uid:"):
                    values = line.split()
                    if len(values) > 1:
                        try:
                            return int(values[1]), None
                        except ValueError:
                            return None, "proc_status_invalid"
                    return None, "proc_status_invalid"
    except PermissionError:
        return None, "permission_denied"
    except FileNotFoundError:
        return None, "process_raced"
    except (OSError, ValueError):
        return None, "proc_status_unavailable"
    return None, "proc_status_missing_uid"


def _read_cmdline(path: Path) -> tuple[bytes | None, bool, str | None]:
    """Read at most the configured command-line cap without decoding secrets."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PROCESS_COMMAND_BYTES + 1)
    except PermissionError:
        return None, False, "permission_denied"
    except FileNotFoundError:
        return None, False, "process_raced"
    except OSError:
        return None, False, "proc_read_failed"
    truncated = len(raw) > MAX_PROCESS_COMMAND_BYTES
    return raw[:MAX_PROCESS_COMMAND_BYTES], truncated, None


def _proc_inventory(
    roots: Mapping[str, Path],
    *,
    proc_root: Path,
    current_uid: int,
) -> dict[str, object]:
    """Find matching same-user processes using only bounded proc metadata."""
    records: list[dict[str, object]] = []
    error_counts: dict[str, int] = {}
    try:
        entries = sorted(
            itertools.islice(
                (entry for entry in proc_root.iterdir() if entry.name.isdecimal()),
                MAX_PROCESSES + 1,
            ),
            key=lambda item: int(item.name),
        )
    except PermissionError:
        return {
            "status": "UNKNOWN",
            "related_process_count": None,
            "records": [],
            "error_counts": {"permission_denied": 1},
        }
    except OSError:
        return {
            "status": "UNKNOWN",
            "related_process_count": None,
            "records": [],
            "error_counts": {"proc_unavailable": 1},
        }
    capped = len(entries) > MAX_PROCESSES
    if capped:
        entries = entries[:MAX_PROCESSES]
        error_counts["process_limit"] = 1
    same_uid_examined = 0
    for entry in entries:
        pid = int(entry.name)
        process_uid, status_error = _proc_status_uid(entry / "status")
        if status_error:
            error_counts[status_error] = error_counts.get(status_error, 0) + 1
            continue
        if process_uid != current_uid:
            continue
        same_uid_examined += 1
        raw, truncated, error = _read_cmdline(entry / "cmdline")
        if error:
            error_counts[error] = error_counts.get(error, 0) + 1
            continue
        if raw is None:
            error_counts["proc_cmdline_missing"] = error_counts.get("proc_cmdline_missing", 0) + 1
            continue
        argv_bytes = [part for part in raw.split(b"\0") if part]
        argv_text = [part.decode("utf-8", errors="replace") for part in argv_bytes[:64]]
        all_arguments = [item.decode("utf-8", errors="replace") for item in argv_bytes]
        cmd_text = "\n".join(all_arguments)
        try:
            cwd_text = os.readlink(entry / "cwd")
        except FileNotFoundError:
            cwd_text = ""
            error_counts["process_raced"] = error_counts.get("process_raced", 0) + 1
        except PermissionError:
            cwd_text = ""
            error_counts["permission_denied"] = error_counts.get("permission_denied", 0) + 1
        except OSError:
            cwd_text = ""
            error_counts["proc_read_failed"] = error_counts.get("proc_read_failed", 0) + 1
        try:
            exe_text = os.readlink(entry / "exe")
        except FileNotFoundError:
            exe_text = ""
            error_counts["process_raced"] = error_counts.get("process_raced", 0) + 1
        except PermissionError:
            exe_text = ""
            error_counts["permission_denied"] = error_counts.get("permission_denied", 0) + 1
        except OSError:
            exe_text = ""
            error_counts["proc_read_failed"] = error_counts.get("proc_read_failed", 0) + 1
        cwd_project = _project_label(cwd_text, roots) if cwd_text else None
        exe_project = _project_label(exe_text, roots) if exe_text else None
        script_paths = _extract_project_paths(cmd_text, roots)
        owned_test_mysql = _OWNED_TEST_MYSQL_PREFIX in cmd_text
        if not (cwd_project or exe_project or script_paths or owned_test_mysql):
            continue
        argv_kinds: list[str] = []
        sensitive_next = False
        for argument in argv_text[1:64]:
            if _extract_project_paths(argument, roots):
                argv_kinds.append("project_path")
                sensitive_next = False
            elif argument.startswith("-"):
                option = argument.split("=", maxsplit=1)[0]
                argv_kinds.append("sensitive_option" if _SENSITIVE_ARG.search(option) else "option")
                sensitive_next = bool(_SENSITIVE_ARG.search(option))
            elif (
                sensitive_next
                or "=" in argument
                and _SENSITIVE_ARG.search(argument.split("=", 1)[0])
            ):
                argv_kinds.append("redacted_sensitive_value")
                sensitive_next = False
            else:
                argv_kinds.append("redacted_value")
        records.append(
            {
                "process_id": pid,
                "project_roles": [
                    label for label in (cwd_project, exe_project) if label is not None
                ]
                + (["owned_test_infrastructure"] if owned_test_mysql else []),
                "cwd_project": cwd_project,
                "executable_basename": Path(exe_text).name if exe_text else None,
                "argv0_basename": Path(argv_text[0]).name if argv_text else None,
                "argv_count": len(argv_bytes),
                "argv_kinds": argv_kinds,
                "argv_truncated": truncated or len(argv_bytes) > 64,
                "command_sha256": _digest_bytes(raw),
                "command_hash_is_prefix": truncated,
                "script_paths": script_paths,
                "classification": (
                    "owned_test_infrastructure"
                    if owned_test_mysql
                    else "project_related_unclassified"
                ),
            }
        )
    status = "OK" if not error_counts else "PARTIAL"
    return {
        "status": status,
        "proc_entries_examined": len(entries),
        "same_uid_processes_examined": same_uid_examined,
        "related_process_count": len(records),
        "records": records,
        "error_counts": error_counts,
    }


def _walk_project(
    roots: Mapping[str, Path],
    *,
    max_entries: int,
) -> tuple[list[tuple[str, Path, Path]], dict[str, int], bool]:
    """Walk configured project trees without following symlinks or entering build caches."""
    files: list[tuple[str, Path, Path]] = []
    errors: dict[str, int] = {}
    capped = False
    for label, root in roots.items():
        visited = 0
        root_capped = False
        if not root.is_dir():
            errors["project_root_unavailable"] = errors.get("project_root_unavailable", 0) + 1
            continue

        def record_walk_error(error: OSError) -> None:
            """Count inaccessible directories without persisting OS error text."""
            del error
            errors["directory_read_failed"] = errors.get("directory_read_failed", 0) + 1

        for current, directory_names, file_names in os.walk(
            root, followlinks=False, onerror=record_walk_error
        ):
            base = Path(current)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name not in _PRUNED_DIR_NAMES and not os.path.islink(base / name)
            )
            visited += len(directory_names)
            if visited > max_entries:
                capped = True
                root_capped = True
                errors[f"{label}_scan_limit"] = 1
                break
            for file_name in sorted(file_names):
                visited += 1
                if visited > max_entries:
                    capped = True
                    root_capped = True
                    errors[f"{label}_scan_limit"] = 1
                    break
                path = base / file_name
                if os.path.islink(path):
                    continue
                files.append((label, root, path))
            if root_capped:
                break
            if visited > max_entries:
                capped = True
                root_capped = True
                errors[f"{label}_scan_limit"] = 1
                break
    return files, errors, capped


def _checkpoint_inventory(roots: Mapping[str, Path]) -> dict[str, object]:
    """List checkpoint-like paths and metadata without reading file contents."""
    files, errors, capped = _walk_project(roots, max_entries=MAX_WALK_ENTRIES)
    candidates: list[dict[str, object]] = []
    for label, root, path in files:
        if not _CHECKPOINT_NAME.search(path.name):
            continue
        try:
            stat_result = path.stat(follow_symlinks=False)
        except OSError:
            errors["checkpoint_stat_failed"] = errors.get("checkpoint_stat_failed", 0) + 1
            continue
        if not path.is_file():
            continue
        candidates.append(
            {
                "project": label,
                "path": path.relative_to(root).as_posix(),
                "size_bytes": stat_result.st_size,
                "modified_utc": datetime.fromtimestamp(
                    stat_result.st_mtime, timezone.utc
                ).isoformat(),
                "candidate_reason": "checkpoint_name",
            }
        )
        if len(candidates) >= MAX_CHECKPOINT_FILES:
            capped = True
            break
    if capped:
        errors["scan_limit"] = 1
    return {
        "status": "PARTIAL" if errors else "OK",
        "candidate_count": len(candidates),
        "records": candidates,
        "error_counts": errors,
    }


def _config_allowed(label: str, root: Path, path: Path) -> bool:
    """Select project-owned operational configuration files, excluding secret names."""
    relative = path.relative_to(root)
    lower_name = path.name.lower()
    if lower_name.startswith(".env") or any(part in lower_name for part in _SECRET_NAME_PARTS):
        return False
    if path.suffix.lower() not in _ALLOWED_CONFIG_SUFFIXES:
        return False
    parts = relative.parts
    if not parts:
        return False
    if label == "afi":
        allowed = (
            len(parts) == 1
            or parts[0] in {"config", "docker"}
            or parts[:3] == ("src", "backend", "config")
        )
    else:
        allowed = len(parts) == 1 or parts[0] in {"docker", "opendata"}
        if parts[:2] == ("opendata", "data"):
            allowed = len(parts) <= 4
        if parts[:2] == ("opendata", "pipeline"):
            allowed = len(parts) <= 4
    return allowed


def _walk_configuration_files(
    roots: Mapping[str, Path],
) -> tuple[list[tuple[str, Path, Path]], dict[str, int]]:
    """Walk only known configuration directories and root-level config files."""
    files: list[tuple[str, Path, Path]] = []
    errors: dict[str, int] = {}
    directory_map = {
        "afi": ("config", "docker", "src/backend/config"),
        "opendata": ("docker", "opendata/data", "opendata/pipeline"),
    }
    for label, root in roots.items():
        if not root.is_dir():
            errors["project_root_unavailable"] = errors.get("project_root_unavailable", 0) + 1
            continue
        try:
            files.extend(
                (label, root, path)
                for path in root.iterdir()
                if not os.path.islink(path)
                and path.is_file()
                and _config_allowed(label, root, path)
            )
        except PermissionError:
            errors["permission_denied"] = errors.get("permission_denied", 0) + 1
            continue
        except OSError:
            errors["root_config_scan_failed"] = errors.get("root_config_scan_failed", 0) + 1
            continue
        visited = 0
        for relative_directory in directory_map[label]:
            start = root / relative_directory
            if not start.is_dir() or os.path.islink(start):
                continue

            def record_walk_error(error: OSError) -> None:
                """Count inaccessible configuration paths without their OS text."""
                del error
                errors["directory_read_failed"] = errors.get("directory_read_failed", 0) + 1

            for current, directory_names, file_names in os.walk(
                start, followlinks=False, onerror=record_walk_error
            ):
                base = Path(current)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if name not in _PRUNED_DIR_NAMES and not os.path.islink(base / name)
                )
                visited += len(directory_names)
                if visited > MAX_WALK_ENTRIES:
                    errors[f"{label}_config_scan_limit"] = 1
                    break
                for file_name in sorted(file_names):
                    visited += 1
                    if visited > MAX_WALK_ENTRIES:
                        errors[f"{label}_config_scan_limit"] = 1
                        break
                    path = base / file_name
                    if not os.path.islink(path) and _config_allowed(label, root, path):
                        files.append((label, root, path))
                if visited > MAX_WALK_ENTRIES:
                    break
    return files, errors


def _config_inventory(roots: Mapping[str, Path]) -> dict[str, object]:
    """Hash selected non-secret configuration files without returning their contents."""
    files, errors = _walk_configuration_files(roots)
    records: list[dict[str, object]] = []
    total_bytes = 0
    for label, root, path in files:
        if not _config_allowed(label, root, path):
            continue
        if len(records) >= MAX_CONFIG_FILES:
            errors["config_file_limit"] = 1
            break
        try:
            size = path.stat(follow_symlinks=False).st_size
            if size > MAX_CONFIG_FILE_BYTES or total_bytes + size > MAX_TOTAL_CONFIG_BYTES:
                errors["config_size_limit"] = errors.get("config_size_limit", 0) + 1
                continue
            content = path.read_bytes()
        except PermissionError:
            errors["permission_denied"] = errors.get("permission_denied", 0) + 1
            continue
        except OSError:
            errors["config_read_failed"] = errors.get("config_read_failed", 0) + 1
            continue
        total_bytes += len(content)
        records.append(
            {
                "project": label,
                "path": path.relative_to(root).as_posix(),
                "size_bytes": len(content),
                "sha256": _digest_bytes(content),
            }
        )
        del content
    return {
        "status": "PARTIAL" if errors else "OK",
        "file_count": len(records),
        "total_bytes_hashed": total_bytes,
        "records": records,
        "error_counts": errors,
    }


def _root_summary(roots: Mapping[str, Path]) -> dict[str, object]:
    """Report project root availability without publishing host-specific paths."""
    return {
        label: {
            "available": root.is_dir(),
            "root_path_sha256": _digest_bytes(str(root).encode("utf-8")),
        }
        for label, root in roots.items()
    }


def _resolve_project_root(label: str, path: Path) -> Path:
    """Resolve one explicit project root or raise a safe configuration code."""
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HostInventoryError(f"{label}_root_invalid") from exc


def build_host_inventory(
    *,
    afi_root: Path,
    opendata_root: Path,
    proc_root: Path = Path("/proc"),
    current_uid: int | None = None,
    runner: CommandRunner | None = None,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Build a sanitized host inventory for the two explicit project roots.

    Args:
        afi_root: The AFI repository root to match; only this tree is walked.
        opendata_root: The OpenData repository root to match and walk.
        proc_root: Procfs root, replaceable by an isolated test fixture.
        current_uid: User ID whose processes are inspected; defaults to the caller.
        runner: Bounded read-only subprocess runner, injectable in tests.
        observed_at: Optional aware timestamp for deterministic tests.

    Returns:
        A JSON-compatible report with explicit UNKNOWN/PARTIAL states and no raw
        unit text, command arguments, environment values, or file contents.
    """
    roots = {
        "afi": _resolve_project_root("afi", afi_root),
        "opendata": _resolve_project_root("opendata", opendata_root),
    }
    if roots["afi"] == roots["opendata"]:
        raise HostInventoryError("project_roots_must_be_distinct")
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise HostInventoryError("observation_timestamp_must_be_aware")
    uid = os.getuid() if current_uid is None else current_uid
    query_runner = runner or _run_bounded_command
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at_utc": now.astimezone(timezone.utc).isoformat(),
        "scope": {
            "user_id": uid,
            "project_roots": _root_summary(roots),
            "filesystem_scope": "explicit_project_roots_and_same_uid_procfs_only",
            "environment_values_read": False,
            "sensitive_material_files_read": False,
            "command_text_persisted": False,
        },
        "systemd_user": _systemd_inventory(roots, query_runner),
        "crontab": _cron_inventory(roots, query_runner),
        "processes": _proc_inventory(roots, proc_root=proc_root, current_uid=uid),
        "checkpoint_candidates": _checkpoint_inventory(roots),
        "configuration_hashes": _config_inventory(roots),
    }


def public_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Remove host-local process identifiers from a sanitized full report."""
    summary: dict[str, Any] = json.loads(json.dumps(report, sort_keys=True))
    process_section = summary.get("processes")
    if isinstance(process_section, dict):
        for record in process_section.get("records", []):
            if isinstance(record, dict):
                record.pop("process_id", None)
        process_section["source_writer_process_count"] = None
        process_section["classification_note"] = (
            "Project-associated process candidates may include development, test, and agent "
            "tools; this count is not a data-worker count. Cwd or project-path association alone "
            "does not prove a source writer. No process-to-script/service binding was established."
        )
    scope = summary.get("scope")
    if isinstance(scope, dict):
        scope.pop("user_id", None)
    return summary


def write_report(path: Path, report: Mapping[str, Any], *, private: bool = False) -> None:
    """Write a JSON report atomically to a caller-selected evidence path."""
    payload = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    if private:
        temporary_path.chmod(0o600)
    temporary_path.replace(path)
    if private:
        path.chmod(0o600)
    else:
        path.chmod(0o644)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse explicit roots and evidence destinations for the CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--afi-root", type=Path, required=True)
    parser.add_argument("--opendata-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="private sanitized receipt under /tmp/od204-receipts",
    )
    parser.add_argument(
        "--public-output",
        type=Path,
        required=True,
        help="sanitized summary under this checkout's docs",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only inventory and write private/public sanitized reports."""
    args = _parse_args(argv)
    expected_private_root = PRIVATE_RECEIPT_ROOT.resolve()
    try:
        private_path = args.output.resolve(strict=False)
        private_path.relative_to(expected_private_root)
    except ValueError:
        print("host_inventory: output_path_not_allowed", file=sys.stderr)
        return 2
    if not private_path.name.startswith("host-") or private_path.suffix != ".json":
        print("host_inventory: output_filename_not_allowed", file=sys.stderr)
        return 2
    try:
        public_path = args.public_output.resolve(strict=False)
        repo_root = Path(__file__).resolve().parents[1]
        expected_public = repo_root / "docs" / "afi204-host-inventory.json"
        if public_path != expected_public.resolve(strict=False):
            print("host_inventory: public_output_path_not_allowed", file=sys.stderr)
            return 2
        report = build_host_inventory(afi_root=args.afi_root, opendata_root=args.opendata_root)
        write_report(private_path, report, private=True)
        write_report(public_path, public_summary(report))
    except HostInventoryError as exc:
        print(f"host_inventory: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("host_inventory: output_or_scope_io_failed", file=sys.stderr)
        return 2
    systemd = report.get("systemd_user")
    cron = report.get("crontab")
    processes = report.get("processes")
    summary: dict[str, object] = {
        "receipt": str(private_path),
        "public_report": str(public_path),
        "systemd_status": systemd.get("status", "UNKNOWN")
        if isinstance(systemd, Mapping)
        else "UNKNOWN",
        "crontab_status": cron.get("status", "UNKNOWN") if isinstance(cron, Mapping) else "UNKNOWN",
        "process_status": processes.get("status", "UNKNOWN")
        if isinstance(processes, Mapping)
        else "UNKNOWN",
        "related_processes": processes.get("related_process_count")
        if isinstance(processes, Mapping)
        else None,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
