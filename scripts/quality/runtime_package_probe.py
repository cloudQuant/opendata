"""Build and clean-install the opendata wheel and sdist without using the checkout.

The probe intentionally starts every Python command with the selected Anaconda base
interpreter.  A temporary, system-site-packages-disabled venv is only the pip
installation target.  Import/resource smoke checks use base Python with ``-S`` and
the target venv's site-packages as their sole non-stdlib import path.

Run from any directory with::

    /Users/yunjinqi/opt/anaconda3/bin/conda run -n base python \
        scripts/quality/runtime_package_probe.py

The source snapshot follows the whitelist in ``tests/test_container_build_contract.py``.
Evidence is written below ``docs/evidence/C100/runtime-packaging/runs``.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import math
import os
import shutil
import subprocess  # nosec B404  # guarded command families are the only subprocess users
import sys
import tarfile
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn, TypeAlias, TypedDict
from zipfile import ZipFile

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        from pip._vendor import tomli as tomllib

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = PROJECT_ROOT / "docs/evidence/C100/runtime-packaging/runs"
SOURCE_FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "LICENSE-AKSHARE",
    "THIRD_PARTY_NOTICES.md",
)
REQUIRED_PACKAGE_RESOURCES = (
    "opendata/data/providers/akshare/_vendor/manifest.json",
    "opendata/data/providers/akshare/_vendor/upstream.lock",
    "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
    "opendata/data/providers/akshare/_vendor/file_fold/calendar.json",
    "opendata/data/providers/akshare/_vendor/stock_feature/ths.js",
    "opendata/data/providers/ths/endpoint_map.yaml",
    "opendata/data/providers/ths/transport/error_messages.yaml",
)
DEFAULT_TIMEOUT_SECONDS = 900
MIT_VENDOR_PREFIX = "opendata/data/providers/akshare/_vendor/"
SMOKE_REPORT_SCHEMA = "opendata-installed-smoke/v2"
SMOKE_PRODUCT_IMPORTS = (
    "opendata",
    "opendata.data",
    "opendata.data.providers.akshare._vendor",
)
SMOKE_GUARD_COUNTERS = ("database", "entrypoint_import", "process", "socket")
SMOKE_GUARD_COVERAGE: dict[str, object] = {
    "socket_audit_events": "socket.*",
    "process_audit_events": [
        "os.exec*",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn*",
        "os.spawn*",
        "os.startfile",
        "os.system",
        "subprocess.Popen",
    ],
    "database_audit_events": "sqlite3.*",
    "blocked_dbapi_import_roots": [
        "_sqlite3",
        "aiomysql",
        "aiosqlite",
        "asyncpg",
        "cx_oracle",
        "databases",
        "duckdb",
        "mariadb",
        "motor",
        "mysql",
        "oracledb",
        "peewee",
        "psycopg",
        "psycopg2",
        "pymongo",
        "pymysql",
        "pyodbc",
        "sqlalchemy",
        "sqlite3",
        "sqlmodel",
        "tortoise",
    ],
    "blocked_entrypoint_module_names": ["lifespan", "main"],
    "module_import_blocker": "sys.meta_path.find_spec",
    "pip_network_blocked": False,
}
RequirementKey = tuple[
    str,
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    str | None,
]
DependencyContract = tuple[Counter[RequirementKey], Counter[str]]
JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class CommandOutput(TypedDict):
    """Typed metadata for a retained stdout or stderr log."""

    path: str
    size_bytes: int
    sha256: str


class CommandRecord(TypedDict):
    """Validated subprocess result stored in the probe receipt."""

    argv: list[str]
    cwd: str
    started_at_utc: str
    finished_at_utc: str
    duration_seconds: float
    timeout_seconds: int
    timed_out: bool
    exit_code: int | None
    stdout: CommandOutput
    stderr: CommandOutput


class ProbeFailureError(RuntimeError):
    """A measured packaging condition failed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def _workspace_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name.startswith(
            ("opendata-runtime-package-", "opendata-package-counterfactual-")
        ):
            return candidate
    return None


def _owned_venv_python(path: Path, workspace: Path) -> bool:
    """Accept only a directly owned venv interpreter path."""
    candidate_parent = path.parent.resolve()
    root = workspace.resolve()
    try:
        relative = candidate_parent.relative_to(root)
    except ValueError:
        return False
    if len(relative.parts) != 2:
        return False
    if os.name == "nt":
        if relative.parts[1:] != ("Scripts",) or path.name != "python.exe":
            return False
    elif relative.parts[1:] != ("bin",) or path.name != "python":
        return False
    venv_root = root / relative.parts[0]
    return not venv_root.exists() or (
        _is_within(venv_root, root) and (venv_root / "pyvenv.cfg").is_file()
    )


def _owned_wheel_input(value: str, workspace: Path) -> bool:
    """Accept a wheel from the probe workspace or retained evidence directory."""
    path = Path(value)
    return (
        path.is_absolute()
        and path.name.endswith(".whl")
        and (_is_within(path, workspace) or _is_within(path, EVIDENCE_ROOT))
    )


def _validate_pip_request(arguments: list[str], cwd: Path, workspace: Path) -> None:
    """Validate the exact pip command shapes dispatched by this probe."""
    if not arguments:
        raise ProbeFailureError("probe pip action is not allowlisted")

    venv_python: Path | None = None
    if arguments[0] == "--python":
        if len(arguments) < 3:
            raise ProbeFailureError("probe pip venv target is incomplete")
        venv_python = Path(arguments[1])
        if not _owned_venv_python(venv_python, workspace):
            raise ProbeFailureError("probe pip target must be an owned venv interpreter")
        action = arguments[2]
        options = arguments[3:]
    else:
        action = arguments[0]
        options = arguments[1:]

    if action == "check":
        if venv_python is None or options:
            raise ProbeFailureError(
                "probe pip check must target its owned venv without extra flags"
            )
        return

    if action == "wheel":
        if venv_python is not None:
            raise ProbeFailureError("probe pip wheel must use the base interpreter")
        seen: set[str] = set()
        positionals: list[str] = []
        wheel_dir: str | None = None
        index = 0
        while index < len(options):
            option = options[index]
            if option in {"--no-deps", "--no-build-isolation"}:
                if option in seen:
                    raise ProbeFailureError("probe pip wheel contains a repeated option")
                seen.add(option)
                index += 1
                continue
            if option == "--wheel-dir":
                if option in seen or index + 1 >= len(options):
                    raise ProbeFailureError(
                        "probe pip wheel output directory is missing or repeated"
                    )
                seen.add(option)
                wheel_dir = options[index + 1]
                index += 2
                continue
            if option.startswith("-"):
                raise ProbeFailureError("probe pip wheel contains an unapproved option")
            positionals.append(option)
            index += 1
        expected_dir = workspace / "sdist-rebuilt-wheel"
        if (
            seen != {"--no-deps", "--no-build-isolation", "--wheel-dir"}
            or wheel_dir is None
            or Path(wheel_dir).resolve() != expected_dir.resolve()
            or not _is_within(Path(wheel_dir), workspace)
            or len(positionals) != 1
            or not positionals[0].endswith(".tar.gz")
            or not _is_within(Path(positionals[0]), workspace)
        ):
            raise ProbeFailureError("probe pip wheel must use its fixed workspace input and output")
        return

    if action != "install":
        raise ProbeFailureError("probe pip action is not allowlisted")

    seen = set()
    positionals = []
    target: str | None = None
    index = 0
    while index < len(options):
        option = options[index]
        if option in {
            "--no-input",
            "--disable-pip-version-check",
            "--force-reinstall",
            "--no-deps",
        }:
            if option in seen:
                raise ProbeFailureError("probe pip install contains a repeated option")
            seen.add(option)
            index += 1
            continue
        if option == "--target":
            if option in seen or index + 1 >= len(options):
                raise ProbeFailureError("probe pip install target is missing or repeated")
            seen.add(option)
            target = options[index + 1]
            index += 2
            continue
        if option.startswith("-"):
            raise ProbeFailureError("probe pip install contains an unapproved option")
        positionals.append(option)
        index += 1

    required = {"--no-input", "--disable-pip-version-check"}
    if not required.issubset(seen) or len(positionals) != 1:
        raise ProbeFailureError(
            "probe pip install must use fixed non-interactive flags and one wheel"
        )
    if not _owned_wheel_input(positionals[0], workspace):
        raise ProbeFailureError("probe pip install input must be an owned local wheel")

    if target is None:
        if venv_python is None:
            raise ProbeFailureError("probe pip install requires its owned venv destination")
        return

    expected_target = workspace / "installed-wheel"
    if (
        venv_python is not None
        or not workspace.name.startswith(
            ("opendata-package-counterfactual-", "opendata-runtime-package-")
        )
        or Path(cwd).resolve() != workspace.resolve()
        or Path(target).resolve() != expected_target.resolve()
        or not _is_within(Path(target), workspace)
        or seen != {"--target", "--no-deps", "--no-input", "--disable-pip-version-check"}
    ):
        raise ProbeFailureError("probe pip target install is outside the fixed counterfactual")


def _validate_subprocess_request(command: list[str], cwd: Path, timeout_seconds: int) -> None:
    """Reject commands or working directories outside the probe's fixed surface."""
    if not command or any(
        type(argument) is not str or not argument or "\0" in argument for argument in command
    ):
        raise ProbeFailureError("probe subprocess argv must be a non-empty list of text arguments")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ProbeFailureError("probe subprocess timeout must be a positive integer")

    resolved_cwd = cwd.resolve()
    workspace = _workspace_root(resolved_cwd)
    project_cwd = resolved_cwd == PROJECT_ROOT.resolve()
    if not project_cwd and workspace is None:
        raise ProbeFailureError(
            "probe subprocess cwd is not owned by this checkout or probe workspace"
        )

    if command == ["git", "rev-parse", "HEAD"]:
        if not project_cwd:
            raise ProbeFailureError("git identity lookup must run from the project checkout")
        return
    if command[0] != sys.executable:
        raise ProbeFailureError("probe subprocess executable is not the selected base Python")

    if command[1:3] == ["-S", "-c"]:
        if len(command) != 4 or command[3] != _smoke_code():
            raise ProbeFailureError("probe smoke subprocess must use the fixed smoke program")
        return
    if command[1:2] == ["-c"]:
        raise ProbeFailureError("runtime probe does not permit arbitrary inline subprocess code")
    if command[1:3] == ["-m", "venv"]:
        if len(command) != 4 or workspace is None or not _is_within(Path(command[3]), workspace):
            raise ProbeFailureError("probe venv target must stay inside its owned workspace")
        return
    if command[1:3] == ["-m", "build"]:
        if (
            len(command) != 8
            or command[3:7] != ["--no-isolation", "--wheel", "--sdist", "--outdir"]
            or workspace is None
            or not _is_within(Path(command[7]), workspace)
        ):
            raise ProbeFailureError("probe build command must use the fixed workspace output")
        return
    if command[1:3] == ["-m", "pip"] and workspace is not None:
        _validate_pip_request(command[3:], resolved_cwd, workspace)
        return
    raise ProbeFailureError("probe subprocess command is outside the fixed command allowlist")


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _validated_json_value(value: object, context: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProbeFailureError(f"{context} contains a non-finite JSON number")
        return value
    if isinstance(value, list):
        return [_validated_json_value(item, context) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProbeFailureError(f"{context} contains a non-text object key")
            result[key] = _validated_json_value(item, context)
        return result
    raise ProbeFailureError(f"{context} contains a value outside the JSON data model")


def _parse_json_object(text: str, context: str) -> JsonObject:
    try:
        decoded: object = json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProbeFailureError(f"{context} is not valid standard JSON") from exc
    validated = _validated_json_value(decoded, context)
    if not isinstance(validated, dict):
        raise ProbeFailureError(f"{context} must be a JSON object")
    return validated


def _command_output_record(value: object, context: str) -> CommandOutput:
    if not isinstance(value, dict):
        raise ProbeFailureError(f"{context} command log record must be an object")
    path = value.get("path")
    size_bytes = value.get("size_bytes")
    sha256 = value.get("sha256")
    if (
        not isinstance(path, str)
        or type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(sha256, str)
    ):
        raise ProbeFailureError(f"{context} command log record is incomplete")
    return {"path": path, "size_bytes": size_bytes, "sha256": sha256}


def _command_record(state: dict[str, Any], name: str) -> CommandRecord:
    commands: object = state.get("commands")
    if not isinstance(commands, dict):
        raise ProbeFailureError("probe command state must be an object")
    value: object = commands.get(name)
    if not isinstance(value, dict):
        raise ProbeFailureError(f"probe command record is missing: {name}")
    argv = value.get("argv")
    cwd = value.get("cwd")
    started_at = value.get("started_at_utc")
    finished_at = value.get("finished_at_utc")
    duration = value.get("duration_seconds")
    timeout = value.get("timeout_seconds")
    timed_out = value.get("timed_out")
    exit_code = value.get("exit_code")
    if (
        not isinstance(argv, list)
        or any(not isinstance(argument, str) for argument in argv)
        or not isinstance(cwd, str)
        or not isinstance(started_at, str)
        or not isinstance(finished_at, str)
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or type(timeout) is not int
        or timeout <= 0
        or not isinstance(timed_out, bool)
        or (exit_code is not None and type(exit_code) is not int)
    ):
        raise ProbeFailureError(f"probe command record has invalid fields: {name}")
    return {
        "argv": argv,
        "cwd": cwd,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "duration_seconds": float(duration),
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": _command_output_record(value.get("stdout"), f"{name}.stdout"),
        "stderr": _command_output_record(value.get("stderr"), f"{name}.stderr"),
    }


def _source_file_paths(root: Path) -> list[Path]:
    """Return the exact package source whitelist, excluding generated Python caches."""
    paths: list[Path] = []
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise ProbeFailureError(f"required source file is missing: {name}")
        paths.append(path)

    package_root = root / "opendata"
    if not package_root.is_dir():
        raise ProbeFailureError("required source directory is missing: opendata/")
    for directory, dirnames, filenames in os.walk(package_root):
        dirnames[:] = sorted(
            name for name in dirnames if name not in {"__pycache__", ".pytest_cache"}
        )
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo", ".pyd")):
                continue
            path = Path(directory) / filename
            if path.is_symlink():
                resolved = path.resolve()
                if not _is_within(resolved, package_root):
                    raise ProbeFailureError(f"package source symlink escapes opendata/: {path}")
            if path.is_file():
                paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _source_manifest(root: Path) -> dict[str, Any]:
    """Hash every file copied into the external build snapshot."""
    records: list[dict[str, Any]] = []
    fingerprint = hashlib.sha256()
    for path in _source_file_paths(root):
        relative = path.relative_to(root).as_posix()
        digest = _sha256_file(path)
        size = path.stat().st_size
        fingerprint.update(relative.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(digest.encode("ascii"))
        fingerprint.update(b"\n")
        records.append({"path": relative, "size_bytes": size, "sha256": digest})
    return {
        "algorithm": "sha256",
        "fingerprint": fingerprint.hexdigest(),
        "file_count": len(records),
        "files": records,
    }


def _copy_source_snapshot(source_root: Path, snapshot_root: Path) -> None:
    """Copy only the established build whitelist to a path outside the checkout."""
    snapshot_root.mkdir(parents=True, exist_ok=False)
    for name in SOURCE_FILES:
        shutil.copy2(source_root / name, snapshot_root / name)
    shutil.copytree(
        source_root / "opendata",
        snapshot_root / "opendata",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]", ".pytest_cache"),
    )


def _clean_environment(*, isolated_pip: bool = False) -> dict[str, str]:
    """Remove import/install redirection and coverage injection from subprocesses."""
    environment = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PIP_TARGET",
        "PIP_PREFIX",
        "COVERAGE_PROCESS_START",
    ):
        environment.pop(name, None)
    for name in tuple(environment):
        if name.startswith("COV_CORE_"):
            environment.pop(name, None)
    environment.update(
        {
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    if isolated_pip:
        # Avoid inherited user/system pip config and any credentialed index URL.
        environment["PIP_CONFIG_FILE"] = os.devnull
        environment["PIP_INDEX_URL"] = "https://pypi.org/simple"
        environment.pop("PIP_EXTRA_INDEX_URL", None)
        environment.pop("PIP_TRUSTED_HOST", None)
    return environment


def _decode_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_command(
    *,
    state: dict[str, Any],
    name: str,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    run_dir: Path,
) -> CommandRecord:
    """Run one bounded command and persist complete logs plus exit/timeout metadata."""
    _validate_subprocess_request(command, cwd, timeout_seconds)
    started_at = _utc_now()
    monotonic_start = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv; owned cwd checked above
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            shell=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_output(exc.stdout)
        stderr = _decode_output(exc.stderr)
    except OSError as exc:
        exit_code = 127
        stderr = f"could not start child process: {type(exc).__name__}: {exc}\n"

    duration = round(time.monotonic() - monotonic_start, 3)
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"{name}.stdout.txt"
    stderr_path = log_dir / f"{name}.stderr.txt"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    record: CommandRecord = {
        "argv": command,
        "cwd": str(cwd),
        "started_at_utc": started_at,
        "finished_at_utc": _utc_now(),
        "duration_seconds": duration,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "exit_code": exit_code,
        "stdout": {
            "path": str(stdout_path.relative_to(run_dir)),
            "size_bytes": stdout_path.stat().st_size,
            "sha256": _sha256_file(stdout_path),
        },
        "stderr": {
            "path": str(stderr_path.relative_to(run_dir)),
            "size_bytes": stderr_path.stat().st_size,
            "sha256": _sha256_file(stderr_path),
        },
    }
    state["commands"][name] = record
    return record


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _add_positive_assertion(
    state: dict[str, Any], name: str, passed: bool, *, observed: object, expected: object
) -> None:
    state["positive_assertions"].append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
        }
    )
    if not passed:
        raise ProbeFailureError(f"counterfact failed: {name}")


def _require_command_success(state: dict[str, Any], name: str) -> CommandRecord:
    record = _command_record(state, name)
    if record["timed_out"] or record["exit_code"] != 0:
        raise ProbeFailureError(
            f"command {name} failed: timeout={record['timed_out']} exit={record['exit_code']}"
        )
    return record


def _normalized_requirement(
    requirement_text: str, *, optional_extra: str | None = None
) -> tuple[str, tuple[str, ...], tuple[str, ...], str | None, str | None]:
    """Canonicalize all PEP 508 requirement dimensions used by package metadata."""
    requirement = Requirement(requirement_text)
    marker_parts = [str(requirement.marker)] if requirement.marker is not None else []
    if optional_extra is not None:
        marker_parts.append(f'extra == "{str(canonicalize_name(optional_extra))}"')
    marker = Marker(" and ".join(f"({part})" for part in marker_parts)) if marker_parts else None
    return (
        str(canonicalize_name(requirement.name)),
        tuple(sorted(str(canonicalize_name(extra)) for extra in requirement.extras)),
        tuple(sorted(str(specifier) for specifier in requirement.specifier)),
        requirement.url,
        str(marker) if marker is not None else None,
    )


def _project_dependency_contract(
    pyproject_bytes: bytes,
) -> DependencyContract:
    """Read PEP 621 dependencies and convert extras into metadata markers."""
    pyproject = tomllib.loads(pyproject_bytes.decode("utf-8"))
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject has no [project] table")

    requirements: Counter[RequirementKey] = Counter()
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        raise ValueError("[project].dependencies must be an array of requirement strings")
    requirements.update(_normalized_requirement(item) for item in dependencies)

    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("[project.optional-dependencies] must be a table")
    extras: Counter[str] = Counter()
    for extra_name, extra_dependencies in optional.items():
        if not isinstance(extra_name, str) or not isinstance(extra_dependencies, list):
            raise ValueError("optional dependency groups must map names to arrays")
        normalized_extra = str(canonicalize_name(extra_name))
        extras[normalized_extra] += 1
        if any(not isinstance(item, str) for item in extra_dependencies):
            raise ValueError("optional dependencies must be requirement strings")
        requirements.update(
            _normalized_requirement(item, optional_extra=normalized_extra)
            for item in extra_dependencies
        )
    return requirements, extras


def _counter_as_json(counter: Counter[tuple]) -> list[dict[str, Any]]:
    """Render normalized requirement counters in a stable evidence format."""
    rendered = []
    for key, count in counter.items():
        name, extras, specifiers, url, marker = key
        rendered.append(
            {
                "name": name,
                "extras": list(extras),
                "specifiers": list(specifiers),
                "url": url,
                "marker": marker,
                "count": count,
            }
        )
    return sorted(rendered, key=lambda item: json.dumps(item, sort_keys=True))


def _metadata_dependency_contract(
    metadata_bytes: bytes,
) -> DependencyContract:
    """Parse Requires-Dist and Provides-Extra headers from wheel/PKG-INFO metadata."""
    metadata = BytesParser().parsebytes(metadata_bytes)
    raw_requirements = metadata.get_all("Requires-Dist", [])
    raw_extras = metadata.get_all("Provides-Extra", [])
    requirements: Counter[RequirementKey] = Counter()
    for item in raw_requirements:
        if not isinstance(item, str):
            raise ValueError("Requires-Dist metadata field is not text")
        requirements[_normalized_requirement(item)] += 1
    extras = Counter(str(canonicalize_name(item)) for item in raw_extras if isinstance(item, str))
    if len(extras) != len(raw_extras):
        raise ValueError("Provides-Extra metadata field is not text")
    return requirements, extras


def _compare_dependency_contracts(
    expected: DependencyContract,
    observed: DependencyContract,
) -> dict[str, Any]:
    expected_requirements, expected_extras = expected
    observed_requirements, observed_extras = observed
    missing_requirements = expected_requirements - observed_requirements
    unexpected_requirements = observed_requirements - expected_requirements
    missing_extras = expected_extras - observed_extras
    unexpected_extras = observed_extras - expected_extras
    return {
        "status": "PASS"
        if not any(
            (missing_requirements, unexpected_requirements, missing_extras, unexpected_extras)
        )
        else "FAIL",
        "expected_requires_dist": _counter_as_json(expected_requirements),
        "observed_requires_dist": _counter_as_json(observed_requirements),
        "missing_requires_dist": _counter_as_json(missing_requirements),
        "unexpected_requires_dist": _counter_as_json(unexpected_requirements),
        "expected_provides_extra": sorted(expected_extras.elements()),
        "observed_provides_extra": sorted(observed_extras.elements()),
        "missing_provides_extra": sorted(missing_extras.elements()),
        "unexpected_provides_extra": sorted(unexpected_extras.elements()),
    }


def _failed_dependency_report(error: Exception) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "error_type": type(error).__name__,
        "error": str(error),
        "expected_requires_dist": [],
        "observed_requires_dist": [],
        "missing_requires_dist": [],
        "unexpected_requires_dist": [],
        "expected_provides_extra": [],
        "observed_provides_extra": [],
        "missing_provides_extra": [],
        "unexpected_provides_extra": [],
    }


def _archive_members(artifact: Path, kind: str) -> tuple[set[str], dict[str, bytes]]:
    """Return normalized member names and dependency, license, and vendor payloads."""

    def needs_payload(name: str) -> bool:
        return (
            name == "pyproject.toml"
            or name.startswith(MIT_VENDOR_PREFIX)
            or name.endswith(".dist-info/METADATA")
            or PurePosixPath(name).name
            in {
                "LICENSE",
                "LICENSE-AKSHARE",
                "THIRD_PARTY_NOTICES.md",
                "PKG-INFO",
            }
        )

    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        with ZipFile(artifact) as zip_archive:
            members = set(zip_archive.namelist())
            wheel_content = {
                name: zip_archive.read(name) for name in members if needs_payload(name)
            }
        return members, wheel_content

    with tarfile.open(artifact, "r:gz") as tar_archive:
        names = [tar_member.name for tar_member in tar_archive.getmembers() if tar_member.isfile()]
        if not names:
            raise ProbeFailureError("sdist contains no regular files")
        roots = {name.split("/", maxsplit=1)[0] for name in names if "/" in name}
        if len(roots) != 1:
            raise ProbeFailureError(f"sdist has unexpected top-level roots: {sorted(roots)}")
        prefix = next(iter(roots)) + "/"
        normalized = {name[len(prefix) :] for name in names if name.startswith(prefix)}
        sdist_content: dict[str, bytes] = {}
        for name in normalized:
            if not needs_payload(name):
                continue
            member_stream = tar_archive.extractfile(prefix + name)
            if member_stream is not None:
                sdist_content[name] = member_stream.read()
        return normalized, sdist_content


def _inspect_archive(*, artifact: Path, kind: str, snapshot_root: Path) -> dict[str, Any]:
    """Return deterministic archive checks without asserting the expected outcome."""
    members, archive_content = _archive_members(artifact, kind)
    missing = sorted(set(REQUIRED_PACKAGE_RESOURCES) - members)
    forbidden_prefixes = ("opendata_client/", "opendata_http/", "opendata_fuyao/")
    forbidden = sorted(name for name in members if name.startswith(forbidden_prefixes))
    license_checks: list[dict[str, Any]] = []

    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        metadata_files = [name for name in members if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) == 1:
            dist_info = metadata_files[0].rsplit("/", maxsplit=1)[0]
            required_license_members = {
                f"{dist_info}/licenses/LICENSE": "LICENSE",
                f"{dist_info}/licenses/LICENSE-AKSHARE": "LICENSE-AKSHARE",
                "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE": (
                    "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE"
                ),
            }
        else:
            required_license_members = {}
        distribution_metadata_files = metadata_files
    else:
        metadata_files = []
        distribution_metadata_files = [
            name for name in members if name == "PKG-INFO" or name.endswith(".egg-info/PKG-INFO")
        ]
        required_license_members = {
            "LICENSE": "LICENSE",
            "LICENSE-AKSHARE": "LICENSE-AKSHARE",
            "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE": (
                "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE"
            ),
        }

    for archive_name, source_name in required_license_members.items():
        source_path = snapshot_root / source_name
        expected_hash = _sha256_file(source_path)
        actual_bytes = archive_content.get(archive_name)
        actual_hash = _sha256_bytes(actual_bytes) if actual_bytes is not None else None
        passed = actual_hash == expected_hash
        license_checks.append(
            {
                "archive_member": archive_name,
                "source_file": source_name,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "status": "PASS" if passed else "FAIL",
            }
        )

    optional_notices = [
        name for name in members if PurePosixPath(name).name == "THIRD_PARTY_NOTICES.md"
    ]
    for archive_name in optional_notices:
        payload = archive_content[archive_name]
        expected_hash = _sha256_file(snapshot_root / "THIRD_PARTY_NOTICES.md")
        license_checks.append(
            {
                "archive_member": archive_name,
                "source_file": "THIRD_PARTY_NOTICES.md",
                "expected_sha256": expected_hash,
                "actual_sha256": _sha256_bytes(payload),
                "status": "PASS" if _sha256_bytes(payload) == expected_hash else "FAIL",
            }
        )

    source_pyproject = snapshot_root / "pyproject.toml"
    source_project_bytes = source_pyproject.read_bytes() if source_pyproject.is_file() else None
    metadata_dependency_checks: list[dict[str, Any]] = []
    source_contract: DependencyContract | None = None
    if source_project_bytes is not None:
        try:
            source_contract = _project_dependency_contract(source_project_bytes)
        except Exception as exc:
            metadata_dependency_checks.append(
                {"source_project_error": _failed_dependency_report(exc)}
            )
    else:
        metadata_dependency_checks.append(
            {"source_project_error": _failed_dependency_report(FileNotFoundError("pyproject.toml"))}
        )

    sdist_project_dependency_check: dict[str, Any] | None = None
    if kind == "sdist":
        archive_project_bytes = archive_content.get("pyproject.toml")
        if source_contract is None or archive_project_bytes is None:
            sdist_project_dependency_check = _failed_dependency_report(
                ValueError("source or sdist pyproject.toml is missing or invalid")
            )
        else:
            try:
                archive_contract = _project_dependency_contract(archive_project_bytes)
                sdist_project_dependency_check = _compare_dependency_contracts(
                    source_contract, archive_contract
                )
            except Exception as exc:
                sdist_project_dependency_check = _failed_dependency_report(exc)

    for metadata_name in distribution_metadata_files:
        metadata_bytes = archive_content.get(metadata_name)
        if source_contract is None or metadata_bytes is None:
            check = _failed_dependency_report(
                ValueError(f"distribution metadata is unreadable: {metadata_name}")
            )
        else:
            try:
                check = _compare_dependency_contracts(
                    source_contract,
                    _metadata_dependency_contract(metadata_bytes),
                )
            except Exception as exc:
                check = _failed_dependency_report(exc)
        metadata_dependency_checks.append({"archive_member": metadata_name, **check})

    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        metadata_dependencies_match = (
            len(distribution_metadata_files) == 1
            and len(metadata_dependency_checks) == 1
            and metadata_dependency_checks[0].get("status") == "PASS"
        )
    else:
        metadata_dependencies_match = (
            bool(distribution_metadata_files)
            and sdist_project_dependency_check is not None
            and sdist_project_dependency_check["status"] == "PASS"
            and all(check.get("status") == "PASS" for check in metadata_dependency_checks)
            and len(metadata_dependency_checks) == len(distribution_metadata_files)
        )

    bsl_text_misplacements: list[dict[str, str]] = []
    root_license = snapshot_root / "LICENSE"
    if root_license.is_file():
        bsl_license_text = (
            root_license.read_bytes()
            .decode("utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
            .encode("utf-8")
        )
        if bsl_license_text:
            for archive_name, payload in archive_content.items():
                if not archive_name.startswith(MIT_VENDOR_PREFIX):
                    continue
                try:
                    normalized_payload = (
                        payload.decode("utf-8-sig")
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .strip()
                        .encode("utf-8")
                    )
                except UnicodeDecodeError:
                    continue
                if bsl_license_text in normalized_payload:
                    bsl_text_misplacements.append(
                        {
                            "archive_member": archive_name,
                            "sha256": _sha256_bytes(payload),
                            "match": "complete normalized root LICENSE text",
                        }
                    )
    bsl_text_misplacements.sort(key=lambda item: item["archive_member"])

    license_hashes_match = all(check["status"] == "PASS" for check in license_checks)
    required_license_checks_present = len(license_checks) >= len(required_license_members)
    check_values = {
        "contains_seven_required_resources": not missing,
        "excludes_separate_client_and_removed_packages": not forbidden,
        "license_files_match_source_hashes": (
            license_hashes_match and required_license_checks_present
        ),
        "has_exactly_one_metadata_member": kind not in {"wheel", "sdist_rebuilt_wheel"}
        or len(metadata_files) == 1,
        "metadata_dependencies_match_pyproject": metadata_dependencies_match,
        "sdist_pyproject_dependencies_match_source": kind != "sdist"
        or (
            sdist_project_dependency_check is not None
            and sdist_project_dependency_check["status"] == "PASS"
        ),
        "has_no_full_bsl_license_text_in_mit_vendor_tree": not bsl_text_misplacements,
    }
    rejection_reasons = [name for name, passed in check_values.items() if not passed]
    return {
        "kind": kind,
        "path": str(artifact),
        "size_bytes": artifact.stat().st_size,
        "sha256": _sha256_file(artifact),
        "member_count": len(members),
        "required_package_resources": list(REQUIRED_PACKAGE_RESOURCES),
        "missing_package_resources": missing,
        "forbidden_package_members": forbidden,
        "license_content_hash_checks": license_checks,
        "third_party_notices_members": optional_notices,
        "metadata_dependency_checks": metadata_dependency_checks,
        "sdist_project_dependency_check": sdist_project_dependency_check,
        "bsl_full_text_misplacements": bsl_text_misplacements,
        "checks": check_values,
        "accepted": not rejection_reasons,
        "rejection_reasons": rejection_reasons,
    }


def _validate_archive(
    *,
    state: dict[str, Any],
    artifact: Path,
    kind: str,
    snapshot_root: Path,
) -> dict[str, Any]:
    archive_report = _inspect_archive(
        artifact=artifact,
        kind=kind,
        snapshot_root=snapshot_root,
    )
    state["archives"][kind] = archive_report
    _add_positive_assertion(
        state,
        f"{kind}_contains_seven_required_resources",
        archive_report["checks"]["contains_seven_required_resources"],
        observed={"missing": archive_report["missing_package_resources"]},
        expected="all seven required resource members",
    )
    _add_positive_assertion(
        state,
        f"{kind}_excludes_separate_client_and_removed_packages",
        archive_report["checks"]["excludes_separate_client_and_removed_packages"],
        observed={"forbidden_members": archive_report["forbidden_package_members"]},
        expected="no opendata_client, opendata_http, or opendata_fuyao members",
    )
    _add_positive_assertion(
        state,
        f"{kind}_license_files_match_source_hashes",
        archive_report["checks"]["license_files_match_source_hashes"],
        observed=archive_report["license_content_hash_checks"],
        expected="required license payloads byte-match the source snapshot",
    )
    _add_positive_assertion(
        state,
        f"{kind}_metadata_dependencies_match_pyproject",
        archive_report["checks"]["metadata_dependencies_match_pyproject"],
        observed=archive_report["metadata_dependency_checks"],
        expected="Requires-Dist and Provides-Extra match normalized PEP 621 declarations",
    )
    _add_positive_assertion(
        state,
        f"{kind}_has_no_full_bsl_license_text_in_mit_vendor_tree",
        archive_report["checks"]["has_no_full_bsl_license_text_in_mit_vendor_tree"],
        observed=archive_report["bsl_full_text_misplacements"],
        expected="complete root BSL license text is absent from the MIT vendor subtree",
    )
    if kind == "sdist":
        _add_positive_assertion(
            state,
            "sdist_pyproject_dependencies_match_source",
            archive_report["checks"]["sdist_pyproject_dependencies_match_source"],
            observed=archive_report["sdist_project_dependency_check"],
            expected="sdist pyproject dependency declarations match the source snapshot",
        )
    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        _add_positive_assertion(
            state,
            f"{kind}_has_exactly_one_metadata_member",
            archive_report["checks"]["has_exactly_one_metadata_member"],
            observed=archive_report["checks"]["has_exactly_one_metadata_member"],
            expected=True,
        )
    return archive_report


def _copy_artifact(artifact: Path, artifacts_dir: Path, *, stored_name: str | None = None) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    destination = artifacts_dir / (stored_name or artifact.name)
    shutil.copy2(artifact, destination)
    return destination


def _smoke_code() -> str:
    """Return the installed-only check; expected values arrive through environment."""
    return r"""import hashlib, importlib, importlib.metadata, importlib.resources, json, os, sys
from pathlib import Path

site = Path(os.environ["OPENDATA_PROBE_SITE"]).resolve()
checkout = Path(os.environ["OPENDATA_PROBE_CHECKOUT"]).resolve()
snapshot = Path(os.environ["OPENDATA_PROBE_SNAPSHOT"]).resolve()
expected = json.loads(os.environ["OPENDATA_PROBE_RESOURCE_HASHES"])

blocked_dbapi_roots = {
    "_sqlite3", "aiomysql", "aiosqlite", "asyncpg", "cx_oracle", "databases",
    "duckdb", "mariadb", "motor", "mysql", "oracledb", "peewee", "psycopg",
    "psycopg2", "pyodbc", "pymongo", "pymysql", "sqlalchemy", "sqlmodel",
    "sqlite3", "tortoise",
}
blocked_entrypoint_names = {"main", "lifespan"}
blocked_process_events = {
    "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.posix_spawnp",
    "os.spawn", "os.startfile", "os.system", "subprocess.Popen",
}
attempt_counts = {
    "database": 0,
    "entrypoint_import": 0,
    "process": 0,
    "socket": 0,
}
blocked_attempts = []

def reject_audit_event(category, event, target=""):
    attempt_counts[category] += 1
    blocked_attempts.append({"category": category, "event": event, "target": target[:256]})
    raise RuntimeError(f"installed smoke guard rejected {category} audit event")

def audit_guard(event, arguments):
    if event.startswith("socket."):
        reject_audit_event("socket", event)
    if event == "sqlite3.connect" or event.startswith("sqlite3."):
        reject_audit_event("database", event)
    if (
        event in blocked_process_events
        or event.startswith("os.exec")
        or event.startswith("os.posix_spawn")
        or event.startswith("os.spawn")
    ):
        reject_audit_event("process", event)
    if event == "import" and arguments and isinstance(arguments[0], str):
        module_name = arguments[0]
        parts = module_name.lower().split(".")
        if parts[0] in blocked_dbapi_roots:
            reject_audit_event("database", event, module_name)
        if parts[-1] in blocked_entrypoint_names:
            reject_audit_event("entrypoint_import", event, module_name)

class ImportBlocker:
    def find_spec(self, fullname, path=None, target=None):
        parts = fullname.lower().split(".")
        if parts[0] in blocked_dbapi_roots:
            reject_audit_event("database", "import.meta_path", fullname)
        if parts[-1] in blocked_entrypoint_names:
            reject_audit_event("entrypoint_import", "import.meta_path", fullname)
        return None

guard_installed = False
guard_install_error = None
try:
    sys.addaudithook(audit_guard)
    sys.meta_path.insert(0, ImportBlocker())
    guard_installed = True
except BaseException as error:
    guard_install_error = type(error).__name__

product_import_names = (
    "opendata",
    "opendata.data",
    "opendata.data.providers.akshare._vendor",
)
product_import_errors = []
product_imports_started = False
guard_installed_before_product_imports = guard_installed
if guard_installed:
    product_imports_started = True
    for name in product_import_names:
        try:
            importlib.import_module(name)
        except BaseException as error:
            product_import_errors.append({"module": name, "error_type": type(error).__name__})

search_paths = [Path(item or os.getcwd()).resolve() for item in sys.path]
leaked = [
    str(item)
    for item in search_paths
    if item == checkout or checkout in item.parents or item == snapshot or snapshot in item.parents
]

smoke_errors = []
if not guard_installed:
    smoke_errors.append("I/O guard was not installed before product imports")
if guard_install_error is not None:
    smoke_errors.append(f"I/O guard installation failed: {guard_install_error}")
if leaked:
    smoke_errors.append("source checkout/snapshot leaked onto sys.path")

product_modules = {}
for name, module in sorted(tuple(sys.modules.items())):
    if name != "opendata" and not name.startswith("opendata."):
        continue
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str):
        module_spec = getattr(module, "__spec__", None)
        raw_path = getattr(module_spec, "origin", None)
    if not isinstance(raw_path, str) or raw_path in {"built-in", "frozen"}:
        smoke_errors.append(f"product module has no file origin: {name}")
        continue
    module_path = Path(raw_path).resolve()
    if not module_path.is_file():
        smoke_errors.append(f"product module origin is not a file: {name}")
        continue
    product_modules[name] = {
        "path": str(module_path),
        "sha256": hashlib.sha256(module_path.read_bytes()).hexdigest(),
    }

opendata_module = product_modules.get("opendata", {}).get("path", "")
if opendata_module:
    if site not in Path(opendata_module).parents:
        smoke_errors.append("opendata imported outside the isolated install")
else:
    smoke_errors.append("opendata module did not import from a file")

observed = {}
if guard_installed and isinstance(expected, dict):
    try:
        for relative, expected_hash in expected.items():
            if not isinstance(relative, str) or not isinstance(expected_hash, str):
                raise TypeError("resource expectations must map text paths to text hashes")
            resource = importlib.resources.files("opendata").joinpath(*relative.split("/"))
            if not resource.is_file():
                raise FileNotFoundError(f"installed resource missing: {relative}")
            actual_hash = hashlib.sha256(resource.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError(f"installed resource hash mismatch: {relative}")
            observed[relative] = actual_hash
    except BaseException as error:
        smoke_errors.append(f"installed resource check failed: {type(error).__name__}")
elif guard_installed:
    smoke_errors.append("resource expectations are not an object")

distribution_version = None
try:
    distribution_version = importlib.metadata.distribution("opendata").version
except BaseException as error:
    smoke_errors.append("distribution metadata check failed: " + type(error).__name__)

attempts_total = sum(attempt_counts.values())
report = {
    "schema": "opendata-installed-smoke/v2",
    "base_python_executable": sys.executable,
    "base_python_version": sys.version,
    "opendata_module": opendata_module,
    "isolated_site_packages": str(site),
    "distribution_version": distribution_version,
    "sys_path_source_leaks": leaked,
    "resources": observed,
    "product_import_names": list(product_import_names),
    "product_import_errors": product_import_errors,
    "product_modules": product_modules,
    "smoke_errors": smoke_errors,
    "io_guard": {
        "installed": guard_installed,
        "installed_before_product_imports": guard_installed_before_product_imports,
        "product_imports_started_after_guard": (
            product_imports_started and guard_installed_before_product_imports
        ),
        "installation_error_type": guard_install_error,
        "attempt_counts": attempt_counts,
        "attempts_total": attempts_total,
        "blocked_attempts": blocked_attempts,
        "coverage": {
            "socket_audit_events": "socket.*",
            "process_audit_events": [
                "os.exec*", "os.fork", "os.forkpty", "os.posix_spawn*", "os.spawn*",
                "os.startfile", "os.system", "subprocess.Popen",
            ],
            "database_audit_events": "sqlite3.*",
            "blocked_dbapi_import_roots": sorted(blocked_dbapi_roots),
            "blocked_entrypoint_module_names": sorted(blocked_entrypoint_names),
            "module_import_blocker": "sys.meta_path.find_spec",
            "pip_network_blocked": False,
        },
    },
}
print(json.dumps(report, sort_keys=True))
if attempts_total or product_import_errors or smoke_errors:
    sys.exit(1)
"""


def _probe_site_packages(venv_root: Path) -> Path:
    if os.name == "nt":
        return venv_root / "Lib" / "site-packages"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return venv_root / "lib" / version / "site-packages"


def _validate_smoke_guard(smoke_report: JsonObject) -> dict[str, int]:
    """Require evidence that the child I/O guard preceded imports and blocked nothing."""
    raw_guard = smoke_report.get("io_guard")
    if not isinstance(raw_guard, dict):
        raise ProbeFailureError("installed smoke report lacks its I/O guard record")
    if (
        raw_guard.get("installed") is not True
        or raw_guard.get("installed_before_product_imports") is not True
        or raw_guard.get("product_imports_started_after_guard") is not True
        or raw_guard.get("installation_error_type") is not None
    ):
        raise ProbeFailureError("installed smoke guard was not installed before product imports")
    if raw_guard.get("coverage") != SMOKE_GUARD_COVERAGE:
        raise ProbeFailureError("installed smoke guard coverage report does not match its policy")

    raw_counts = raw_guard.get("attempt_counts")
    if not isinstance(raw_counts, dict) or set(raw_counts) != set(SMOKE_GUARD_COUNTERS):
        raise ProbeFailureError("installed smoke guard attempt counters are incomplete")
    counts: dict[str, int] = {}
    for category in SMOKE_GUARD_COUNTERS:
        value = raw_counts.get(category)
        if type(value) is not int or value < 0:
            raise ProbeFailureError(
                "installed smoke guard attempt counter is not a non-negative integer"
            )
        counts[category] = value

    total = raw_guard.get("attempts_total")
    raw_attempts = raw_guard.get("blocked_attempts")
    if type(total) is not int or total < 0 or not isinstance(raw_attempts, list):
        raise ProbeFailureError("installed smoke guard attempt evidence is malformed")
    observed_counts = dict.fromkeys(SMOKE_GUARD_COUNTERS, 0)
    for raw_attempt in raw_attempts:
        if not isinstance(raw_attempt, dict):
            raise ProbeFailureError("installed smoke guard blocked-attempt entry is malformed")
        attempt_category = raw_attempt.get("category")
        event = raw_attempt.get("event")
        target = raw_attempt.get("target")
        if (
            not isinstance(attempt_category, str)
            or attempt_category not in observed_counts
            or not isinstance(event, str)
            or not isinstance(target, str)
        ):
            raise ProbeFailureError("installed smoke guard blocked-attempt entry is incomplete")
        observed_counts[attempt_category] += 1
    if total != sum(counts.values()) or len(raw_attempts) != total or observed_counts != counts:
        raise ProbeFailureError("installed smoke guard counters disagree with blocked attempts")
    if total != 0:
        raise ProbeFailureError(f"installed smoke guard blocked I/O attempts: {counts}")
    return counts


def _validate_smoke_product_modules(
    smoke_report: JsonObject, venv_site: Path
) -> dict[str, JsonObject]:
    """Verify every reported product module maps to an installed file and matching SHA."""
    raw_modules = smoke_report.get("product_modules")
    if not isinstance(raw_modules, dict) or not raw_modules:
        raise ProbeFailureError("installed smoke report lacks product module file identities")
    modules: dict[str, JsonObject] = {}
    for name, raw_module in raw_modules.items():
        if (
            not isinstance(name, str)
            or (name != "opendata" and not name.startswith("opendata."))
            or not isinstance(raw_module, dict)
        ):
            raise ProbeFailureError("installed smoke product module identity is malformed")
        path_value = raw_module.get("path")
        digest = raw_module.get("sha256")
        if (
            not isinstance(path_value, str)
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ProbeFailureError(
                "installed smoke product module identity lacks a path or SHA-256"
            )
        module_path = Path(path_value)
        if (
            not module_path.is_absolute()
            or not _is_within(module_path, venv_site)
            or not module_path.is_file()
            or _sha256_file(module_path) != digest
        ):
            raise ProbeFailureError(
                f"installed smoke product module file identity mismatch: {name}"
            )
        modules[name] = {"path": str(module_path.resolve()), "sha256": digest}
    if smoke_report.get("opendata_module") != modules.get("opendata", {}).get("path"):
        raise ProbeFailureError(
            "installed smoke opendata module path disagrees with its module map"
        )
    return modules


def _run_smoke(
    *,
    state: dict[str, Any],
    stage: str,
    venv_site: Path,
    snapshot_root: Path,
    work_dir: Path,
    resource_hashes: dict[str, str],
    timeout_seconds: int,
    run_dir: Path,
) -> JsonObject:
    environment = _clean_environment()
    environment.update(
        {
            "PYTHONPATH": str(venv_site),
            "OPENDATA_PROBE_SITE": str(venv_site),
            "OPENDATA_PROBE_CHECKOUT": str(PROJECT_ROOT),
            "OPENDATA_PROBE_SNAPSHOT": str(snapshot_root),
            "OPENDATA_PROBE_RESOURCE_HASHES": json.dumps(resource_hashes, sort_keys=True),
        }
    )
    record = _run_command(
        state=state,
        name=stage,
        command=[sys.executable, "-S", "-c", _smoke_code()],
        cwd=work_dir,
        environment=environment,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    stdout_path = run_dir / record["stdout"]["path"]
    try:
        smoke_report = _parse_json_object(
            stdout_path.read_text(encoding="utf-8"), f"{stage} installed smoke report"
        )
    except ProbeFailureError as exc:
        raise ProbeFailureError(f"{stage} did not emit the installed smoke JSON") from exc
    state["installations"][stage] = smoke_report
    if smoke_report.get("schema") != SMOKE_REPORT_SCHEMA:
        raise ProbeFailureError(f"{stage} installed smoke report has an unknown schema")
    if smoke_report.get("product_import_names") != list(SMOKE_PRODUCT_IMPORTS):
        raise ProbeFailureError(f"{stage} installed smoke report has unexpected product imports")
    if smoke_report.get("base_python_executable") != sys.executable:
        raise ProbeFailureError(f"{stage} smoke did not report the selected base interpreter")
    if not isinstance(smoke_report.get("base_python_version"), str):
        raise ProbeFailureError(f"{stage} smoke omitted the base interpreter version")
    if smoke_report.get("isolated_site_packages") != str(venv_site.resolve()):
        raise ProbeFailureError(f"{stage} smoke used an unexpected site-packages directory")
    distribution_version = smoke_report.get("distribution_version")
    if not isinstance(distribution_version, str) or not distribution_version:
        raise ProbeFailureError(f"{stage} smoke omitted installed distribution metadata")
    raw_resources = smoke_report.get("resources")
    if not isinstance(raw_resources, dict) or raw_resources != resource_hashes:
        raise ProbeFailureError(f"{stage} installed resource report does not match expectations")
    raw_leaks = smoke_report.get("sys_path_source_leaks")
    if not isinstance(raw_leaks, list) or any(not isinstance(item, str) for item in raw_leaks):
        raise ProbeFailureError(f"{stage} source path leakage report is malformed")
    if raw_leaks:
        raise ProbeFailureError(f"{stage} detected source path leakage")
    guard_counts = _validate_smoke_guard(smoke_report)
    product_modules = _validate_smoke_product_modules(smoke_report, venv_site)
    import_errors = smoke_report.get("product_import_errors")
    smoke_errors = smoke_report.get("smoke_errors")
    if not isinstance(import_errors, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("module"), str)
        or not isinstance(item.get("error_type"), str)
        for item in import_errors
    ):
        raise ProbeFailureError(f"{stage} product import error report is malformed")
    if not isinstance(smoke_errors, list) or any(
        not isinstance(item, str) for item in smoke_errors
    ):
        raise ProbeFailureError(f"{stage} smoke error report is malformed")
    if import_errors or smoke_errors:
        raise ProbeFailureError(
            f"{stage} reported smoke errors: imports={import_errors}, checks={smoke_errors}"
        )
    _require_command_success(state, stage)
    _add_positive_assertion(
        state,
        f"{stage}_io_guard_installed_before_product_imports",
        True,
        observed={"attempt_counts": guard_counts, "coverage": SMOKE_GUARD_COVERAGE},
        expected="guard precedes product imports and records zero denied I/O attempts",
    )
    _add_positive_assertion(
        state,
        f"{stage}_product_module_paths_and_hashes_match_installed_files",
        True,
        observed=product_modules,
        expected="every imported opendata module resolves to an installed file with its SHA-256",
    )
    _add_positive_assertion(
        state,
        f"{stage}_imports_installed_package_without_source_leak",
        bool(smoke_report.get("opendata_module")) and not raw_leaks,
        observed=smoke_report,
        expected="module imports from target venv; checkout and snapshot absent from sys.path",
    )
    _add_positive_assertion(
        state,
        f"{stage}_installed_resources_match_source",
        smoke_report.get("resources") == resource_hashes,
        observed=smoke_report.get("resources"),
        expected=resource_hashes,
    )
    return smoke_report


def _write_wheel_copy(
    artifact: Path,
    destination: Path,
    *,
    remove_member: str | None = None,
    replacements: dict[str, bytes] | None = None,
    additions: dict[str, bytes] | None = None,
) -> None:
    """Rewrite a wheel copy while leaving its input artifact untouched."""
    if artifact.resolve() == destination.resolve():
        raise ProbeFailureError("counterfactual destination must differ from its source artifact")
    replacements = replacements or {}
    additions = additions or {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    with ZipFile(artifact, "r") as source, ZipFile(destination, "w") as target:
        for info in source.infolist():
            name = info.filename
            seen.add(name)
            if name == remove_member:
                continue
            payload = replacements.get(name, source.read(name))
            target.writestr(info, payload)
        conflicts = set(additions) & seen
        if conflicts:
            destination.unlink(missing_ok=True)
            raise ProbeFailureError(f"counterfactual members already exist: {sorted(conflicts)}")
        for name, payload in additions.items():
            target.writestr(name, payload)
    requested = ({remove_member} if remove_member else set()) | set(replacements)
    missing = requested - seen
    if missing:
        destination.unlink(missing_ok=True)
        raise ProbeFailureError(f"counterfactual wheel member is absent: {sorted(missing)}")


def _write_sdist_copy(
    artifact: Path,
    destination: Path,
    *,
    remove_member: str | None = None,
    replacements: dict[str, bytes] | None = None,
    additions: dict[str, bytes] | None = None,
) -> None:
    """Rewrite an sdist copy while preserving the source archive."""
    if artifact.resolve() == destination.resolve():
        raise ProbeFailureError("counterfactual destination must differ from its source artifact")
    replacements = replacements or {}
    additions = additions or {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as source:
        members = source.getmembers()
        file_names = [member.name for member in members if member.isfile()]
        roots = {name.split("/", maxsplit=1)[0] for name in file_names if "/" in name}
        if len(roots) != 1:
            raise ProbeFailureError(f"sdist has unexpected top-level roots: {sorted(roots)}")
        prefix = next(iter(roots)) + "/"
        seen = {name[len(prefix) :] for name in file_names if name.startswith(prefix)}
        conflicts = set(additions) & seen
        if conflicts:
            raise ProbeFailureError(f"counterfactual members already exist: {sorted(conflicts)}")
        requested = ({remove_member} if remove_member else set()) | set(replacements)
        missing = requested - seen
        if missing:
            raise ProbeFailureError(f"counterfactual sdist member is absent: {sorted(missing)}")
        with tarfile.open(destination, "w:gz") as target:
            for member in members:
                relative = (
                    member.name[len(prefix) :] if member.name.startswith(prefix) else member.name
                )
                if relative == remove_member:
                    continue
                copied_member = copy.copy(member)
                if not member.isfile():
                    target.addfile(copied_member)
                    continue
                source_file = source.extractfile(member)
                if source_file is None:
                    raise ProbeFailureError(f"sdist member cannot be read: {member.name}")
                payload = replacements[relative] if relative in replacements else source_file.read()
                copied_member.size = len(payload)
                target.addfile(copied_member, io.BytesIO(payload))
            for relative, payload in additions.items():
                member = tarfile.TarInfo(prefix + relative)
                member.size = len(payload)
                member.mode = 0o644
                member.mtime = 0
                target.addfile(member, io.BytesIO(payload))


def _remove_metadata_dependency(
    metadata_bytes: bytes, package_name: str
) -> tuple[bytes, list[str]]:
    """Drop all base Requires-Dist entries for one package from an artifact copy."""
    lines = metadata_bytes.splitlines(keepends=True)
    output: list[bytes] = []
    removed: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.lower().startswith(b"requires-dist:"):
            output.append(line)
            index += 1
            continue
        end = index + 1
        while end < len(lines) and lines[end][:1] in {b" ", b"\t"}:
            end += 1
        header = b" ".join(part.strip() for part in lines[index:end])
        requirement_text = header.split(b":", maxsplit=1)[1].decode("utf-8").strip()
        normalized = _normalized_requirement(requirement_text)
        if normalized[0] == canonicalize_name(package_name) and normalized[4] is None:
            removed.append(requirement_text)
        else:
            output.extend(lines[index:end])
        index = end
    if not removed:
        raise ProbeFailureError(f"metadata has no base Requires-Dist for {package_name}")
    return b"".join(output), removed


def _append_counterfactual(
    state: dict[str, Any],
    *,
    name: str,
    passed: bool,
    mutation: dict[str, Any],
    expected: dict[str, Any],
    observed: dict[str, Any],
) -> dict[str, Any]:
    record = {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "mutation": mutation,
        "expected": expected,
        "observed": observed,
    }
    state["counterfactuals"].append(record)
    if not passed:
        raise ProbeFailureError(f"counterfactual was not rejected as expected: {name}")
    return record


def _run_archive_counterfactuals(
    *,
    state: dict[str, Any],
    artifact: Path,
    snapshot_root: Path,
    run_dir: Path,
    sdist_artifact: Path | None = None,
) -> None:
    """Prove the checker rejects wheel/sdist dependency and license mutations."""
    original_hash = _sha256_file(artifact)
    baseline = _inspect_archive(artifact=artifact, kind="wheel", snapshot_root=snapshot_root)
    state.setdefault("positive_assertions", [])
    _add_positive_assertion(
        state,
        "counterfactual_baseline_wheel_is_accepted",
        baseline["accepted"],
        observed={
            "artifact_sha256": original_hash,
            "checks": baseline["checks"],
        },
        expected={"accepted": True, "checks": dict.fromkeys(baseline["checks"], True)},
    )

    resource_member = REQUIRED_PACKAGE_RESOURCES[0]
    resource_copy = run_dir / "counterfactual-artifacts" / "wheel-with-resource-removed.whl"
    _write_wheel_copy(artifact, resource_copy, remove_member=resource_member)
    resource_report = _inspect_archive(
        artifact=resource_copy,
        kind="wheel",
        snapshot_root=snapshot_root,
    )
    original_unchanged = _sha256_file(artifact) == original_hash
    resource_reasons = resource_report["rejection_reasons"]
    _append_counterfactual(
        state,
        name="wheel_required_resource_removed_rejected",
        passed=(
            not resource_report["accepted"]
            and "contains_seven_required_resources" in resource_reasons
            and original_unchanged
        ),
        mutation={
            "kind": "remove_archive_member",
            "member": resource_member,
            "source_artifact_sha256": original_hash,
            "counterfactual_artifact": str(resource_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(resource_copy),
        },
        expected={
            "checker_rejected": True,
            "rejection_check": "contains_seven_required_resources",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not resource_report["accepted"],
            "rejection_reasons": resource_reasons,
            "missing_package_resources": resource_report["missing_package_resources"],
            "source_artifact_unchanged": original_unchanged,
        },
    )

    license_member = "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE"
    license_copy = run_dir / "counterfactual-artifacts" / "wheel-with-license-mismatch.whl"
    _write_wheel_copy(
        artifact,
        license_copy,
        replacements={license_member: b"intentionally mismatched license bytes\n"},
    )
    license_report = _inspect_archive(
        artifact=license_copy,
        kind="wheel",
        snapshot_root=snapshot_root,
    )
    original_unchanged = _sha256_file(artifact) == original_hash
    license_reasons = license_report["rejection_reasons"]
    mismatched_license = [
        check
        for check in license_report["license_content_hash_checks"]
        if check["archive_member"] == license_member
    ]
    _append_counterfactual(
        state,
        name="wheel_license_bytes_replaced_rejected",
        passed=(
            not license_report["accepted"]
            and "license_files_match_source_hashes" in license_reasons
            and len(mismatched_license) == 1
            and mismatched_license[0]["status"] == "FAIL"
            and original_unchanged
        ),
        mutation={
            "kind": "replace_archive_member_bytes",
            "member": license_member,
            "replacement_sha256": _sha256_bytes(b"intentionally mismatched license bytes\n"),
            "source_artifact_sha256": original_hash,
            "counterfactual_artifact": str(license_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(license_copy),
        },
        expected={
            "checker_rejected": True,
            "rejection_check": "license_files_match_source_hashes",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not license_report["accepted"],
            "rejection_reasons": license_reasons,
            "license_hash_check": mismatched_license,
            "source_artifact_unchanged": original_unchanged,
        },
    )

    metadata_member = baseline["metadata_dependency_checks"][0]["archive_member"]
    with ZipFile(artifact) as archive:
        metadata_bytes = archive.read(metadata_member)
    omitted_metadata, removed_requirements = _remove_metadata_dependency(metadata_bytes, "requests")
    dependency_copy = (
        run_dir / "counterfactual-artifacts" / "wheel-without-requests-requirement.whl"
    )
    _write_wheel_copy(
        artifact,
        dependency_copy,
        replacements={metadata_member: omitted_metadata},
    )
    dependency_report = _inspect_archive(
        artifact=dependency_copy,
        kind="wheel",
        snapshot_root=snapshot_root,
    )
    dependency_reasons = dependency_report["rejection_reasons"]
    dependency_metadata = dependency_report["metadata_dependency_checks"][0]
    original_unchanged = _sha256_file(artifact) == original_hash
    _append_counterfactual(
        state,
        name="wheel_required_runtime_dependency_removed_rejected",
        passed=(
            not dependency_report["accepted"]
            and "metadata_dependencies_match_pyproject" in dependency_reasons
            and any(
                item["name"] == "requests" for item in dependency_metadata["missing_requires_dist"]
            )
            and original_unchanged
        ),
        mutation={
            "kind": "remove_metadata_requires_dist",
            "member": metadata_member,
            "removed_requires_dist": removed_requirements,
            "source_artifact_sha256": original_hash,
            "counterfactual_artifact": str(dependency_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(dependency_copy),
        },
        expected={
            "checker_rejected": True,
            "missing_runtime_dependency": "requests>=2.31.0",
            "rejection_check": "metadata_dependencies_match_pyproject",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not dependency_report["accepted"],
            "rejection_reasons": dependency_reasons,
            "dependency_check": dependency_metadata,
            "source_artifact_unchanged": original_unchanged,
        },
    )

    bsl_member = f"{MIT_VENDOR_PREFIX}BSL-LICENSE"
    bsl_license_bytes = (snapshot_root / "LICENSE").read_bytes()
    bsl_copy = run_dir / "counterfactual-artifacts" / "wheel-with-bsl-license-in-mit-vendor.whl"
    _write_wheel_copy(
        artifact,
        bsl_copy,
        additions={bsl_member: bsl_license_bytes},
    )
    bsl_report = _inspect_archive(artifact=bsl_copy, kind="wheel", snapshot_root=snapshot_root)
    bsl_reasons = bsl_report["rejection_reasons"]
    original_unchanged = _sha256_file(artifact) == original_hash
    detected_bsl_members = bsl_report["bsl_full_text_misplacements"]
    _append_counterfactual(
        state,
        name="wheel_full_bsl_license_in_mit_vendor_tree_rejected",
        passed=(
            not bsl_report["accepted"]
            and "has_no_full_bsl_license_text_in_mit_vendor_tree" in bsl_reasons
            and any(item["archive_member"] == bsl_member for item in detected_bsl_members)
            and original_unchanged
        ),
        mutation={
            "kind": "add_full_root_license_under_mit_vendor_tree",
            "member": bsl_member,
            "payload_sha256": _sha256_bytes(bsl_license_bytes),
            "source_artifact_sha256": original_hash,
            "counterfactual_artifact": str(bsl_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(bsl_copy),
        },
        expected={
            "checker_rejected": True,
            "rejection_check": "has_no_full_bsl_license_text_in_mit_vendor_tree",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not bsl_report["accepted"],
            "rejection_reasons": bsl_reasons,
            "misplaced_bsl_license_members": detected_bsl_members,
            "source_artifact_unchanged": original_unchanged,
        },
    )

    if sdist_artifact is None:
        return
    sdist_hash = _sha256_file(sdist_artifact)
    sdist_baseline = _inspect_archive(
        artifact=sdist_artifact,
        kind="sdist",
        snapshot_root=snapshot_root,
    )
    _add_positive_assertion(
        state,
        "counterfactual_baseline_sdist_is_accepted",
        sdist_baseline["accepted"],
        observed={"artifact_sha256": sdist_hash, "checks": sdist_baseline["checks"]},
        expected={"accepted": True, "checks": dict.fromkeys(sdist_baseline["checks"], True)},
    )

    _, sdist_contents = _archive_members(sdist_artifact, "sdist")
    sdist_metadata_member = "PKG-INFO"
    sdist_metadata_bytes = sdist_contents.get(sdist_metadata_member)
    if sdist_metadata_bytes is None:
        raise ProbeFailureError("retained sdist has no root PKG-INFO member")
    sdist_metadata_mutation, sdist_removed_requirements = _remove_metadata_dependency(
        sdist_metadata_bytes, "requests"
    )
    sdist_dependency_copy = (
        run_dir / "counterfactual-artifacts" / "sdist-without-requests-requirement.tar.gz"
    )
    _write_sdist_copy(
        sdist_artifact,
        sdist_dependency_copy,
        replacements={sdist_metadata_member: sdist_metadata_mutation},
    )
    sdist_dependency_report = _inspect_archive(
        artifact=sdist_dependency_copy,
        kind="sdist",
        snapshot_root=snapshot_root,
    )
    sdist_dependency_reasons = sdist_dependency_report["rejection_reasons"]
    source_sdist_unchanged = _sha256_file(sdist_artifact) == sdist_hash
    sdist_pkg_checks = [
        check
        for check in sdist_dependency_report["metadata_dependency_checks"]
        if check.get("archive_member") == "PKG-INFO"
    ]
    _append_counterfactual(
        state,
        name="sdist_required_runtime_dependency_removed_rejected",
        passed=(
            not sdist_dependency_report["accepted"]
            and "metadata_dependencies_match_pyproject" in sdist_dependency_reasons
            and any(
                item["name"] == "requests"
                for check in sdist_pkg_checks
                for item in check["missing_requires_dist"]
            )
            and source_sdist_unchanged
        ),
        mutation={
            "kind": "remove_pkg_info_requires_dist",
            "member": sdist_metadata_member,
            "removed_requires_dist": sdist_removed_requirements,
            "source_artifact_sha256": sdist_hash,
            "counterfactual_artifact": str(sdist_dependency_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(sdist_dependency_copy),
        },
        expected={
            "checker_rejected": True,
            "missing_runtime_dependency": "requests>=2.31.0",
            "rejection_check": "metadata_dependencies_match_pyproject",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not sdist_dependency_report["accepted"],
            "rejection_reasons": sdist_dependency_reasons,
            "dependency_checks": sdist_pkg_checks,
            "source_artifact_unchanged": source_sdist_unchanged,
        },
    )

    sdist_bsl_copy = run_dir / "counterfactual-artifacts" / "sdist-with-bsl-in-mit-vendor.tar.gz"
    _write_sdist_copy(
        sdist_artifact,
        sdist_bsl_copy,
        additions={bsl_member: bsl_license_bytes},
    )
    sdist_bsl_report = _inspect_archive(
        artifact=sdist_bsl_copy,
        kind="sdist",
        snapshot_root=snapshot_root,
    )
    sdist_bsl_reasons = sdist_bsl_report["rejection_reasons"]
    source_sdist_unchanged = _sha256_file(sdist_artifact) == sdist_hash
    _append_counterfactual(
        state,
        name="sdist_full_bsl_license_in_mit_vendor_tree_rejected",
        passed=(
            not sdist_bsl_report["accepted"]
            and "has_no_full_bsl_license_text_in_mit_vendor_tree" in sdist_bsl_reasons
            and any(
                item["archive_member"] == bsl_member
                for item in sdist_bsl_report["bsl_full_text_misplacements"]
            )
            and source_sdist_unchanged
        ),
        mutation={
            "kind": "add_full_root_license_under_mit_vendor_tree",
            "member": bsl_member,
            "payload_sha256": _sha256_bytes(bsl_license_bytes),
            "source_artifact_sha256": sdist_hash,
            "counterfactual_artifact": str(sdist_bsl_copy.relative_to(run_dir)),
            "counterfactual_artifact_sha256": _sha256_file(sdist_bsl_copy),
        },
        expected={
            "checker_rejected": True,
            "rejection_check": "has_no_full_bsl_license_text_in_mit_vendor_tree",
            "source_artifact_unchanged": True,
        },
        observed={
            "checker_rejected": not sdist_bsl_report["accepted"],
            "rejection_reasons": sdist_bsl_reasons,
            "misplaced_bsl_license_members": sdist_bsl_report["bsl_full_text_misplacements"],
            "source_artifact_unchanged": source_sdist_unchanged,
        },
    )


def _run_source_path_pollution_counterfactual(
    *,
    state: dict[str, Any],
    stage: str,
    venv_site: Path,
    snapshot_root: Path,
    work_dir: Path,
    resource_hashes: dict[str, str],
    timeout_seconds: int,
    run_dir: Path,
) -> None:
    """Run the installed smoke with an intentional source path leak."""
    environment = _clean_environment()
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(venv_site), str(snapshot_root))),
            "OPENDATA_PROBE_SITE": str(venv_site),
            "OPENDATA_PROBE_CHECKOUT": str(PROJECT_ROOT),
            "OPENDATA_PROBE_SNAPSHOT": str(snapshot_root),
            "OPENDATA_PROBE_RESOURCE_HASHES": json.dumps(resource_hashes, sort_keys=True),
        }
    )
    command_record = _run_command(
        state=state,
        name=stage,
        command=[sys.executable, "-S", "-c", _smoke_code()],
        cwd=work_dir,
        environment=environment,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    output = "\n".join(
        (run_dir / command_record[stream]["path"]).read_text(encoding="utf-8")
        for stream in ("stdout", "stderr")
    )
    leak_marker = "source checkout/snapshot leaked onto sys.path"
    observed_rejection = (
        not command_record["timed_out"]
        and command_record["exit_code"] not in (None, 0)
        and leak_marker in output
    )
    _append_counterfactual(
        state,
        name="installed_smoke_rejects_source_path_pollution",
        passed=observed_rejection,
        mutation={
            "kind": "append_source_snapshot_to_pythonpath",
            "pythonpath_order": [str(venv_site), str(snapshot_root)],
        },
        expected={
            "checker_rejected": True,
            "rejection_marker": leak_marker,
            "command_exit_code": "nonzero",
        },
        observed={
            "checker_rejected": observed_rejection,
            "rejection_marker_seen": leak_marker in output,
            "command_exit_code": command_record["exit_code"],
            "timed_out": command_record["timed_out"],
            "stdout": command_record["stdout"],
            "stderr": command_record["stderr"],
        },
    )


def _run_counterfactual_suite(
    *,
    state: dict[str, Any],
    artifact: Path,
    snapshot_root: Path,
    venv_site: Path,
    work_dir: Path,
    resource_hashes: dict[str, str],
    timeout_seconds: int,
    run_dir: Path,
    sdist_artifact: Path | None = None,
) -> None:
    """Exercise archive and installed-smoke rejection using an unchanged wheel."""
    _run_archive_counterfactuals(
        state=state,
        artifact=artifact,
        snapshot_root=snapshot_root,
        run_dir=run_dir,
        sdist_artifact=sdist_artifact,
    )
    _run_source_path_pollution_counterfactual(
        state=state,
        stage="counterfactual_source_path_pollution_smoke",
        venv_site=venv_site,
        snapshot_root=snapshot_root,
        work_dir=work_dir,
        resource_hashes=resource_hashes,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )


def _write_counterfactual_evidence(run_dir: Path, state: dict[str, Any]) -> None:
    counterfactuals = state.get("counterfactuals", [])
    if not counterfactuals:
        return
    _write_json(
        run_dir / "counterfactuals.json",
        {
            "schema": "opendata-runtime-package-counterfactuals/v1",
            "checker_script": str(Path(__file__).resolve()),
            "checker_script_sha256": _sha256_file(Path(__file__).resolve()),
            "baseline_positive_assertions": [
                assertion
                for assertion in state.get("positive_assertions", [])
                if assertion["name"]
                in {
                    "counterfactual_baseline_wheel_is_accepted",
                    "counterfactual_baseline_sdist_is_accepted",
                }
            ],
            "counterfactuals": counterfactuals,
        },
    )


def _pip_command(venv_python: Path, *arguments: str) -> list[str]:
    return [sys.executable, "-m", "pip", "--python", str(venv_python), *arguments]


def _record_artifact(path: Path, role: str, run_dir: Path) -> dict[str, Any]:
    stored = _copy_artifact(
        path,
        run_dir / "artifacts",
        stored_name=f"{role}--{path.name}",
    )
    return {
        "role": role,
        "path": str(stored.relative_to(run_dir)),
        "filename": stored.name,
        "size_bytes": stored.stat().st_size,
        "sha256": _sha256_file(stored),
    }


def _reconstruct_sdist_contract_snapshot(
    *,
    sdist: Path,
    source_hashes: dict[str, str],
    destination: Path,
) -> dict[str, Any]:
    """Rebuild only checker inputs from a retained sdist and verify manifest hashes."""
    required_paths = {
        "pyproject.toml",
        "LICENSE",
        "LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        *REQUIRED_PACKAGE_RESOURCES,
    }
    with tarfile.open(sdist, "r:gz") as archive:
        files = [tar_member for tar_member in archive.getmembers() if tar_member.isfile()]
        roots = {
            tar_member.name.split("/", maxsplit=1)[0]
            for tar_member in files
            if "/" in tar_member.name
        }
        if len(roots) != 1:
            raise ProbeFailureError(f"sdist has unexpected top-level roots: {sorted(roots)}")
        prefix = next(iter(roots)) + "/"
        member_by_relative: dict[str, tarfile.TarInfo] = {}
        for tar_member in files:
            if not tar_member.name.startswith(prefix):
                continue
            relative = tar_member.name[len(prefix) :]
            if relative in member_by_relative:
                raise ProbeFailureError(f"sdist has duplicate member: {relative}")
            member_by_relative[relative] = tar_member

        optional_paths = {"THIRD_PARTY_NOTICES.md"}
        selected_paths = required_paths | (optional_paths & member_by_relative.keys())
        destination.mkdir(parents=True, exist_ok=False)
        observed_files: list[dict[str, str]] = []
        for relative in sorted(selected_paths):
            expected_hash = source_hashes.get(relative)
            selected_member = member_by_relative.get(relative)
            if expected_hash is None or selected_member is None:
                raise ProbeFailureError(
                    f"retained sdist/source manifest lacks required path: {relative}"
                )
            source_file = archive.extractfile(selected_member)
            if source_file is None:
                raise ProbeFailureError(f"retained sdist member cannot be read: {relative}")
            payload = source_file.read()
            actual_hash = _sha256_bytes(payload)
            if actual_hash != expected_hash:
                raise ProbeFailureError(
                    f"retained sdist member disagrees with manifest: {relative}"
                )
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            observed_files.append({"path": relative, "sha256": actual_hash})
    return {
        "sdist": str(sdist),
        "sdist_sha256": _sha256_file(sdist),
        "files": observed_files,
        "validated_against_source_manifest": True,
    }


def run_probe(
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build, inspect, and install package artifacts; return the persisted report."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = evidence_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report_path = run_dir / "result.json"
    state: dict[str, Any] = {
        "schema": "opendata-runtime-package-probe/v1",
        "probe": "AC2-06 runtime package wheel/sdist clean-install probe",
        "run_id": run_id,
        "status": "RUNNING",
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
        "project_root": str(PROJECT_ROOT),
        "base_python": {
            "executable": sys.executable,
            "version": sys.version,
            "prefix": sys.prefix,
        },
        "timeout_seconds_per_command": timeout_seconds,
        "source": {},
        "commands": {},
        "archives": {},
        "artifacts": [],
        "installations": {},
        "positive_assertions": [],
        "counterfactuals": [],
        "failure": None,
    }

    def persist() -> None:
        _write_json(report_path, state)

    persist()
    try:
        if not importlib.util.find_spec("build"):
            raise ProbeFailureError("base Python does not have the build frontend installed")
        if not importlib.util.find_spec("setuptools") or not importlib.util.find_spec("wheel"):
            raise ProbeFailureError(
                "base Python must provide setuptools and wheel for no-isolation builds"
            )

        with tempfile.TemporaryDirectory(prefix="opendata-runtime-package-") as temporary:
            workspace = Path(temporary).resolve()
            original_manifest = _source_manifest(PROJECT_ROOT)
            snapshot_root = workspace / "source"
            _copy_source_snapshot(PROJECT_ROOT, snapshot_root)
            snapshot_root = snapshot_root.resolve()
            source_manifest = _source_manifest(snapshot_root)
            state["source"] = {
                "snapshot_path": str(snapshot_root),
                "snapshot_retained": False,
                "snapshot_outside_checkout": not _is_within(snapshot_root, PROJECT_ROOT),
                "fingerprint": source_manifest["fingerprint"],
                "file_count": source_manifest["file_count"],
                "manifest_path": "source-manifest.json",
                "checkout_fingerprint_at_snapshot": original_manifest["fingerprint"],
            }
            _write_json(run_dir / "source-manifest.json", source_manifest)
            _add_positive_assertion(
                state,
                "source_snapshot_created_outside_checkout",
                state["source"]["snapshot_outside_checkout"],
                observed=str(snapshot_root),
                expected="snapshot path is not beneath the project checkout",
            )
            _add_positive_assertion(
                state,
                "source_snapshot_matches_checkout_build_whitelist",
                source_manifest["fingerprint"] == original_manifest["fingerprint"],
                observed=source_manifest["fingerprint"],
                expected=original_manifest["fingerprint"],
            )

            git_result = _run_command(
                state=state,
                name="git_head",
                command=["git", "rev-parse", "HEAD"],
                cwd=PROJECT_ROOT,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            if not git_result["timed_out"] and git_result["exit_code"] == 0:
                state["source"]["git_head"] = (
                    (run_dir / git_result["stdout"]["path"]).read_text(encoding="utf-8").strip()
                )
            else:
                state["source"]["git_head"] = None

            dist_dir = workspace / "dist"
            dist_dir.mkdir()
            build_command = [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--wheel",
                "--sdist",
                "--outdir",
                str(dist_dir),
            ]
            _run_command(
                state=state,
                name="build_wheel_and_sdist",
                command=build_command,
                cwd=snapshot_root,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "build_wheel_and_sdist")
            wheel_files = sorted(dist_dir.glob("*.whl"))
            sdist_files = sorted(dist_dir.glob("*.tar.gz"))
            if len(wheel_files) != 1 or len(sdist_files) != 1:
                raise ProbeFailureError(
                    f"expected exactly one wheel and one sdist; found {wheel_files}, {sdist_files}"
                )
            wheel = wheel_files[0]
            sdist = sdist_files[0]
            state["artifacts"].append(_record_artifact(wheel, "source-build-wheel", run_dir))
            state["artifacts"].append(_record_artifact(sdist, "source-build-sdist", run_dir))
            _validate_archive(
                state=state,
                artifact=wheel,
                kind="wheel",
                snapshot_root=snapshot_root,
            )
            _validate_archive(
                state=state,
                artifact=sdist,
                kind="sdist",
                snapshot_root=snapshot_root,
            )

            venv_root = workspace / "install-target"
            _run_command(
                state=state,
                name="create_isolated_venv",
                command=[sys.executable, "-m", "venv", str(venv_root)],
                cwd=workspace,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "create_isolated_venv")
            venv_python = (
                venv_root / "Scripts/python.exe" if os.name == "nt" else venv_root / "bin/python"
            )
            venv_config = venv_root / "pyvenv.cfg"
            config_text = venv_config.read_text(encoding="utf-8").lower()
            system_packages_enabled = any(
                line.strip() == "include-system-site-packages = true"
                for line in config_text.splitlines()
            )
            _add_positive_assertion(
                state,
                "installation_target_isolated_from_system_site_packages",
                venv_python.is_file() and not system_packages_enabled,
                observed={
                    "python_exists": venv_python.is_file(),
                    "include_system_site_packages": system_packages_enabled,
                },
                expected={"python_exists": True, "include_system_site_packages": False},
            )
            state["installations"]["target"] = {
                "venv_root": str(venv_root),
                "python": str(venv_python),
                "pyvenv_cfg_sha256": _sha256_file(venv_config),
                "system_site_packages_enabled": system_packages_enabled,
                "execution_interpreter": sys.executable,
            }
            site_packages = _probe_site_packages(venv_root)
            missing_resources = [
                path for path in REQUIRED_PACKAGE_RESOURCES if not (snapshot_root / path).is_file()
            ]
            if missing_resources:
                raise ProbeFailureError(
                    f"source snapshot lacks package resources: {missing_resources}"
                )
            resource_hashes = {
                path.removeprefix("opendata/"): _sha256_file(snapshot_root / path)
                for path in REQUIRED_PACKAGE_RESOURCES
            }

            wheel_install_command = _pip_command(
                venv_python,
                "install",
                "--no-input",
                "--disable-pip-version-check",
                str(wheel),
            )
            _run_command(
                state=state,
                name="install_source_wheel_with_dependencies",
                command=wheel_install_command,
                cwd=workspace,
                environment=_clean_environment(isolated_pip=True),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "install_source_wheel_with_dependencies")
            pip_check_wheel = _pip_command(venv_python, "check")
            _run_command(
                state=state,
                name="pip_check_after_wheel_install",
                command=pip_check_wheel,
                cwd=workspace,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            pip_check_record = _require_command_success(state, "pip_check_after_wheel_install")
            _add_positive_assertion(
                state,
                "pip_dependency_check_after_wheel_install",
                pip_check_record["exit_code"] == 0 and not pip_check_record["timed_out"],
                observed={"exit_code": pip_check_record["exit_code"]},
                expected={"exit_code": 0},
            )
            _run_smoke(
                state=state,
                stage="wheel_install_smoke",
                venv_site=site_packages,
                snapshot_root=snapshot_root,
                work_dir=workspace,
                resource_hashes=resource_hashes,
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _run_counterfactual_suite(
                state=state,
                artifact=run_dir / state["artifacts"][0]["path"],
                snapshot_root=snapshot_root,
                venv_site=site_packages,
                work_dir=workspace,
                resource_hashes=resource_hashes,
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
                sdist_artifact=sdist,
            )

            rebuilt_dir = workspace / "sdist-rebuilt-wheel"
            rebuilt_dir.mkdir()
            rebuild_command = [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(rebuilt_dir),
                str(sdist),
            ]
            _run_command(
                state=state,
                name="rebuild_wheel_from_sdist",
                command=rebuild_command,
                cwd=workspace,
                environment=_clean_environment(isolated_pip=True),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "rebuild_wheel_from_sdist")
            rebuilt_wheels = sorted(rebuilt_dir.glob("*.whl"))
            if len(rebuilt_wheels) != 1:
                raise ProbeFailureError(f"expected one sdist-rebuilt wheel, found {rebuilt_wheels}")
            rebuilt_wheel = rebuilt_wheels[0]
            state["artifacts"].append(
                _record_artifact(rebuilt_wheel, "sdist-rebuilt-wheel", run_dir)
            )
            _validate_archive(
                state=state,
                artifact=rebuilt_wheel,
                kind="sdist_rebuilt_wheel",
                snapshot_root=snapshot_root,
            )
            sdist_install_command = _pip_command(
                venv_python,
                "install",
                "--no-input",
                "--disable-pip-version-check",
                "--force-reinstall",
                "--no-deps",
                str(rebuilt_wheel),
            )
            _run_command(
                state=state,
                name="install_sdist_rebuilt_wheel",
                command=sdist_install_command,
                cwd=workspace,
                environment=_clean_environment(isolated_pip=True),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "install_sdist_rebuilt_wheel")
            _run_command(
                state=state,
                name="pip_check_after_sdist_rebuild_install",
                command=_pip_command(venv_python, "check"),
                cwd=workspace,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            pip_check_record = _require_command_success(
                state, "pip_check_after_sdist_rebuild_install"
            )
            _add_positive_assertion(
                state,
                "pip_dependency_check_after_sdist_rebuild_install",
                pip_check_record["exit_code"] == 0 and not pip_check_record["timed_out"],
                observed={"exit_code": pip_check_record["exit_code"]},
                expected={"exit_code": 0},
            )
            _run_smoke(
                state=state,
                stage="sdist_rebuild_install_smoke",
                venv_site=site_packages,
                snapshot_root=snapshot_root,
                work_dir=workspace,
                resource_hashes=resource_hashes,
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )

            final_manifest = _source_manifest(PROJECT_ROOT)
            stable = final_manifest["fingerprint"] == original_manifest["fingerprint"]
            state["source"]["checkout_fingerprint_at_finish"] = final_manifest["fingerprint"]
            _add_positive_assertion(
                state,
                "checkout_build_surface_unchanged_during_probe",
                stable,
                observed=final_manifest["fingerprint"],
                expected=original_manifest["fingerprint"],
            )

        state["status"] = "PASS"
    except Exception as exc:  # Persist a truthful partial run and its diagnostics.
        state["status"] = "FAIL"
        state["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        state["finished_at_utc"] = _utc_now()
        _write_counterfactual_evidence(run_dir, state)
        persist()
    return state


def run_archive_counterfactuals(
    source_run_dir: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run negative probes against a retained wheel without rebuilding it.

    The archived result, source manifest, wheel, and sdist are read-only inputs. New
    reports, mutated archive copies, and logs go to a fresh ``counterfactuals`` child.
    """
    source_run_dir = source_run_dir.resolve()
    source_receipt = source_run_dir / "result.json"
    source_manifest_path = source_run_dir / "source-manifest.json"
    if not source_receipt.is_file() or not source_manifest_path.is_file():
        raise ProbeFailureError("source run must retain result.json and source-manifest.json")

    receipt_hash_before = _sha256_file(source_receipt)
    source_receipt_data = _parse_json_object(
        source_receipt.read_text(encoding="utf-8"), "source package probe receipt"
    )
    manifest = _parse_json_object(
        source_manifest_path.read_text(encoding="utf-8"), "source package probe manifest"
    )
    artifact_entries = source_receipt_data.get("artifacts")
    if not isinstance(artifact_entries, list):
        raise ProbeFailureError("source package probe receipt artifacts must be an array")
    records = [item for item in artifact_entries if isinstance(item, dict)]
    wheel_record: JsonObject | None = None
    wheel: Path | None = None
    wheel_hash_before: str | None = None
    for preferred_role in ("source-build-wheel", "sdist-rebuilt-wheel"):
        for item in records:
            if item.get("role") != preferred_role:
                continue
            candidate = source_run_dir / str(item.get("path", ""))
            if candidate.is_file() and _sha256_file(candidate) == item.get("sha256"):
                wheel_record = item
                wheel = candidate
                wheel_hash_before = _sha256_file(candidate)
                break
        if wheel_record is not None:
            break
    if wheel_record is None or wheel is None or wheel_hash_before is None:
        raise ProbeFailureError("source run has no retained wheel matching a receipt SHA-256")

    sdist_record: JsonObject | None = None
    sdist: Path | None = None
    sdist_hash_before: str | None = None
    for item in records:
        if item.get("role") != "source-build-sdist":
            continue
        candidate = source_run_dir / str(item.get("path", ""))
        if candidate.is_file() and _sha256_file(candidate) == item.get("sha256"):
            sdist_record = item
            sdist = candidate
            sdist_hash_before = _sha256_file(candidate)
            break
    if sdist_record is None or sdist is None or sdist_hash_before is None:
        raise ProbeFailureError("source run has no retained sdist matching a receipt SHA-256")
    manifest_hash_before = _sha256_file(source_manifest_path)

    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise ProbeFailureError("source package probe manifest files must be an array")
    source_hashes: dict[str, str] = {}
    for file_record in manifest_files:
        if not isinstance(file_record, dict):
            continue
        relative = file_record.get("path")
        digest = file_record.get("sha256")
        if isinstance(relative, str) and isinstance(digest, str):
            source_hashes[relative] = digest
    required_source_paths = {
        "pyproject.toml",
        "LICENSE",
        "LICENSE-AKSHARE",
        "opendata/data/providers/akshare/_vendor/LICENSE-AKSHARE",
        *REQUIRED_PACKAGE_RESOURCES,
    }
    resource_hashes: dict[str, str] = {}
    for relative in REQUIRED_PACKAGE_RESOURCES:
        expected_hash = source_hashes.get(relative)
        if expected_hash is None:
            raise ProbeFailureError(f"retained source manifest lacks resource: {relative}")
        resource_hashes[relative.removeprefix("opendata/")] = expected_hash

    output_parent = source_run_dir / "counterfactuals"
    output_parent.mkdir(parents=True, exist_ok=True)
    output_dir = output_parent / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir.mkdir(parents=True, exist_ok=False)
    evidence_path = output_dir / "counterfactuals.json"
    state: dict[str, Any] = {
        "schema": "opendata-runtime-package-counterfactuals/v1",
        "status": "RUNNING",
        "source_run_dir": str(source_run_dir),
        "source_receipt": {
            "path": str(source_receipt.relative_to(PROJECT_ROOT)),
            "sha256_before": receipt_hash_before,
        },
        "source_manifest_sha256": _sha256_file(source_manifest_path),
        "source_manifest_fingerprint": manifest.get("fingerprint"),
        "source_artifact": {
            "path": str(wheel.relative_to(PROJECT_ROOT)),
            "role": wheel_record["role"],
            "sha256_before": wheel_hash_before,
        },
        "source_distribution": {
            "path": str(sdist.relative_to(PROJECT_ROOT)),
            "role": sdist_record["role"],
            "sha256_before": sdist_hash_before,
        },
        "source_manifest_sha256_before": manifest_hash_before,
        "source_baseline_paths": sorted(required_source_paths),
        "checker_script": str(Path(__file__).resolve()),
        "checker_script_sha256": _sha256_file(Path(__file__).resolve()),
        "evidence_path": str(evidence_path),
        "commands": {},
        "positive_assertions": [],
        "counterfactuals": [],
        "failure": None,
    }
    _write_json(evidence_path, state)
    try:
        with tempfile.TemporaryDirectory(prefix="opendata-package-counterfactual-") as temporary:
            workspace = Path(temporary).resolve()
            snapshot_root = workspace / "source-snapshot"
            reconstructed_paths = _reconstruct_sdist_contract_snapshot(
                sdist=sdist,
                source_hashes=source_hashes,
                destination=snapshot_root,
            )
            state["reconstructed_source_baseline"] = reconstructed_paths
            _write_json(evidence_path, state)
            installed_site = workspace / "installed-wheel"
            install_record = _run_command(
                state=state,
                name="install_retained_wheel_for_counterfactual_smoke",
                command=[
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--target",
                    str(installed_site),
                    "--no-deps",
                    "--no-input",
                    "--disable-pip-version-check",
                    str(wheel),
                ],
                cwd=workspace,
                environment=_clean_environment(isolated_pip=True),
                timeout_seconds=timeout_seconds,
                run_dir=output_dir,
            )
            if install_record["timed_out"] or install_record["exit_code"] != 0:
                raise ProbeFailureError("could not isolate-install retained wheel for smoke test")
            _run_counterfactual_suite(
                state=state,
                artifact=wheel,
                snapshot_root=snapshot_root,
                venv_site=installed_site,
                work_dir=workspace,
                resource_hashes=resource_hashes,
                timeout_seconds=timeout_seconds,
                run_dir=output_dir,
                sdist_artifact=sdist,
            )
        state["status"] = "PASS"
    except Exception as exc:
        state["status"] = "FAIL"
        state["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        state["source_receipt"]["sha256_after"] = _sha256_file(source_receipt)
        state["source_artifact"]["sha256_after"] = _sha256_file(wheel)
        state["source_distribution"]["sha256_after"] = _sha256_file(sdist)
        state["source_manifest_sha256_after"] = _sha256_file(source_manifest_path)
        state["source_receipt"]["unchanged"] = (
            state["source_receipt"]["sha256_after"] == receipt_hash_before
        )
        state["source_artifact"]["unchanged"] = (
            state["source_artifact"]["sha256_after"] == wheel_hash_before
        )
        state["source_distribution"]["unchanged"] = (
            state["source_distribution"]["sha256_after"] == sdist_hash_before
        )
        state["source_manifest_unchanged"] = (
            state["source_manifest_sha256_after"] == manifest_hash_before
        )
        if state["status"] == "PASS" and not (
            state["source_receipt"]["unchanged"]
            and state["source_artifact"]["unchanged"]
            and state["source_distribution"]["unchanged"]
            and state["source_manifest_unchanged"]
        ):
            state["status"] = "FAIL"
            state["failure"] = {
                "type": "ProbeFailureError",
                "message": (
                    "counterfactual run modified a source receipt, manifest, wheel, or sdist"
                ),
            }
        _write_json(evidence_path, state)
    return state


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=EVIDENCE_ROOT,
        help="root directory for timestamped evidence runs",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="timeout applied independently to each child command",
    )
    parser.add_argument(
        "--counterfactual-run-dir",
        type=Path,
        help="run only negative archive/smoke checks against a retained run's wheel",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    """Run the probe and return a shell-friendly status code."""
    args = _parse_args(argv)
    if args.timeout_seconds <= 0:
        print("timeout-seconds must be positive", file=sys.stderr)
        return 2
    if args.counterfactual_run_dir is not None:
        try:
            result = run_archive_counterfactuals(
                args.counterfactual_run_dir,
                timeout_seconds=args.timeout_seconds,
            )
        except Exception as exc:
            print(json.dumps({"status": "FAIL", "failure": str(exc)}, sort_keys=True))
            return 1
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "evidence_path": result["evidence_path"],
                    "counterfactual_count": len(result["counterfactuals"]),
                    "failure": result["failure"],
                },
                sort_keys=True,
            )
        )
        return 0 if result["status"] == "PASS" else 1

    result = run_probe(evidence_root=args.evidence_root, timeout_seconds=args.timeout_seconds)
    print(
        json.dumps(
            {
                "status": result["status"],
                "run_id": result["run_id"],
                "result_path": str(args.evidence_root.resolve() / result["run_id"] / "result.json"),
                "failure": result["failure"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
