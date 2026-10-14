r"""Build and exercise opendata-client as a clean, independently installed SDK.

The probe copies only ``opendata_client`` into a temporary source tree outside
the checkout, builds a wheel and sdist there, and performs isolated installs.
REST, ``ws``, and ``frames`` installs have separate virtual environments. The
installed SDK is exercised against local HTTP and WebSocket loopback servers;
no live API, provider, credential, or database is used.

Run with the selected Anaconda base Python::

    /Users/yunjinqi/opt/anaconda3/bin/conda run -n base python \\
        scripts/quality/sdk_package_probe.py

Evidence is written below ``docs/evidence/C100/sdk-packaging/runs``.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess  # nosec B404  # guarded command families are the only subprocess users
import sys
import tarfile
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn, TypeAlias, TypedDict
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = PROJECT_ROOT / "opendata_client"
EVIDENCE_ROOT = PROJECT_ROOT / "docs/evidence/C100/sdk-packaging/runs"
SOURCE_FILES = ("pyproject.toml", "README.md")
PACKAGE_DIRECTORY = "opendata_client"
EXPECTED_NAME = "opendata-client"
EXPECTED_VERSION = "0.1.0"
EXPECTED_REQUIRES_PYTHON = ">=3.10"
EXPECTED_REQUIREMENTS = frozenset(
    {
        "httpx>=0.27",
        "loguru>=0.7",
        "pandas>=2.0;extra==frames",
        "websockets>=11.0;extra==ws",
    }
)
OPTIONAL_FEATURES = {"rest": None, "ws": "ws", "frames": "frames"}
DEFAULT_TIMEOUT_SECONDS = 600
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
    """A measured SDK packaging or installed behavior condition failed."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


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
        if candidate.name.startswith("opendata-sdk-package-"):
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


def _owned_local_wheel(value: str, workspace: Path) -> bool:
    """Accept only a local wheel path, including this SDK's optional extra URLs."""
    path_value = value
    if " @ " in value:
        requirement, separator, uri = value.partition(" @ ")
        if not separator or requirement not in {
            EXPECTED_NAME,
            f"{EXPECTED_NAME}[ws]",
            f"{EXPECTED_NAME}[frames]",
        }:
            return False
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            return False
        path_value = unquote(parsed.path)
        if (
            os.name == "nt"
            and len(path_value) >= 3
            and path_value[0] == "/"
            and path_value[2] == ":"
        ):
            path_value = path_value[1:]
    path = Path(path_value)
    return (
        path.is_absolute()
        and path.name.endswith(".whl")
        and (_is_within(path, workspace) or _is_within(path, EVIDENCE_ROOT))
    )


def _validate_pip_request(arguments: list[str], cwd: Path, workspace: Path) -> None:
    """Validate the exact pip command shapes dispatched by this SDK probe."""
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
    index_url: str | None = None
    index = 0
    while index < len(options):
        option = options[index]
        if option in {"--no-input", "--disable-pip-version-check", "--no-cache-dir"}:
            if option in seen:
                raise ProbeFailureError("probe pip install contains a repeated option")
            seen.add(option)
            index += 1
            continue
        if option == "--index-url":
            if option in seen or index + 1 >= len(options):
                raise ProbeFailureError("probe pip index URL is missing or repeated")
            seen.add(option)
            index_url = options[index + 1]
            index += 2
            continue
        if option.startswith("-"):
            raise ProbeFailureError("probe pip install contains an unapproved option")
        positionals.append(option)
        index += 1

    required = {"--no-input", "--disable-pip-version-check", "--no-cache-dir", "--index-url"}
    if (
        seen != required
        or index_url != "https://pypi.org/simple"
        or len(positionals) != 1
        or venv_python is None
        or not _owned_local_wheel(positionals[0], workspace)
    ):
        raise ProbeFailureError("probe pip install must use its fixed venv, index and local wheel")


def _validate_subprocess_request(
    command: list[str], cwd: Path, timeout_seconds: int, run_dir: Path
) -> None:
    """Reject commands or working directories outside the SDK probe surface."""
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
            "probe subprocess cwd is not owned by this checkout or SDK workspace"
        )

    if command == ["git", "rev-parse", "HEAD"]:
        if not project_cwd:
            raise ProbeFailureError("git identity lookup must run from the project checkout")
        return
    if command[0] != sys.executable:
        raise ProbeFailureError("probe subprocess executable is not the selected base Python")

    if command[1:3] == ["-S", "-c"]:
        if len(command) != 4 or command[3] != _smoke_code():
            raise ProbeFailureError("probe smoke subprocess must use the fixed loopback program")
        return
    if command[1:2] == ["-c"]:
        if (
            len(command) != 4
            or command[2] != _metadata_negative_control_code()
            or not _is_within(Path(command[3]), run_dir)
        ):
            raise ProbeFailureError("probe inline subprocess is outside the owned negative control")
        return
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


def _json_object_field(value: JsonObject, key: str, context: str) -> JsonObject:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise ProbeFailureError(f"{context}.{key} must be a JSON object")
    return nested


def _json_object_list(value: JsonValue | None, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        raise ProbeFailureError(f"{context} must be a JSON array")
    result: list[JsonObject] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProbeFailureError(f"{context}[{index}] must be a JSON object")
        result.append(item)
    return result


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
    """Return the SDK build inputs, excluding generated files and caches."""
    paths: list[Path] = []
    for name in SOURCE_FILES:
        path = root / name
        if not path.is_file():
            raise ProbeFailureError(f"required SDK build input is missing: {name}")
        paths.append(path)

    package_root = root / PACKAGE_DIRECTORY
    if not package_root.is_dir():
        raise ProbeFailureError("required SDK package directory is missing")
    for directory, dirnames, filenames in os.walk(package_root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in {"__pycache__", ".pytest_cache", "build", "dist"}
            and not name.endswith((".egg-info", ".dist-info"))
        )
        for filename in sorted(filenames):
            if filename.endswith((".pyc", ".pyo", ".pyd")):
                continue
            path = Path(directory) / filename
            if path.is_symlink() and not _is_within(path.resolve(), package_root):
                raise ProbeFailureError(f"SDK source symlink escapes package directory: {path}")
            if path.is_file():
                paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _source_manifest(root: Path) -> dict[str, Any]:
    """Hash each build input and bind both its relative path and contents."""
    records: list[dict[str, Any]] = []
    fingerprint = hashlib.sha256()
    for path in _source_file_paths(root):
        relative = path.relative_to(root).as_posix()
        digest = _sha256_file(path)
        fingerprint.update(relative.encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(digest.encode("ascii"))
        fingerprint.update(b"\n")
        records.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": digest})
    return {
        "algorithm": "sha256",
        "fingerprint": fingerprint.hexdigest(),
        "file_count": len(records),
        "files": records,
    }


def _copy_source_snapshot(source_root: Path, snapshot_root: Path) -> None:
    """Copy only the SDK build inputs to a new external directory."""
    snapshot_root.mkdir(parents=True, exist_ok=False)
    for name in SOURCE_FILES:
        shutil.copy2(source_root / name, snapshot_root / name)
    shutil.copytree(
        source_root / PACKAGE_DIRECTORY,
        snapshot_root / PACKAGE_DIRECTORY,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.py[cod]", ".pytest_cache", "build", "dist", "*.egg-info"
        ),
    )


def _clean_environment(*, isolated_pip: bool = False) -> dict[str, str]:
    """Remove checkout and private-index redirection from child processes."""
    environment = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "PIP_TARGET",
        "PIP_PREFIX",
        "VIRTUAL_ENV",
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
            "NUMEXPR_NUM_THREADS": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        }
    )
    if isolated_pip:
        environment["PIP_CONFIG_FILE"] = os.devnull
        environment["PIP_INDEX_URL"] = "https://pypi.org/simple"
        environment.pop("PIP_EXTRA_INDEX_URL", None)
        environment.pop("PIP_TRUSTED_HOST", None)
    return environment


def _loopback_environment() -> dict[str, str]:
    """Keep local SDK smoke traffic away from inherited HTTP/WebSocket proxies."""
    environment = _clean_environment()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "WS_PROXY",
        "WSS_PROXY",
        "ws_proxy",
        "wss_proxy",
    ):
        environment.pop(name, None)
    environment.update({"NO_PROXY": "*", "no_proxy": "*"})
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
    """Run one fixed-argv child and retain exact command and log evidence."""
    _validate_subprocess_request(command, cwd, timeout_seconds, run_dir)
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
        "duration_seconds": round(time.monotonic() - monotonic_start, 3),
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


def _add_fact(
    state: dict[str, Any], name: str, passed: bool, *, observed: object, expected: object
) -> None:
    state["positive_assertions"].append(
        {
            "name": name,
            "category": "positive_assertion",
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
        }
    )
    if not passed:
        raise ProbeFailureError(f"positive assertion failed: {name}")


def _add_synthetic_control(
    state: dict[str, Any],
    name: str,
    detected: bool,
    *,
    mutation: object,
    observed: object,
    expected: object,
) -> None:
    state["synthetic_controls"].append(
        {
            "name": name,
            "category": "synthetic_checker_input",
            "status": "PASS" if detected else "FAIL",
            "mutation": mutation,
            "observed": observed,
            "expected": expected,
        }
    )
    if not detected:
        raise ProbeFailureError(f"synthetic control was not detected: {name}")


def _require_command_success(state: dict[str, Any], name: str) -> CommandRecord:
    record = _command_record(state, name)
    if record["timed_out"] or record["exit_code"] != 0:
        raise ProbeFailureError(
            f"command {name} failed: timeout={record['timed_out']} exit={record['exit_code']}"
        )
    return record


def _canonical_requirement(value: str) -> str:
    normalized = "".join(value.casefold().split()).replace('"', "").replace("'", "")
    return re.sub(r"([<>=!~;])", r"\1", normalized)


def _metadata_contract_issues(metadata_text: str) -> list[str]:
    """Return precise violations of the independently installable SDK contract."""
    message = Parser().parsestr(metadata_text)
    issues: list[str] = []
    name = re.sub(r"[-_.]+", "-", (message.get("Name") or "").strip()).casefold()
    if name != EXPECTED_NAME:
        issues.append(f"Name must be {EXPECTED_NAME!r}")
    if message.get("Version") != EXPECTED_VERSION:
        issues.append(f"Version must be {EXPECTED_VERSION!r}")
    if _canonical_requirement(message.get("Requires-Python", "")) != ">=3.10":
        issues.append("Requires-Python must be >=3.10")

    actual = Counter(_canonical_requirement(item) for item in message.get_all("Requires-Dist", []))
    expected = Counter(_canonical_requirement(item) for item in EXPECTED_REQUIREMENTS)
    if actual != expected:
        missing = sorted((expected - actual).elements())
        unexpected = sorted((actual - expected).elements())
        issues.append(f"Requires-Dist mismatch; missing={missing}, unexpected={unexpected}")

    extras = {item.strip().casefold() for item in message.get_all("Provides-Extra", [])}
    if extras != {"frames", "ws"}:
        issues.append(f"Provides-Extra must be ['frames', 'ws']; observed={sorted(extras)}")
    return issues


def _metadata_from_archive(artifact: Path, kind: str) -> tuple[str, str]:
    """Read wheel METADATA or source-distribution PKG-INFO from an artifact."""
    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        with ZipFile(artifact) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ProbeFailureError(f"wheel must contain one METADATA file; found {names}")
            return names[0], archive.read(names[0]).decode("utf-8")

    with tarfile.open(artifact, "r:gz") as archive:
        matches = [
            member
            for member in archive.getmembers()
            if member.isfile()
            and len(PurePosixPath(member.name).parts) == 2
            and PurePosixPath(member.name).name == "PKG-INFO"
        ]
        if len(matches) != 1:
            raise ProbeFailureError(f"sdist must contain one PKG-INFO; found {len(matches)}")
        handle = archive.extractfile(matches[0])
        if handle is None:
            raise ProbeFailureError("sdist PKG-INFO is unreadable")
        return matches[0].name, handle.read().decode("utf-8")


def _mutate_wheel_missing_loguru(source: Path, destination: Path) -> dict[str, str]:
    """Create a distinct wheel counterfactual with only its dependency metadata changed."""
    with ZipFile(source) as archive:
        infos = archive.infolist()
        metadata_names = [
            info.filename for info in infos if info.filename.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            raise ProbeFailureError(f"wheel must contain one METADATA file: {metadata_names}")
        metadata_name = metadata_names[0]
        record_names = [
            info.filename
            for info in infos
            if info.filename == metadata_name.rsplit("/", maxsplit=1)[0] + "/RECORD"
        ]
        if len(record_names) != 1:
            raise ProbeFailureError(f"wheel must contain one RECORD file: {record_names}")
        members = {info.filename: archive.read(info.filename) for info in infos}

    metadata_text = members[metadata_name].decode("utf-8")
    mutated_metadata, removed_count = re.subn(
        r"(?im)^Requires-Dist:\s*loguru>=0\.7\s*\r?\n?", "", metadata_text, count=1
    )
    if removed_count != 1:
        raise ProbeFailureError("built wheel did not contain exactly one loguru>=0.7 requirement")
    mutated_metadata_bytes = mutated_metadata.encode("utf-8")
    members[metadata_name] = mutated_metadata_bytes

    record_name = record_names[0]
    record_rows = list(csv.reader(io.StringIO(members[record_name].decode("utf-8"))))
    metadata_rows = [row for row in record_rows if row and row[0] == metadata_name]
    if len(metadata_rows) != 1 or len(metadata_rows[0]) < 3:
        raise ProbeFailureError("wheel RECORD did not contain a unique METADATA row")
    digest = base64.urlsafe_b64encode(hashlib.sha256(mutated_metadata_bytes).digest())
    metadata_rows[0][1] = "sha256=" + digest.decode("ascii").rstrip("=")
    metadata_rows[0][2] = str(len(mutated_metadata_bytes))
    record_output = io.StringIO(newline="")
    csv.writer(record_output, lineterminator="\n").writerows(record_rows)
    members[record_name] = record_output.getvalue().encode("utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ProbeFailureError(f"wheel mutation destination already exists: {destination.name}")
    with ZipFile(source) as original, ZipFile(destination, "w") as mutated:
        for info in original.infolist():
            mutated.writestr(info, members[info.filename])
    return {"metadata_member": metadata_name, "record_member": record_name}


def _metadata_negative_control_code() -> str:
    """Run the metadata checker in a child process against a mutated built wheel."""
    return r"""import json, sys
from pathlib import Path
from scripts.quality.sdk_package_probe import _metadata_from_archive, _metadata_contract_issues

artifact = Path(sys.argv[1])
_, metadata = _metadata_from_archive(artifact, "wheel")
issues = _metadata_contract_issues(metadata)
detected = any("loguru>=0.7" in issue for issue in issues)
print(json.dumps({"issues": issues, "detected": detected}, sort_keys=True))
raise SystemExit(0 if detected else 1)
"""


def _record_artifact_mutation(
    state: dict[str, Any], mutation: dict[str, Any], passed: bool
) -> None:
    mutation["category"] = "built_artifact_negative_control"
    mutation["status"] = "PASS" if passed else "FAIL"
    state["artifact_mutations"].append(mutation)
    if not passed:
        raise ProbeFailureError(f"built-artifact negative control failed: {mutation['name']}")


def _run_missing_loguru_artifact_control(
    *,
    state: dict[str, Any],
    source_wheel: Path,
    source_artifact: dict[str, Any],
    workspace: Path,
    timeout_seconds: int,
    run_dir: Path,
) -> None:
    mutation_path = run_dir / "mutations" / f"built-wheel-without-loguru--{source_wheel.name}"
    members = _mutate_wheel_missing_loguru(source_wheel, mutation_path)
    stage = "built_wheel_missing_loguru_metadata_negative_control"
    command = _run_command(
        state=state,
        name=stage,
        command=[sys.executable, "-c", _metadata_negative_control_code(), str(mutation_path)],
        cwd=PROJECT_ROOT,
        environment=_clean_environment(),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    stdout_text = (run_dir / command["stdout"]["path"]).read_text(encoding="utf-8")
    observed: JsonObject
    try:
        observed = _parse_json_object(
            stdout_text.splitlines()[-1], f"{stage} negative-control output"
        )
    except (IndexError, ProbeFailureError):
        observed = {
            "stdout": stdout_text[-1000:],
            "stderr": (run_dir / command["stderr"]["path"]).read_text(encoding="utf-8")[-1000:],
        }
    issues = observed.get("issues")
    detected_missing_dependency = isinstance(issues, list) and any(
        isinstance(item, str) and "loguru>=0.7" in item for item in issues
    )
    passed = (
        not command["timed_out"]
        and command["exit_code"] == 0
        and observed.get("detected") is True
        and detected_missing_dependency
    )
    _record_artifact_mutation(
        state,
        {
            "name": stage,
            "mutation": {
                "kind": "remove_runtime_dependency_from_built_wheel_metadata",
                **members,
            },
            "source_artifact": {
                "role": source_artifact["role"],
                "path": source_artifact["path"],
                "sha256": source_artifact["sha256"],
            },
            "mutated_artifact": {
                "path": str(mutation_path.relative_to(run_dir)),
                "sha256": _sha256_file(mutation_path),
                "size_bytes": mutation_path.stat().st_size,
            },
            "expected": {
                "detected": True,
                "missing_requirement": "loguru>=0.7",
                "command_exit_code": 0,
                "timed_out": False,
            },
            "observed": observed,
            "command": {
                "name": stage,
                "exit_code": command["exit_code"],
                "timed_out": command["timed_out"],
                "stdout": command["stdout"],
                "stderr": command["stderr"],
            },
        },
        passed,
    )


def _run_checkout_pollution_control(
    *,
    state: dict[str, Any],
    source_artifact: dict[str, Any],
    target: dict[str, Any],
    snapshot_root: Path,
    workspace: Path,
    timeout_seconds: int,
    run_dir: Path,
) -> None:
    site_packages = _site_packages(Path(target["venv_root"]))
    polluted_pythonpath = os.pathsep.join((str(site_packages), str(PROJECT_ROOT)))
    environment = _loopback_environment()
    environment.update(
        {
            "PYTHONPATH": polluted_pythonpath,
            "SDK_PROBE_SITE": str(site_packages),
            "SDK_PROBE_CHECKOUT": str(PROJECT_ROOT),
            "SDK_PROBE_SNAPSHOT": str(snapshot_root),
            "SDK_PROBE_VARIANT": "rest",
        }
    )
    stage = "installed_wheel_checkout_pollution_negative_control"
    command = _run_command(
        state=state,
        name=stage,
        command=[sys.executable, "-S", "-c", _smoke_code()],
        cwd=workspace,
        environment=environment,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    stderr_text = (run_dir / command["stderr"]["path"]).read_text(encoding="utf-8")
    checkout_text = str(PROJECT_ROOT.resolve())
    observed = {
        "checkout_path_reported": checkout_text in stderr_text,
        "leak_assertion_reported": "leaked_paths" in stderr_text,
        "isolated_target_reported": str(site_packages.resolve()) in stderr_text,
        "stderr_excerpt": stderr_text[-1200:],
    }
    passed = (
        not command["timed_out"]
        and command["exit_code"] == 1
        and observed["checkout_path_reported"] is True
        and observed["leak_assertion_reported"] is True
        and observed["isolated_target_reported"] is True
    )
    _record_artifact_mutation(
        state,
        {
            "name": stage,
            "mutation": {
                "kind": "inject_checkout_path_after_installed_wheel_site_packages",
                "pythonpath": polluted_pythonpath,
                "installed_distribution": "opendata-client",
            },
            "source_artifact": {
                "role": source_artifact["role"],
                "path": source_artifact["path"],
                "sha256": source_artifact["sha256"],
            },
            "installed_target": {
                "venv_root": target["venv_root"],
                "site_packages": str(site_packages),
            },
            "expected": {
                "import_from_installed_target": True,
                "checkout_path_detected": True,
                "command_exit_code": 1,
                "timed_out": False,
            },
            "observed": observed,
            "command": {
                "name": stage,
                "exit_code": command["exit_code"],
                "timed_out": command["timed_out"],
                "stdout": command["stdout"],
                "stderr": command["stderr"],
            },
        },
        passed,
    )


def _archive_members(artifact: Path, kind: str) -> set[str]:
    if kind in {"wheel", "sdist_rebuilt_wheel"}:
        with ZipFile(artifact) as archive:
            return set(archive.namelist())
    with tarfile.open(artifact, "r:gz") as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    roots = {name.split("/", maxsplit=1)[0] for name in names if "/" in name}
    if len(roots) != 1:
        raise ProbeFailureError(f"sdist has unexpected top-level roots: {sorted(roots)}")
    prefix = next(iter(roots)) + "/"
    return {name[len(prefix) :] for name in names if name.startswith(prefix)}


def _validate_archive(
    *,
    state: dict[str, Any],
    artifact: Path,
    kind: str,
    snapshot_root: Path,
) -> dict[str, Any]:
    members = _archive_members(artifact, kind)
    _, metadata_text = _metadata_from_archive(artifact, kind)
    metadata_issues = _metadata_contract_issues(metadata_text)
    expected_package_files = {
        path.relative_to(snapshot_root).as_posix()
        for path in _source_file_paths(snapshot_root)
        if path.is_relative_to(snapshot_root / PACKAGE_DIRECTORY)
    }
    forbidden = sorted(
        member
        for member in members
        if member.startswith(("opendata/", "opendata_http/", "opendata_fuyao/"))
    )
    actual_package_files = {
        member
        for member in members
        if member.startswith(f"{PACKAGE_DIRECTORY}/") and not member.endswith((".pyc", ".pyo"))
    }
    missing_package_files = sorted(expected_package_files - actual_package_files)
    required_top_level = {"pyproject.toml", "README.md"} if kind == "sdist" else set()
    missing_top_level = sorted(required_top_level - members)

    report = {
        "kind": kind,
        "path": str(artifact),
        "filename": artifact.name,
        "size_bytes": artifact.stat().st_size,
        "sha256": _sha256_file(artifact),
        "member_count": len(members),
        "metadata_member": _metadata_from_archive(artifact, kind)[0],
        "metadata_contract_issues": metadata_issues,
        "missing_package_files": missing_package_files,
        "missing_top_level_build_inputs": missing_top_level,
        "forbidden_package_members": forbidden,
    }
    state["archives"][kind] = report
    _add_fact(
        state,
        f"{kind}_dependency_metadata_matches_sdk_contract",
        not metadata_issues,
        observed=metadata_issues,
        expected={
            "requires_python": EXPECTED_REQUIRES_PYTHON,
            "requires_dist": sorted(EXPECTED_REQUIREMENTS),
            "extras": ["frames", "ws"],
        },
    )
    _add_fact(
        state,
        f"{kind}_contains_sdk_sources_and_excludes_server_package",
        not missing_package_files and not missing_top_level and not forbidden,
        observed={
            "missing_sdk_files": missing_package_files,
            "missing_top_level": missing_top_level,
            "forbidden": forbidden,
        },
        expected="all SDK sources; sdist has pyproject/README; no server package tree",
    )
    return report


def _artifact_copy(source: Path, run_dir: Path, role: str) -> dict[str, Any]:
    destination_dir = run_dir / "artifacts"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{role}--{source.name}"
    if destination.exists():
        raise ProbeFailureError(f"artifact destination already exists: {destination.name}")
    shutil.copy2(source, destination)
    return {
        "role": role,
        "path": str(destination.relative_to(run_dir)),
        "filename": destination.name,
        "source_filename": source.name,
        "size_bytes": destination.stat().st_size,
        "sha256": _sha256_file(destination),
    }


def _pip_command(venv_python: Path, *arguments: str) -> list[str]:
    """Run base pip against a venv without switching the orchestrator Python."""
    return [sys.executable, "-m", "pip", "--python", str(venv_python), *arguments]


def _pip_requirement(wheel: Path, extra: str | None) -> str:
    if extra is None:
        return str(wheel)
    if extra not in {"ws", "frames"}:
        raise ValueError("unsupported SDK extra")
    return f"{EXPECTED_NAME}[{extra}] @ {wheel.resolve().as_uri()}"


def _site_packages(venv_root: Path) -> Path:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    if os.name == "nt":
        return venv_root / "Lib" / "site-packages"
    return venv_root / "lib" / version / "site-packages"


def _smoke_code() -> str:
    """Return installed-only REST/WS checks against local loopback services."""
    return r"""import base64, datetime, hashlib, http.server, importlib.metadata, importlib.util
import json, os, pathlib, sys, threading, urllib.parse

site = pathlib.Path(os.environ["SDK_PROBE_SITE"]).resolve()
checkout = pathlib.Path(os.environ["SDK_PROBE_CHECKOUT"]).resolve()
snapshot = pathlib.Path(os.environ["SDK_PROBE_SNAPSHOT"]).resolve()
variant = os.environ["SDK_PROBE_VARIANT"]

import httpx
import opendata_client
from opendata_client import (
    AuthenticationError, DataUpdate, OpendataClient, OpendataClientError, Page,
)

module_path = pathlib.Path(opendata_client.__file__).resolve()
leaked = []
for raw in sys.path:
    path = pathlib.Path(raw or os.getcwd()).resolve()
    if path == checkout or checkout in path.parents or path == snapshot or snapshot in path.parents:
        leaked.append(str(path))
if site not in module_path.parents or leaked:
    raise AssertionError({"module_path": str(module_path), "leaked_paths": leaked})

importlib.metadata.distribution("opendata-client")
importlib.metadata.version("httpx")
importlib.metadata.version("loguru")
has_pandas = importlib.util.find_spec("pandas") is not None
has_websockets = importlib.util.find_spec("websockets") is not None
expected_optional = {"rest": (False, False), "ws": (False, True), "frames": (True, False)}
if (has_pandas, has_websockets) != expected_optional[variant]:
    raise AssertionError({"variant": variant, "pandas": has_pandas, "websockets": has_websockets})

requests = []
request_lock = threading.Lock()
ws_seen = {}
PIT_AS_OF = "2024-04-02T00:00:00+00:00"
MODEL_QUERY = {
    "source": "fixture",
    "market": "US",
    "as_of": PIT_AS_OF,
    "filters": {"symbols": ["AAA", "BBB"], "include_inactive": False},
}

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def capture(self, body=None):
        parsed = urllib.parse.urlsplit(self.path)
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        record = {
            "method": self.command,
            "path": parsed.path,
            "params": params,
            "body": body,
            "api_key": self.headers.get("X-API-Key"),
        }
        with request_lock:
            requests.append(record)
        return record

    def read_websocket_text(self):
        header = self.rfile.read(2)
        if len(header) != 2:
            raise ConnectionError("truncated WebSocket frame header")
        first, second = header
        if not first & 0x80 or first & 0x0F != 1 or not second & 0x80:
            raise AssertionError("expected one masked, final WebSocket text frame")
        length = second & 0x7F
        if length == 126:
            extended = self.rfile.read(2)
            if len(extended) != 2:
                raise ConnectionError("truncated WebSocket frame length")
            length = int.from_bytes(extended, "big")
        elif length == 127:
            extended = self.rfile.read(8)
            if len(extended) != 8:
                raise ConnectionError("truncated WebSocket frame length")
            length = int.from_bytes(extended, "big")
        mask = self.rfile.read(4)
        payload = self.rfile.read(length)
        if len(mask) != 4 or len(payload) != length:
            raise ConnectionError("truncated WebSocket frame payload")
        decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return decoded.decode("utf-8")

    def write_websocket_json(self, payload):
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        length = len(encoded)
        if length < 126:
            header = bytes((0x81, length))
        elif length <= 65535:
            header = b"\x81\x7e" + length.to_bytes(2, "big")
        else:
            header = b"\x81\x7f" + length.to_bytes(8, "big")
        self.wfile.write(header + encoded)
        self.wfile.flush()

    def handle_websocket(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if self.headers.get("Upgrade", "").casefold() != "websocket" or not key:
            self.send_error(400, "invalid WebSocket upgrade")
            return
        self.close_connection = True
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        self.send_response_only(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        ws_seen["auth"] = json.loads(self.read_websocket_text())
        self.write_websocket_json({"type": "auth.ok"})
        ws_seen["subscribe"] = json.loads(self.read_websocket_text())
        self.write_websocket_json({"type": "subscribe.ok", "replayed": 0})
        self.write_websocket_json({
            "type": "data.update", "domain": "stock_daily", "source": "fixture",
            "layer": "dwd", "batch_id": "loopback-1",
            "window": {"start": "2024-01-02", "end": "2024-01-02"},
            "rows": 1, "created_at": "2024-01-02T00:00:01Z", "payload": "meta",
        })

    def do_GET(self):
        record = self.capture()
        if record["path"] == "/ws/data/subscribe":
            self.handle_websocket()
            return
        if record["api_key"] != "probe-api-key":
            return self.send_json(401, {"detail": "invalid API key"})

        path = record["path"]
        params = record["params"]
        if path == "/api/v1/data/capabilities":
            return self.send_json(200, [{"domain": "stock_daily", "asset_class": "equity"}])

        if path == "/api/v1/data/equity/stock_daily":
            if params.get("symbols"):
                page = int(params.get("page", "1"))
                if page == 1:
                    rows = [
                        {"symbol": "600519", "trade_date": "2024-01-02", "close": 10.0},
                        {"symbol": "600519", "trade_date": "2024-01-03", "close": 11.0},
                    ]
                    count = 2
                elif page == 2:
                    rows = [{"symbol": "600519", "trade_date": "2024-01-04", "close": 12.0}]
                    count = 1
                else:
                    rows, count = [], 0
            else:
                rows = [{"symbol": "600519", "trade_date": params.get("start"), "close": 13.0}]
                count = 1
            return self.send_json(200, {"success": True, "data": {
                "rows": rows, "columns": ["trade_date", "symbol", "close"],
                "page": int(params.get("page", "1")),
                "page_size": int(params.get("page_size", "500")), "count": count,
            }})

        if path == "/api/v1/data/equity/pit_probe":
            return self.send_json(200, {"success": True, "data": {
                "rows": [{"symbol": "600519", "revision": 3}],
                "columns": ["symbol", "revision"], "page": 1, "page_size": 5,
                "count": 1, "pit": {"as_of": params.get("as_of"), "source": params.get("source")},
            }})

        if path == "/api/v1/providers/models":
            if params.get("source") != "fixture":
                return self.send_json(400, {"detail": "source filter missing"})
            return self.send_json(200, [{"source": "fixture", "model": "PitModel"}])

        if path == "/api/v1/providers/fixture/models/PitModel/schema":
            return self.send_json(200, {"success": True, "data": {
                "source": "fixture", "model": "PitModel",
                "schema": {"type": "object", "properties": {"market": {"type": "string"}}},
            }})

        return self.send_json(404, {"detail": "unexpected route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        record = self.capture(body)
        if record["api_key"] != "probe-api-key":
            return self.send_json(401, {"detail": "invalid API key"})
        if record["path"] != "/api/v1/providers/fixture/models/PitModel/query":
            return self.send_json(404, {"detail": "unexpected model route"})
        if record["body"] != {"query": MODEL_QUERY}:
            return self.send_json(422, {"detail": "typed JSON query changed"})
        query = record["body"]["query"]
        return self.send_json(200, {"success": True, "data": {
            "source": "fixture", "model": "PitModel",
            "results": [{
                "source": query["source"], "market": query["market"],
                "as_of": query["as_of"],
            }],
            "pagination": {"total": 1, "offset": 0, "limit": 10},
        }})

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
server_thread = threading.Thread(target=server.serve_forever, daemon=True)
server_thread.start()
base_url = "http://127.0.0.1:%d" % server.server_address[1]
observed = {}

try:
    with OpendataClient(base_url, api_key="probe-api-key", timeout=5) as client:
        first = client.stock_daily("600519", start="2024-01-02", page_size=2)
        all_rows = client.stock_daily_all(
            "600519", start="2024-01-02", end="2024-01-31", source="fixture",
            page=2, page_size=2,
        )
        pit = client.query(
            "equity", "pit_probe", symbols="600519", source="fixture", market="US",
            as_of=datetime.datetime(2024, 4, 2, tzinfo=datetime.timezone.utc), page_size=5,
        )
        models = client.provider_models(source="fixture")
        schema = client.provider_model_schema("fixture", "PitModel")
        model_result = client.query_provider_model("fixture", "PitModel", MODEL_QUERY)

        if not isinstance(first, Page) or first.page != 1 or first.count != 2:
            raise AssertionError("REST query did not return a typed first page")
        if len(all_rows) != 3 or [row["close"] for row in all_rows] != [10.0, 11.0, 12.0]:
            raise AssertionError(
                {
                    "message": "REST SDK pagination did not fetch both pages",
                    "rows": all_rows,
                    "requests": requests,
                }
            )
        page_requests = [
            item for item in requests
            if item["path"].endswith("/stock_daily")
            and item["params"].get("symbols")
            and item["params"].get("end") == "2024-01-31"
        ]
        if [item["params"].get("page") for item in page_requests] != ["1", "2"]:
            raise AssertionError("stock_daily_all did not start at page 1 and follow pagination")
        if any(item["params"].get("page_size") != "2" for item in page_requests):
            raise AssertionError("stock_daily_all did not forward its requested page_size")
        if pit.pit != {"as_of": PIT_AS_OF, "source": "fixture"}:
            raise AssertionError("PIT metadata or timestamp was not preserved")
        if not models or models[0].get("model") != "PitModel" or schema.get("model") != "PitModel":
            raise AssertionError("provider model metadata methods returned the wrong identity")
        if model_result["results"] != [{"source": "fixture", "market": "US", "as_of": PIT_AS_OF}]:
            raise AssertionError("provider model query did not preserve typed JSON fields")

        bad_client = OpendataClient(base_url, api_key="wrong-key", timeout=5)
        try:
            try:
                bad_client.stock_daily("600519")
            except AuthenticationError as exc:
                if "invalid API key" not in str(exc):
                    raise AssertionError("unauthenticated error detail was lost") from exc
            else:
                raise AssertionError("invalid API key did not raise AuthenticationError")
        finally:
            bad_client.close()

        if variant == "ws":
            with client.subscribe(
                "stock_daily", symbols="600519", pull=True, page_size=2, timeout=5,
            ) as subscription:
                if not subscription.acknowledged:
                    raise AssertionError("WebSocket subscription was not acknowledged")
                update = next(iter(subscription))
            if update.batch_id != "loopback-1" or update.data != ({
                "symbol": "600519", "trade_date": "2024-01-02", "close": 13.0,
            },):
                raise AssertionError("WebSocket notification did not trigger an HTTP row pull")
            if ws_seen.get("auth") != {"action": "auth", "token": "probe-api-key"}:
                raise AssertionError("WebSocket auth handshake did not send the SDK token")
            if ws_seen.get("subscribe") != {
                "action": "subscribe", "domain": "stock_daily", "layer": "dwd",
                "payload": "meta", "symbols": ["600519"], "since_batch_id": None,
            }:
                raise AssertionError("WebSocket subscribe handshake differed from SDK contract")
            observed["websocket"] = {"auth": ws_seen["auth"], "subscribe": ws_seen["subscribe"]}

        if variant == "frames":
            frame = first.to_dataframe()
            if list(frame.columns) != ["trade_date", "symbol", "close"] or len(frame) != 2:
                raise AssertionError("frames extra did not convert Page with its column order")
            update = DataUpdate(
                domain="stock_daily", source="fixture", layer="dwd", batch_id="frame-1",
                window={"start": "2024-01-02", "end": "2024-01-02"}, rows=1,
                created_at="2024-01-02T00:00:01Z",
                data=({"symbol": "600519", "close": 13.0},),
            )
            if list(update.to_dataframe().columns) != ["symbol", "close"]:
                raise AssertionError("frames extra did not convert DataUpdate rows")

    with request_lock:
        captured = list(requests)
    pit_request = next(item for item in captured if item["path"].endswith("/pit_probe"))
    if (
        pit_request["params"].get("source") != "fixture"
        or pit_request["params"].get("market") != "US"
    ):
        raise AssertionError("PIT source and market parameters were not sent")
    if pit_request["params"].get("as_of") != PIT_AS_OF:
        raise AssertionError("PIT timestamp was not serialized as ISO-8601")
    model_request = next(item for item in captured if item["method"] == "POST")
    if model_request["body"] != {"query": MODEL_QUERY}:
        raise AssertionError("provider model JSON serialization changed the query")
    pull_requests = [
        item
        for item in captured
        if item["path"].endswith("/stock_daily") and not item["params"].get("symbols")
    ]
    if variant == "ws" and not pull_requests:
        raise AssertionError("WebSocket notification was not followed by REST pull")

    distribution = importlib.metadata.distribution("opendata-client")
    observed = {
        "variant": variant,
        "base_python_executable": sys.executable,
        "base_python_version": sys.version,
        "module_path": str(module_path),
        "isolated_site_packages": str(site),
        "distribution_version": distribution.version,
        "installed_versions": {
            name: importlib.metadata.version(name) for name in ("httpx", "loguru")
        },
        "optional_dependencies_present": {"pandas": has_pandas, "websockets": has_websockets},
        "source_path_leaks": leaked,
        "http_requests": captured,
        "http_pagination_rows": len(all_rows),
        "stock_daily_all_page_requests": [
            item for item in captured
            if item["path"].endswith("/stock_daily")
            and item["params"].get("symbols")
            and item["params"].get("end") == "2024-01-31"
        ],
        "pit_page": {"rows": pit.rows, "columns": pit.columns, "pit": pit.pit},
        "provider_models": models,
        "provider_model_schema": schema,
        "provider_model_result": model_result,
        "authentication_error": "HTTP 401: invalid API key",
        **observed,
    }
    print(json.dumps(observed, sort_keys=True, separators=(",", ":")))
finally:
    server.shutdown()
    server.server_close()
    server_thread.join(timeout=5)
"""


def _source_path_leaks(paths: Iterable[str], roots: Iterable[Path]) -> list[str]:
    resolved_roots = tuple(root.resolve() for root in roots)
    leaks: list[str] = []
    for raw in paths:
        path = Path(raw or os.getcwd()).resolve()
        if any(path == root or root in path.parents for root in resolved_roots):
            leaks.append(str(path))
    return leaks


def _run_smoke(
    *,
    state: dict[str, Any],
    stage: str,
    variant: str,
    site_packages: Path,
    snapshot_root: Path,
    work_dir: Path,
    timeout_seconds: int,
    run_dir: Path,
) -> JsonObject:
    environment = _loopback_environment()
    environment.update(
        {
            "PYTHONPATH": str(site_packages),
            "SDK_PROBE_SITE": str(site_packages),
            "SDK_PROBE_CHECKOUT": str(PROJECT_ROOT),
            "SDK_PROBE_SNAPSHOT": str(snapshot_root),
            "SDK_PROBE_VARIANT": variant,
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
    _require_command_success(state, stage)
    stdout_path = run_dir / record["stdout"]["path"]
    lines = [line for line in stdout_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        report = _parse_json_object(lines[-1], f"{stage} installed SDK smoke report")
    except (IndexError, ProbeFailureError) as exc:
        raise ProbeFailureError(f"{stage} did not emit loopback smoke JSON") from exc
    if report.get("source_path_leaks"):
        raise ProbeFailureError(f"{stage} detected checkout or build snapshot on sys.path")
    if report.get("variant") != variant:
        raise ProbeFailureError(f"{stage} reported the wrong install variant")
    state["installations"][stage] = report
    page_records = _json_object_list(
        report.get("stock_daily_all_page_requests"),
        f"{stage}.stock_daily_all_page_requests",
    )
    page_request_params: list[dict[str, str]] = []
    for index, item in enumerate(page_records):
        params = _json_object_field(
            item, "params", f"{stage}.stock_daily_all_page_requests[{index}]"
        )
        page = params.get("page")
        page_size = params.get("page_size")
        if not isinstance(page, str) or not isinstance(page_size, str):
            raise ProbeFailureError(f"{stage} page request fields must be text")
        page_request_params.append({"page": page, "page_size": page_size})
    expected_page_request_params = [
        {"page": "1", "page_size": "2"},
        {"page": "2", "page_size": "2"},
    ]
    _add_fact(
        state,
        f"{stage}_runs_installed_sdk_without_source_leak",
        bool(report.get("module_path")) and not report.get("source_path_leaks"),
        observed={
            "module_path": report.get("module_path"),
            "leaks": report.get("source_path_leaks"),
        },
        expected="SDK imports from isolated target; checkout and snapshot are absent from sys.path",
    )
    _add_fact(
        state,
        f"{stage}_stock_daily_all_forwards_page_size_and_starts_at_one",
        page_request_params == expected_page_request_params,
        observed=page_request_params,
        expected=expected_page_request_params,
    )
    pit_page = _json_object_field(report, "pit_page", f"{stage}.report")
    pit = _json_object_field(pit_page, "pit", f"{stage}.pit_page")
    optional_dependencies = _json_object_field(
        report, "optional_dependencies_present", f"{stage}.report"
    )
    _add_fact(
        state,
        f"{stage}_real_loopback_sdk_contract_passes",
        report.get("http_pagination_rows") == 3
        and pit.get("as_of") == "2024-04-02T00:00:00+00:00"
        and (variant != "ws" or bool(report.get("websocket")))
        and (variant != "frames" or optional_dependencies.get("pandas") is True),
        observed={
            "pagination_rows": report.get("http_pagination_rows"),
            "pit": pit,
            "websocket": bool(report.get("websocket")),
            "optional_dependencies_present": optional_dependencies,
        },
        expected=(
            "SDK pagination, PIT, model metadata/query and selected optional feature "
            "work over loopback"
        ),
    )
    return report


def _create_target(
    *,
    state: dict[str, Any],
    name: str,
    workspace: Path,
    timeout_seconds: int,
    run_dir: Path,
) -> tuple[Path, Path]:
    venv_root = workspace / f"venv-{name}"
    _run_command(
        state=state,
        name=f"{name}_create_isolated_venv",
        command=[sys.executable, "-m", "venv", str(venv_root)],
        cwd=workspace,
        environment=_clean_environment(),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    _require_command_success(state, f"{name}_create_isolated_venv")
    venv_python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    config_path = venv_root / "pyvenv.cfg"
    config = config_path.read_text(encoding="utf-8").casefold()
    system_packages = any(
        line.strip() == "include-system-site-packages = true" for line in config.splitlines()
    )
    _add_fact(
        state,
        f"{name}_target_disables_system_site_packages",
        venv_python.is_file() and not system_packages,
        observed={"python_exists": venv_python.is_file(), "system_site_packages": system_packages},
        expected={"python_exists": True, "system_site_packages": False},
    )
    target = {
        "venv_root": str(venv_root),
        "python": str(venv_python),
        "pyvenv_cfg_sha256": _sha256_file(config_path),
        "system_site_packages_enabled": system_packages,
        "orchestrator_python": sys.executable,
    }
    state["installations"][f"{name}_target"] = target
    return venv_python, _site_packages(venv_root)


def _install_variant(
    *,
    state: dict[str, Any],
    name: str,
    wheel: Path,
    extra: str | None,
    expected_variant: str,
    workspace: Path,
    timeout_seconds: int,
    snapshot_root: Path,
    run_dir: Path,
) -> None:
    venv_python, site_packages = _create_target(
        state=state,
        name=name,
        workspace=workspace,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    requirement = _pip_requirement(wheel, extra)
    _run_command(
        state=state,
        name=f"{name}_install",
        command=_pip_command(
            venv_python,
            "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--index-url",
            "https://pypi.org/simple",
            requirement,
        ),
        cwd=workspace,
        environment=_clean_environment(isolated_pip=True),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    _require_command_success(state, f"{name}_install")
    _run_command(
        state=state,
        name=f"{name}_pip_check",
        command=_pip_command(venv_python, "check"),
        cwd=workspace,
        environment=_clean_environment(isolated_pip=True),
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )
    check = _require_command_success(state, f"{name}_pip_check")
    _add_fact(
        state,
        f"{name}_pip_dependency_check_passes",
        check["exit_code"] == 0 and not check["timed_out"],
        observed={"exit_code": check["exit_code"], "timed_out": check["timed_out"]},
        expected={"exit_code": 0, "timed_out": False},
    )
    _run_smoke(
        state=state,
        stage=f"{name}_loopback_smoke",
        variant=expected_variant,
        site_packages=site_packages,
        snapshot_root=snapshot_root,
        work_dir=workspace,
        timeout_seconds=timeout_seconds,
        run_dir=run_dir,
    )


def run_probe(
    *,
    evidence_root: Path = EVIDENCE_ROOT,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build artifacts, install all package variants and persist measured evidence."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_dir = evidence_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    state: dict[str, Any] = {
        "schema": "opendata-sdk-package-probe/v2",
        "probe": "AC2-20 independent SDK clean-install and loopback probe",
        "run_id": run_id,
        "status": "RUNNING",
        "started_at_utc": _utc_now(),
        "finished_at_utc": None,
        "project_root": str(PROJECT_ROOT),
        "sdk_root": str(SDK_ROOT),
        "base_python": {"executable": sys.executable, "version": sys.version, "prefix": sys.prefix},
        "timeout_seconds_per_command": timeout_seconds,
        "source": {},
        "commands": {},
        "archives": {},
        "artifacts": [],
        "positive_assertions": [],
        "synthetic_controls": [],
        "artifact_mutations": [],
        "installations": {},
        "failure": None,
    }
    _write_json(result_path, state)
    original_manifest: dict[str, Any] | None = None
    try:
        if not importlib.util.find_spec("build"):
            raise ProbeFailureError("base Python must provide the PEP 517 build frontend")
        if not importlib.util.find_spec("setuptools") or not importlib.util.find_spec("wheel"):
            raise ProbeFailureError("base Python must provide setuptools and wheel")

        with tempfile.TemporaryDirectory(prefix="opendata-sdk-package-") as temporary:
            workspace = Path(temporary).resolve()
            original_manifest = _source_manifest(SDK_ROOT)
            snapshot_root = workspace / "sdk-source"
            _copy_source_snapshot(SDK_ROOT, snapshot_root)
            snapshot_root = snapshot_root.resolve()
            snapshot_manifest = _source_manifest(snapshot_root)
            state["source"] = {
                "snapshot_path": str(snapshot_root),
                "snapshot_retained": False,
                "snapshot_outside_checkout": not _is_within(snapshot_root, PROJECT_ROOT),
                "fingerprint_before": original_manifest["fingerprint"],
                "fingerprint_snapshot": snapshot_manifest["fingerprint"],
                "file_count": snapshot_manifest["file_count"],
                "manifest_path": "source-manifest.json",
            }
            _write_json(run_dir / "source-manifest.json", snapshot_manifest)
            _add_fact(
                state,
                "sdk_source_snapshot_matches_and_is_outside_checkout",
                state["source"]["snapshot_outside_checkout"]
                and original_manifest["fingerprint"] == snapshot_manifest["fingerprint"],
                observed={
                    "outside_checkout": state["source"]["snapshot_outside_checkout"],
                    "checkout": original_manifest["fingerprint"],
                    "snapshot": snapshot_manifest["fingerprint"],
                },
                expected="identical SDK build inputs copied outside the repository",
            )

            git = _run_command(
                state=state,
                name="git_head",
                command=["git", "rev-parse", "HEAD"],
                cwd=PROJECT_ROOT,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            if not git["timed_out"] and git["exit_code"] == 0:
                state["source"]["git_head"] = (
                    (run_dir / git["stdout"]["path"]).read_text(encoding="utf-8").strip()
                )
            else:
                state["source"]["git_head"] = None

            dist = workspace / "dist"
            dist.mkdir()
            _run_command(
                state=state,
                name="build_wheel_and_sdist",
                command=[
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--wheel",
                    "--sdist",
                    "--outdir",
                    str(dist),
                ],
                cwd=snapshot_root,
                environment=_clean_environment(),
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _require_command_success(state, "build_wheel_and_sdist")
            wheels = sorted(dist.glob("*.whl"))
            sdists = sorted(dist.glob("*.tar.gz"))
            if len(wheels) != 1 or len(sdists) != 1:
                raise ProbeFailureError(
                    f"expected exactly one wheel and sdist; found {wheels} and {sdists}"
                )
            wheel, sdist = wheels[0], sdists[0]
            source_wheel_artifact = _artifact_copy(wheel, run_dir, "source-build-wheel")
            state["artifacts"].append(source_wheel_artifact)
            state["artifacts"].append(_artifact_copy(sdist, run_dir, "source-build-sdist"))
            _validate_archive(
                state=state, artifact=wheel, kind="wheel", snapshot_root=snapshot_root
            )
            _validate_archive(
                state=state, artifact=sdist, kind="sdist", snapshot_root=snapshot_root
            )

            rebuilt_dir = workspace / "sdist-rebuilt-wheel"
            rebuilt_dir.mkdir()
            _run_command(
                state=state,
                name="rebuild_wheel_from_sdist",
                command=[
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(rebuilt_dir),
                    str(sdist),
                ],
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
            state["artifacts"].append(_artifact_copy(rebuilt_wheel, run_dir, "sdist-rebuilt-wheel"))
            _validate_archive(
                state=state,
                artifact=rebuilt_wheel,
                kind="sdist_rebuilt_wheel",
                snapshot_root=snapshot_root,
            )

            _run_missing_loguru_artifact_control(
                state=state,
                source_wheel=wheel,
                source_artifact=source_wheel_artifact,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )

            # Synthetic controls remain separate from mutations of real artifacts.
            _, source_metadata = _metadata_from_archive(wheel, "wheel")
            missing_loguru = re.sub(
                r"(?im)^Requires-Dist:\s*loguru>=0\.7\s*\n", "", source_metadata, count=1
            )
            metadata_negative_detected = bool(_metadata_contract_issues(missing_loguru))
            _add_synthetic_control(
                state,
                "synthetic_metadata_text_missing_loguru_is_rejected",
                metadata_negative_detected,
                mutation={
                    "kind": "remove_line_from_in_memory_metadata_text",
                    "source_artifact_sha256": source_wheel_artifact["sha256"],
                },
                observed={"issues": _metadata_contract_issues(missing_loguru)},
                expected="metadata checker reports the missing loguru>=0.7 dependency",
            )
            leak_checker_result = _source_path_leaks(
                [str(PROJECT_ROOT)], [PROJECT_ROOT, snapshot_root]
            )
            _add_synthetic_control(
                state,
                "synthetic_path_list_checkout_contamination_is_rejected",
                bool(leak_checker_result),
                mutation={"kind": "pass_checkout_path_string_to_path_checker"},
                observed=leak_checker_result,
                expected="checkout path is reported when supplied as a checker input",
            )

            _install_variant(
                state=state,
                name="rest_wheel",
                wheel=wheel,
                extra=None,
                expected_variant="rest",
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                snapshot_root=snapshot_root,
                run_dir=run_dir,
            )
            _run_checkout_pollution_control(
                state=state,
                source_artifact=source_wheel_artifact,
                target=state["installations"]["rest_wheel_target"],
                snapshot_root=snapshot_root,
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                run_dir=run_dir,
            )
            _install_variant(
                state=state,
                name="ws_extra_wheel",
                wheel=wheel,
                extra="ws",
                expected_variant="ws",
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                snapshot_root=snapshot_root,
                run_dir=run_dir,
            )
            _install_variant(
                state=state,
                name="frames_extra_wheel",
                wheel=wheel,
                extra="frames",
                expected_variant="frames",
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                snapshot_root=snapshot_root,
                run_dir=run_dir,
            )
            _install_variant(
                state=state,
                name="rest_sdist_rebuilt_wheel",
                wheel=rebuilt_wheel,
                extra=None,
                expected_variant="rest",
                workspace=workspace,
                timeout_seconds=timeout_seconds,
                snapshot_root=snapshot_root,
                run_dir=run_dir,
            )

    except Exception as exc:  # Persist an honest partial run and diagnostics.
        state["status"] = "FAIL"
        state["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if original_manifest is not None:
            try:
                final_manifest = _source_manifest(SDK_ROOT)
                state["source"]["fingerprint_after"] = final_manifest["fingerprint"]
                state["source"]["source_hash_stable"] = (
                    final_manifest["fingerprint"] == original_manifest["fingerprint"]
                )
                _write_json(run_dir / "source-manifest-after.json", final_manifest)
                stable = state["source"]["source_hash_stable"]
                state["positive_assertions"].append(
                    {
                        "name": "sdk_source_fingerprint_unchanged_during_probe",
                        "category": "positive_assertion",
                        "status": "PASS" if stable else "FAIL",
                        "observed": final_manifest["fingerprint"],
                        "expected": original_manifest["fingerprint"],
                    }
                )
                if not stable:
                    state["status"] = "FAIL"
                    state["failure"] = {
                        "type": "ProbeFailureError",
                        "message": "SDK build source changed while the probe was running",
                    }
            except Exception as exc:
                state["status"] = "FAIL"
                state["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        if state["status"] == "RUNNING":
            state["status"] = "PASS"
        state["assertion_counts"] = {
            "positive_assertions": len(state["positive_assertions"]),
            "synthetic_controls": len(state["synthetic_controls"]),
            "artifact_mutations": len(state["artifact_mutations"]),
        }
        state["finished_at_utc"] = _utc_now()
        _write_json(result_path, state)
    return state


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE_ROOT)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    """Run the SDK package probe and return a shell-friendly status code."""
    args = _parse_args(argv)
    if args.timeout_seconds <= 0:
        print("timeout-seconds must be positive", file=sys.stderr)
        return 2
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
