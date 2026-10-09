"""Validate the local, read-only evidence bundle for the C65 runtime stack."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil

# Used only for the fixed, shell-free read-only Git inventory below.
import subprocess  # nosec B404  # one run(): which(git) ls-files -z, no shell
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Collection

from scripts.ops.verify_isolated_stack import (
    APP_CONTAINER,
    APP_IMAGE_TAG,
    DB_SCHEMAS,
    ENDPOINT,
    VerificationError,
    validate_docker_inspect,
)

EVIDENCE_DIR = Path("docs/evidence/C65")
BUILD_SOURCE = EVIDENCE_DIR / "runtime-build-source.json"
BUILD_LOG = EVIDENCE_DIR / "runtime-build-current.txt"
START_RUN = EVIDENCE_DIR / "runtime-start-after-asyncio-fix.json"
CONTAINER_INSPECT = EVIDENCE_DIR / "runtime-container-inspect.json"
STACK_REPORT = EVIDENCE_DIR / "runtime-stack-current.json"
DRIVER_LOG = EVIDENCE_DIR / "runtime-stack-current-independent.txt"
VERIFIER_SCRIPT = Path("scripts/ops/verify_isolated_stack.py")
EXPECTED_PROJECTION = (
    "current Docker COPY files from managed/nonignored set, excluding credential and cache paths"
)
EXPECTED_BUILD_TASK = "opendata-c65"
MAX_EVIDENCE_AGE = timedelta(hours=24)
MAX_SOURCE_BUILD_SKEW = timedelta(minutes=2)
COPY_ROOTS = (
    "opendata",
    "opendata_client",
    "alembic",
    "alembic_data",
    "frontend",
)
EXPLICIT_COPY_FILES = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "LICENSE-AKSHARE",
        "THIRD_PARTY_NOTICES.md",
        "alembic.ini",
        "alembic_data.ini",
        "logging_config.ini",
        "frontend/package.json",
        "frontend/package-lock.json",
    }
)
EXPECTED_COPY_SOURCES = frozenset(COPY_ROOTS) | EXPLICIT_COPY_FILES
COPY_CONTEXT_FILES = frozenset({"Dockerfile", ".dockerignore"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})(?:\s+.*)?$")
_EXPECTED_ENV_KEYS = {
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_DATABASE",
    "DATA_MYSQL_HOST",
    "DATA_MYSQL_PORT",
    "DATA_MYSQL_DATABASE",
}


@dataclass(frozen=True)
class RuntimeIssue:
    """A safe evidence failure without command output or secret values."""

    code: str
    message: str


@dataclass(frozen=True)
class RuntimeValidationResult:
    """The current runtime state and measured validation findings."""

    state: str
    facts: dict[str, str]
    issues: tuple[RuntimeIssue, ...]

    @property
    def valid(self) -> bool:
        """Return true only when the complete current apply report validates."""
        return self.state == "validated" and not self.issues


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _relative_file_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str) or not value:
        return None
    raw_parts = value.split("/")
    if "\\" in value or "\x00" in value or any(part in {"", ".", ".."} for part in raw_parts):
        return None
    path = PurePosixPath(value)
    if path.is_absolute():
        return None
    return path


def _is_int(value: object, *, minimum: int | None = None) -> bool:
    return type(value) is int and (minimum is None or value >= minimum)


def _docker_ignored_path(relative: str) -> bool:
    """Mirror the current file-only Docker ignore rules for tracked source paths."""
    path = PurePosixPath(relative)
    parts = path.parts
    name = path.name
    if name == ".env" or name.startswith(".env."):
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx", ".crt", ".cer")):
        return True
    if name.endswith((".pyc", ".pyo", ".pyd", ".egg-info")):
        return True
    ignored_directories = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".benchmarks",
        "node_modules",
        "private",
        "ssl",
        "certs",
    }
    if any(part in ignored_directories for part in parts[:-1]):
        return True
    if any(part.endswith(".egg-info") for part in parts[:-1]):
        return True
    if relative.startswith("frontend/dist/"):
        return True
    return relative in {
        "data",
        "localdata",
        "local_data",
        "local-data",
        "raw",
        "raw_data",
        "raw-data",
        "logs",
        "backups",
        "ssl",
        "private",
        "certs",
        "htmlcov",
        "coverage.xml",
        ".coverage",
        "build",
    } or any(
        relative.startswith(f"{directory}/")
        for directory in (
            "data",
            "localdata",
            "local_data",
            "local-data",
            "raw",
            "raw_data",
            "raw-data",
            "logs",
            "backups",
            "ssl",
            "private",
            "certs",
            "htmlcov",
            "build",
        )
    )


def _managed_copy_paths(
    root: Path,
    issues: list[RuntimeIssue],
    managed_paths: Collection[str] | None,
) -> set[str]:
    if managed_paths is None:
        git = shutil.which("git")
        if git is None:
            issues.append(
                RuntimeIssue(
                    "build-source-inventory-unavailable",
                    "managed build source inventory is unavailable",
                )
            )
            return set()
        try:
            # Fixed git ls-files argv is read-only; shell=False is the subprocess default.
            process = subprocess.run(  # noqa: S603  # nosec B603  # which(git) ls-files -z argv
                [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            issues.append(
                RuntimeIssue(
                    "build-source-inventory-unavailable",
                    "managed build source inventory is unavailable",
                )
            )
            return set()
        if process.returncode != 0:
            issues.append(
                RuntimeIssue(
                    "build-source-inventory-unavailable",
                    "managed build source inventory is unavailable",
                )
            )
            return set()
        raw_paths = process.stdout.split(b"\0")
        try:
            candidates = [item.decode("utf-8") for item in raw_paths if item]
        except UnicodeDecodeError:
            issues.append(
                RuntimeIssue(
                    "build-source-inventory-invalid",
                    "managed source inventory contains a non-UTF-8 path",
                )
            )
            return set()
    else:
        candidates = list(managed_paths)

    managed: set[str] = set()
    copy_roots = tuple(f"{name}/" for name in COPY_ROOTS)
    for value in candidates:
        safe_path = _relative_file_path(value)
        if safe_path is None:
            issues.append(
                RuntimeIssue(
                    "build-source-inventory-invalid",
                    "managed source inventory contains an invalid path",
                )
            )
            continue
        relative = safe_path.as_posix()
        in_projection = (
            relative in COPY_CONTEXT_FILES
            or relative in EXPLICIT_COPY_FILES
            or relative.startswith(copy_roots)
        )
        if in_projection and not _docker_ignored_path(relative):
            managed.add(relative)
    return managed


def _dockerfile_copy_sources(root: Path, issues: list[RuntimeIssue]) -> set[str]:
    dockerfile = root / "Dockerfile"
    try:
        lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        issues.append(RuntimeIssue("build-dockerfile-invalid", "Dockerfile is unreadable"))
        return set()
    sources: set[str] = set()
    for line in lines:
        if re.match(r"^\s*COPY(?:\s|$)", line, re.IGNORECASE) is None:
            continue
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            issues.append(
                RuntimeIssue("build-dockerfile-invalid", "Dockerfile COPY syntax is invalid")
            )
            continue
        if not tokens:
            continue
        instruction = tokens[1:]
        if any(argument == "--from" or argument.startswith("--from=") for argument in instruction):
            continue
        while instruction and instruction[0].startswith("--"):
            instruction = instruction[1:]
        if len(instruction) < 2:
            issues.append(
                RuntimeIssue("build-dockerfile-invalid", "Dockerfile COPY source is missing")
            )
            continue
        sources.update(source.rstrip("/") for source in instruction[:-1])
    if sources != EXPECTED_COPY_SOURCES:
        issues.append(
            RuntimeIssue(
                "build-copy-contract-invalid",
                "Dockerfile COPY sources differ from the fixed roots and metadata",
            )
        )
    return sources


def _read_build_source(
    root: Path,
    now: datetime,
    issues: list[RuntimeIssue],
    managed_paths: Collection[str] | None = None,
) -> str | None:
    path = root / BUILD_SOURCE
    if not path.is_file():
        issues.append(
            RuntimeIssue("build-source-missing", "current build source manifest is missing")
        )
        return None
    try:
        document = _read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        issues.append(
            RuntimeIssue("build-source-invalid", "current build source manifest is invalid")
        )
        return None
    if not isinstance(document, dict):
        issues.append(
            RuntimeIssue("build-source-invalid", "current build source manifest is invalid")
        )
        return None

    timestamp = _parse_timestamp(document.get("date"))
    if timestamp is None or timestamp > now + timedelta(minutes=2):
        issues.append(RuntimeIssue("build-source-date-invalid", "build source date is invalid"))
    elif now - timestamp > MAX_EVIDENCE_AGE:
        issues.append(RuntimeIssue("build-source-stale", "build source manifest is stale"))
    if document.get("projection") != EXPECTED_PROJECTION:
        issues.append(
            RuntimeIssue(
                "build-source-projection-invalid", "build source projection is not recognized"
            )
        )

    _dockerfile_copy_sources(root, issues)
    managed_paths_now = _managed_copy_paths(root, issues, managed_paths)
    expected_files = managed_paths_now | COPY_CONTEXT_FILES
    issues.extend(
        RuntimeIssue(
            "build-source-copy-root-empty",
            f"managed source inventory has no file under COPY root {copy_root}",
        )
        for copy_root in COPY_ROOTS
        if not any(path.startswith(f"{copy_root}/") for path in managed_paths_now)
    )
    issues.extend(
        RuntimeIssue(
            "build-source-copy-input-missing",
            f"managed source inventory is missing explicit COPY input {required_input}",
        )
        for required_input in sorted(EXPLICIT_COPY_FILES)
        if required_input not in managed_paths_now
    )

    raw_files = document.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        issues.append(
            RuntimeIssue("build-source-files-invalid", "build source file map is empty or invalid")
        )
        return None
    files: dict[str, str] = {}
    for raw_path, raw_digest in raw_files.items():
        safe_path = _relative_file_path(raw_path)
        if (
            safe_path is None
            or not isinstance(raw_digest, str)
            or SHA256_RE.fullmatch(raw_digest) is None
        ):
            issues.append(
                RuntimeIssue(
                    "build-source-file-entry-invalid",
                    "build source file map contains an invalid entry",
                )
            )
            continue
        normalized_path = safe_path.as_posix()
        if normalized_path in files:
            issues.append(
                RuntimeIssue(
                    "build-source-file-entry-invalid",
                    "build source file map contains a duplicate path",
                )
            )
            continue
        files[normalized_path] = raw_digest
        source_path = root.joinpath(*safe_path.parts)
        if source_path.is_symlink() or not source_path.is_file():
            issues.append(
                RuntimeIssue(
                    "build-source-file-missing",
                    f"projected source file is missing: {normalized_path}",
                )
            )
            continue
        try:
            digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            issues.append(
                RuntimeIssue(
                    "build-source-file-unreadable",
                    f"projected source file is unreadable: {normalized_path}",
                )
            )
            continue
        if digest != raw_digest:
            issues.append(
                RuntimeIssue(
                    "build-source-file-hash-mismatch",
                    f"projected source bytes changed: {normalized_path}",
                )
            )

    if set(files) != expected_files:
        issues.append(
            RuntimeIssue(
                "build-source-projection-set-mismatch",
                "build source file map differs from the current managed COPY projection",
            )
        )

    issues.extend(
        RuntimeIssue(
            "build-source-projection-incomplete",
            f"build source projection omits {required}",
        )
        for required in ("Dockerfile", ".dockerignore")
        if required not in files
    )
    computed_identity = hashlib.sha256(
        json.dumps(files, sort_keys=True).encode("utf-8")
    ).hexdigest()
    source_identity = document.get("source_identity")
    if not isinstance(source_identity, str) or source_identity != computed_identity:
        issues.append(
            RuntimeIssue(
                "build-source-identity-mismatch",
                "build source identity does not match its file map",
            )
        )
        return None
    return source_identity


def _validate_build_log(
    root: Path,
    source_identity: str | None,
    now: datetime,
    issues: list[RuntimeIssue],
) -> tuple[datetime | None, str | None]:
    path = root / BUILD_LOG
    if not path.is_file():
        issues.append(RuntimeIssue("build-log-missing", "current Docker build log is missing"))
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        issues.append(RuntimeIssue("build-log-invalid", "current Docker build log is unreadable"))
        return None, None
    date_match = re.search(r"^Date: (.+)$", text, re.MULTILINE)
    build_date = _parse_timestamp(date_match.group(1).strip()) if date_match else None
    if build_date is None or build_date > now + timedelta(minutes=2):
        issues.append(RuntimeIssue("build-log-date-invalid", "Docker build log date is invalid"))
    elif now - build_date > MAX_EVIDENCE_AGE:
        issues.append(RuntimeIssue("build-log-stale", "Docker build log is stale"))
    head_values = re.findall(r"^HEAD:\s*(.+?)\s*$", text, re.MULTILINE)
    head_match = COMMIT_RE.fullmatch(head_values[0]) if len(head_values) == 1 else None
    build_head = head_match.group(1) if head_match else None
    if build_head is None:
        issues.append(
            RuntimeIssue("build-head-invalid", "Docker build log HEAD is missing or invalid")
        )
    source_date = _parse_timestamp_from_manifest(root / BUILD_SOURCE)
    if (
        source_identity is None
        or build_date is None
        or source_date is None
        or abs(build_date - source_date) > MAX_SOURCE_BUILD_SKEW
    ):
        issues.append(
            RuntimeIssue(
                "build-source-date-mismatch",
                "build log and source manifest are not bound to the same build",
            )
        )
    expected_fragments = (
        f"--label codex.task={EXPECTED_BUILD_TASK}",
        f"--label codex.source={source_identity or ''}",
        f"-t {APP_IMAGE_TAG}",
    )
    command_match = re.search(r"^Command: (.+)$", text, re.MULTILINE)
    if not command_match or not all(
        fragment in command_match.group(1) for fragment in expected_fragments
    ):
        issues.append(
            RuntimeIssue(
                "build-command-binding-invalid",
                "Docker build command is not bound to the current source identity",
            )
        )
    exit_values = re.findall(r"^BUILD_EXIT=(\d+)\s*$", text, re.MULTILINE)
    if exit_values != ["0"]:
        issues.append(
            RuntimeIssue(
                "build-exit-not-zero",
                "current Docker build did not finish with exactly one successful exit",
            )
        )
    return build_date, build_head


def _parse_timestamp_from_manifest(path: Path) -> datetime | None:
    try:
        document = _read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    timestamp = _parse_timestamp(document.get("date") if isinstance(document, dict) else None)
    return timestamp


def _validate_driver_log(
    root: Path,
    build_date: datetime | None,
    build_head: str | None,
    now: datetime,
    issues: list[RuntimeIssue],
) -> dict[str, str] | None:
    path = root / DRIVER_LOG
    if not path.is_file():
        issues.append(
            RuntimeIssue("runtime-driver-missing", "current verifier driver log is missing")
        )
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        issues.append(
            RuntimeIssue("runtime-driver-invalid", "current verifier driver log is unreadable")
        )
        return None

    date_values = re.findall(r"^Date:\s*(.+?)\s*$", text, re.MULTILINE)
    driver_date = _parse_timestamp(date_values[0]) if len(date_values) == 1 else None
    if driver_date is None or driver_date > now + timedelta(minutes=2):
        issues.append(
            RuntimeIssue(
                "runtime-driver-date-invalid", "verifier driver date is missing or invalid"
            )
        )
    elif now - driver_date > MAX_EVIDENCE_AGE:
        issues.append(RuntimeIssue("runtime-driver-stale", "verifier driver log is stale"))
    if driver_date is None or build_date is None or driver_date < build_date:
        issues.append(
            RuntimeIssue(
                "runtime-driver-build-date-mismatch",
                "verifier run does not follow the current build",
            )
        )

    head_values = re.findall(r"^HEAD:\s*(.+?)\s*$", text, re.MULTILINE)
    head_match = COMMIT_RE.fullmatch(head_values[0]) if len(head_values) == 1 else None
    driver_head = head_match.group(1) if head_match else None
    if driver_head is None:
        issues.append(
            RuntimeIssue(
                "runtime-driver-head-invalid", "verifier driver HEAD is missing or invalid"
            )
        )
    elif build_head is None or driver_head != build_head:
        issues.append(
            RuntimeIssue(
                "runtime-driver-head-mismatch", "verifier driver HEAD differs from the bound build"
            )
        )

    command_values = re.findall(r"^Command:\s*(.+?)\s*$", text, re.MULTILINE)
    command = command_values[0] if len(command_values) == 1 else ""
    if not all(
        value in command
        for value in (VERIFIER_SCRIPT.as_posix(), "--apply", STACK_REPORT.as_posix())
    ):
        issues.append(
            RuntimeIssue(
                "runtime-driver-command-invalid",
                "verifier driver command is not the current apply run",
            )
        )

    verifier_values = re.findall(r"^Verifier_SHA256:\s*(.+?)\s*$", text, re.MULTILINE)
    verifier_hash = verifier_values[0] if len(verifier_values) == 1 else ""
    if SHA256_RE.fullmatch(verifier_hash) is None:
        issues.append(
            RuntimeIssue(
                "runtime-driver-verifier-hash-invalid",
                "verifier source SHA256 is missing or invalid",
            )
        )
    else:
        try:
            current_hash = hashlib.sha256((root / VERIFIER_SCRIPT).read_bytes()).hexdigest()
        except OSError:
            current_hash = ""
        if verifier_hash != current_hash:
            issues.append(
                RuntimeIssue(
                    "runtime-driver-verifier-hash-mismatch",
                    "verifier driver hash does not match the current verifier source",
                )
            )

    exits = re.findall(r"^RUNTIME_EXIT=(\d+)\s*$", text, re.MULTILINE)
    exit_value = exits[0] if len(exits) == 1 else "unknown"
    if exits != ["0"]:
        issues.append(
            RuntimeIssue(
                "runtime-driver-exit-not-zero",
                "verifier driver did not finish with exactly one successful exit",
            )
        )
    return {
        "date": date_values[0] if len(date_values) == 1 else "",
        "head": driver_head or "",
        "exit": exit_value,
        "verifier_sha256": verifier_hash,
        "command": command,
    }


def _validate_start_run(
    root: Path,
    source_identity: str | None,
    build_date: datetime | None,
    now: datetime,
    issues: list[RuntimeIssue],
) -> dict[str, Any] | None:
    path = root / START_RUN
    if not path.is_file():
        issues.append(
            RuntimeIssue("runtime-start-missing", "current runtime start record is missing")
        )
        return None
    try:
        document = _read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        issues.append(
            RuntimeIssue("runtime-start-invalid", "current runtime start record is invalid")
        )
        return None
    if not isinstance(document, dict):
        issues.append(
            RuntimeIssue("runtime-start-invalid", "current runtime start record is invalid")
        )
        return None
    started = _parse_timestamp(document.get("date"))
    if started is None or started > now + timedelta(minutes=2):
        issues.append(RuntimeIssue("runtime-start-date-invalid", "runtime start date is invalid"))
    elif now - started > MAX_EVIDENCE_AGE:
        issues.append(RuntimeIssue("runtime-start-stale", "runtime start record is stale"))
    if build_date is not None and started is not None and started < build_date:
        issues.append(
            RuntimeIssue(
                "runtime-start-before-build", "runtime start predates the bound Docker build"
            )
        )
    if document.get("round") != "C65" or document.get("container") != APP_CONTAINER:
        issues.append(
            RuntimeIssue(
                "runtime-start-target-invalid",
                "runtime start is not bound to the owned C65 container",
            )
        )
    if type(document.get("exit")) is not int or document.get("exit") != 0:
        issues.append(
            RuntimeIssue(
                "runtime-start-exit-not-zero", "owned runtime container did not start successfully"
            )
        )
    if (
        not isinstance(document.get("command"), str)
        or "127.0.0.1:33566:8000" not in document["command"]
    ):
        issues.append(
            RuntimeIssue(
                "runtime-start-endpoint-invalid",
                "runtime start command does not bind the fixed local endpoint",
            )
        )
    if source_identity is None or document.get("source_identity") != source_identity:
        issues.append(
            RuntimeIssue(
                "runtime-start-source-mismatch",
                "runtime start source differs from the current build",
            )
        )
    image_id = document.get("image_id")
    if not isinstance(image_id, str) or IMAGE_ID_RE.fullmatch(image_id) is None:
        issues.append(
            RuntimeIssue("runtime-start-image-invalid", "runtime start image identity is invalid")
        )
    if (
        not isinstance(document.get("identity"), str)
        or SHA256_RE.fullmatch(document["identity"]) is None
    ):
        issues.append(
            RuntimeIssue(
                "runtime-start-identity-invalid", "runtime start record identity is invalid"
            )
        )
    if (
        document.get("native_3306_writes") != 0
        or type(document.get("native_3306_writes")) is not int
    ):
        issues.append(
            RuntimeIssue(
                "runtime-native-3306-write",
                "runtime start reports a write to native MySQL port 3306",
            )
        )
    if document.get("mysql_port") != 33565 or type(document.get("mysql_port")) is not int:
        issues.append(
            RuntimeIssue(
                "runtime-target-port-invalid", "runtime start does not use the isolated MySQL port"
            )
        )
    if document.get("target_schemas") != [
        DB_SCHEMAS["MYSQL_DATABASE"],
        DB_SCHEMAS["DATA_MYSQL_DATABASE"],
    ]:
        issues.append(
            RuntimeIssue(
                "runtime-target-schemas-invalid", "runtime start schema targets are not isolated"
            )
        )
    issues.extend(
        RuntimeIssue(
            "runtime-start-safety-invalid",
            f"runtime start field {field} is not safely disabled",
        )
        for field in ("emails_enabled", "upstream_credentials_supplied", "secret_values_recorded")
        if document.get(field) is not False
    )
    return document


def _validate_inspect(root: Path, issues: list[RuntimeIssue]) -> object | None:
    path = root / CONTAINER_INSPECT
    if not path.is_file():
        issues.append(
            RuntimeIssue(
                "container-inspect-missing", "sanitized Docker inspect artifact is missing"
            )
        )
        return None
    try:
        document = _read_json(path)
        identity = validate_docker_inspect(document)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, VerificationError):
        issues.append(
            RuntimeIssue(
                "container-inspect-invalid",
                "sanitized Docker inspect does not prove the isolated target",
            )
        )
        return None
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        issues.append(
            RuntimeIssue("container-inspect-invalid", "sanitized Docker inspect shape is invalid")
        )
        return None
    container = document[0]
    config = container.get("Config")
    environment = config.get("Env") if isinstance(config, dict) else None
    if (
        not isinstance(environment, list)
        or len(environment) != len(_EXPECTED_ENV_KEYS)
        or any(not isinstance(entry, str) or "=" not in entry for entry in environment)
        or {entry.partition("=")[0] for entry in environment if isinstance(entry, str)}
        != _EXPECTED_ENV_KEYS
    ):
        issues.append(
            RuntimeIssue(
                "container-inspect-not-sanitized",
                "Docker inspect must contain only the six approved database environment fields",
            )
        )
    state = container.get("State")
    if not isinstance(state, dict) or state.get("Status") != "running":
        issues.append(
            RuntimeIssue("container-not-running", "owned application container is not running")
        )
    return identity


def _validate_browser(report: object, issues: list[RuntimeIssue]) -> None:
    if not isinstance(report, dict) or report.get("ok") is not True:
        issues.append(
            RuntimeIssue(
                "frontend-login-missing",
                "current browser login evidence is missing or unsuccessful",
            )
        )
        return
    expected = (
        ("dashboard", f"{ENDPOINT}/", "首页 - opendata"),
        ("catalog", f"{ENDPOINT}/data", "数据目录 - opendata"),
    )
    for name, expected_url, expected_title in expected:
        page = report.get(name)
        if (
            not isinstance(page, dict)
            or page.get("url") != expected_url
            or page.get("title") != expected_title
            or page.get("authenticated_element") != ".user-dropdown .username"
            or page.get("authenticated_element_visible") is not True
        ):
            issues.append(
                RuntimeIssue(
                    f"frontend-{name}-identity-invalid",
                    f"browser {name} identity does not match the authenticated C65 page",
                )
            )


def _validate_stack_report(
    root: Path,
    source_identity: str | None,
    image_id: str | None,
    now: datetime,
    issues: list[RuntimeIssue],
) -> dict[str, Any] | None:
    path = root / STACK_REPORT
    if not path.is_file():
        issues.append(
            RuntimeIssue("runtime-stack-missing", "current runtime verification report is missing")
        )
        return None
    try:
        document = _read_json(path)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        issues.append(
            RuntimeIssue("runtime-stack-invalid", "current runtime verification report is invalid")
        )
        return None
    if not isinstance(document, dict):
        issues.append(
            RuntimeIssue("runtime-stack-invalid", "current runtime verification report is invalid")
        )
        return None
    report_date = document.get("date")
    try:
        parsed_date = date.fromisoformat(report_date) if isinstance(report_date, str) else None
    except ValueError:
        parsed_date = None
    local_today = now.astimezone().date()
    if parsed_date is None or parsed_date > local_today or (local_today - parsed_date).days > 1:
        issues.append(
            RuntimeIssue(
                "runtime-stack-date-stale",
                "current runtime verification report date is stale or invalid",
            )
        )
    if document.get("mode") != "apply":
        issues.append(
            RuntimeIssue(
                "runtime-stack-not-apply", "current runtime verification was not an apply run"
            )
        )
    if document.get("endpoint") != ENDPOINT:
        issues.append(
            RuntimeIssue(
                "runtime-stack-endpoint-invalid",
                "runtime report endpoint is not the fixed isolated endpoint",
            )
        )
    source = document.get("source")
    if (
        not isinstance(source, dict)
        or source.get("source_identity") != source_identity
        or source.get("image_id") != image_id
    ):
        issues.append(
            RuntimeIssue(
                "runtime-stack-image-source-mismatch",
                "runtime report image/source differs from the built and inspected image",
            )
        )
    health = document.get("health")
    if (
        not isinstance(health, dict)
        or health.get("status") != "healthy"
        or health.get("database") != "connected"
    ):
        issues.append(
            RuntimeIssue(
                "runtime-health-invalid",
                "runtime report does not show a healthy connected database",
            )
        )
    static = document.get("static")
    if (
        not isinstance(static, dict)
        or static.get("home_http_200") is not True
        or static.get("assets_valid") is not True
        or not _is_int(static.get("asset_count"), minimum=1)
    ):
        issues.append(
            RuntimeIssue(
                "runtime-static-invalid", "runtime report does not show valid static assets"
            )
        )
    if document.get("userdb_write_isolated") is not True:
        issues.append(
            RuntimeIssue(
                "runtime-user-write-isolation-invalid",
                "runtime report does not prove isolated user registration",
            )
        )
    if document.get("error") is not None:
        issues.append(
            RuntimeIssue(
                "runtime-report-error", "current runtime verification report contains an error"
            )
        )
    _validate_browser(document.get("frontend_login"), issues)
    return document


def validate(
    root: Path,
    *,
    now: datetime | None = None,
    managed_paths: Collection[str] | None = None,
) -> RuntimeValidationResult:
    """Validate saved C65 build, start, inspect, and live-verification evidence only.

    The function performs file reads and hashes only. In particular, it does not invoke Docker,
    contact the application, connect to a database, or perform a user write.
    """
    current = _utc_now(now)
    issues: list[RuntimeIssue] = []
    paths = (START_RUN, CONTAINER_INSPECT, STACK_REPORT)
    runtime_artifacts_present = any((root / path).exists() for path in paths)
    if not runtime_artifacts_present:
        return RuntimeValidationResult(
            state="not-started",
            facts={"source_identity": "", "image_id": "", "endpoint": ENDPOINT},
            issues=(
                RuntimeIssue(
                    "runtime-not-started",
                    "no current runtime start or verification evidence exists",
                ),
            ),
        )

    start_exists = (root / START_RUN).is_file()
    inspect_exists = (root / CONTAINER_INSPECT).is_file()
    stack_path = root / STACK_REPORT
    if not start_exists and not inspect_exists and stack_path.is_file():
        try:
            preliminary_report = _read_json(stack_path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            preliminary_report = None
        if isinstance(preliminary_report, dict) and preliminary_report.get("mode") in {
            "dry-run",
            "preflight",
        }:
            return RuntimeValidationResult(
                state="not-started",
                facts={"source_identity": "", "image_id": "", "endpoint": ENDPOINT},
                issues=(
                    RuntimeIssue("runtime-not-started", "runtime evidence records preflight only"),
                ),
            )

    source_identity = _read_build_source(root, current, issues, managed_paths)
    build_date, build_head = _validate_build_log(root, source_identity, current, issues)
    driver = _validate_driver_log(root, build_date, build_head, current, issues)
    start = _validate_start_run(root, source_identity, build_date, current, issues)
    driver_date = _parse_timestamp(driver.get("date")) if driver else None
    start_date = _parse_timestamp(start.get("date")) if isinstance(start, dict) else None
    if driver_date is None or start_date is None or driver_date < start_date:
        issues.append(
            RuntimeIssue(
                "runtime-driver-before-start",
                "verifier driver run does not follow the container start",
            )
        )
    inspect = _validate_inspect(root, issues)
    inspected_source = getattr(inspect, "source_identity", None)
    inspected_image = getattr(inspect, "image_id", None)
    if source_identity is None or inspected_source != source_identity:
        issues.append(
            RuntimeIssue(
                "container-source-binding-mismatch",
                "inspected image source differs from the current source manifest",
            )
        )
    start_image = start.get("image_id") if isinstance(start, dict) else None
    start_source = start.get("source_identity") if isinstance(start, dict) else None
    if inspected_image is None or start_image != inspected_image:
        issues.append(
            RuntimeIssue(
                "runtime-start-image-mismatch",
                "started container image differs from the inspected image",
            )
        )
    if source_identity is None or start_source != source_identity:
        issues.append(
            RuntimeIssue(
                "runtime-start-source-mismatch",
                "started container source differs from the current source manifest",
            )
        )
    report = _validate_stack_report(root, source_identity, inspected_image, current, issues)
    if report is not None:
        stack_date = report.get("date")
        driver_time = _parse_timestamp(driver.get("date")) if driver else None
        verifier_local_date = driver_time.astimezone().date() if driver_time is not None else None
        if verifier_local_date is not None and stack_date != verifier_local_date.isoformat():
            issues.append(
                RuntimeIssue(
                    "runtime-stack-driver-date-mismatch",
                    "runtime report date differs from the verifier run's local calendar date",
                )
            )

    codes = sorted({issue.code for issue in issues})
    facts = {
        "source_identity": source_identity or "",
        "image_id": inspected_image or (start_image if isinstance(start_image, str) else ""),
        "endpoint": ENDPOINT,
        "issue_count": str(len(issues)),
        "issue_summary": ",".join(codes[:8]) or "none",
        "build_exit": (
            "0"
            if build_date is not None
            and not any(issue.code == "build-exit-not-zero" for issue in issues)
            else "failed"
            if build_date is not None
            else "unknown"
        ),
        "build_head": build_head or "",
        "driver_exit": driver.get("exit", "unknown") if driver else "unknown",
        "driver_head": driver.get("head", "") if driver else "",
        "driver_verifier_sha256": driver.get("verifier_sha256", "") if driver else "",
        "start_exit": str(start.get("exit", "unknown")) if isinstance(start, dict) else "unknown",
        "health_database": "connected"
        if report
        and isinstance(report.get("health"), dict)
        and report["health"].get("database") == "connected"
        else "unknown",
        "static_asset_count": str(report["static"].get("asset_count", 0))
        if report and isinstance(report.get("static"), dict)
        else "0",
        "frontend_identity": "verified"
        if report and not any(issue.code.startswith("frontend-") for issue in issues)
        else "unverified",
    }
    return RuntimeValidationResult(
        state="validated" if not issues else "failed",
        facts=facts,
        issues=tuple(issues),
    )
