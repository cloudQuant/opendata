"""Check base Python and frozen quality-tool versions without changing the environment."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil

# This import is used only by the fixed, validated version probes below.
import subprocess  # nosec B404
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomllib

TOOLS = {
    "ruff": {"distribution": "ruff", "module": "ruff"},
    "mypy": {"distribution": "mypy", "module": "mypy"},
    "bandit": {"distribution": "bandit", "module": "bandit"},
}
BASE_PYTHON = (3, 11)
VERSION_PATTERN = re.compile(r"(?<![A-Za-z0-9])([0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.+-]*)?)")
REQUIREMENT_PATTERN = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(.*?)\s*$")
PYTHON_SPECIFIER_PATTERN = re.compile(r"^(~=|==|!=|>=|<=|>|<)\s*(\d+(?:\.\d+)*)$")


def _issue(code: str, message: str, **details: object) -> dict[str, Any]:
    """Build one JSON-safe preflight issue."""
    result: dict[str, Any] = {"code": code, "message": message}
    result.update(details)
    return result


def _version_tuple(value: str) -> tuple[int, ...] | None:
    """Parse the numeric prefix of a Python version specifier."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    """Compare numeric versions after padding missing components with zeroes."""
    width = max(len(left), len(right))
    normalized_left = left + (0,) * (width - len(left))
    normalized_right = right + (0,) * (width - len(right))
    return (normalized_left > normalized_right) - (normalized_left < normalized_right)


def python_requirement_supports_311(requirement: str) -> tuple[bool, str | None]:
    """Check whether a simple PEP 440 Python requirement admits Python 3.11."""
    clauses = requirement.split(",")
    for clause in clauses:
        match = PYTHON_SPECIFIER_PATTERN.fullmatch(clause.strip())
        if match is None:
            return False, f"unsupported requires-python clause: {clause.strip()}"
        operator, raw_version = match.groups()
        candidate = _version_tuple(raw_version)
        if candidate is None:
            return False, f"invalid requires-python version: {raw_version}"
        comparison = _compare_versions(BASE_PYTHON, candidate)
        if operator == ">=" and comparison < 0:
            return False, f"Python 3.11 is below {raw_version}"
        if operator == ">" and comparison <= 0:
            return False, f"Python 3.11 is not above {raw_version}"
        if operator == "<=" and comparison > 0:
            return False, f"Python 3.11 is above {raw_version}"
        if operator == "<" and comparison >= 0:
            return False, f"Python 3.11 is not below {raw_version}"
        if operator == "==" and comparison != 0:
            return False, f"Python 3.11 does not equal {raw_version}"
        if operator == "!=" and comparison == 0:
            return False, f"Python 3.11 is excluded by {raw_version}"
        if operator == "~=":
            prefix = candidate[:-1]
            actual_prefix = BASE_PYTHON[: len(prefix)]
            if comparison < 0 or actual_prefix != prefix:
                return False, f"Python 3.11 is outside compatible release {raw_version}"
    return True, None


def _extract_version(value: object) -> str | None:
    """Return the first semantic version from a tool version string."""
    if not isinstance(value, str):
        return None
    match = VERSION_PATTERN.search(value)
    return match.group(1) if match is not None else None


def expected_tool_versions(
    project_data: Mapping[str, Any], ratchet_data: Mapping[str, Any]
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Derive exact tool pins from pyproject and compare them with ratchet values."""
    issues: list[dict[str, Any]] = []
    optional_dependencies = project_data.get("project", {}).get("optional-dependencies", {})
    dev_requirements = optional_dependencies.get("dev", [])
    if not isinstance(dev_requirements, list):
        dev_requirements = []
        issues.append(
            _issue(
                "DEV_DEPENDENCIES_INVALID",
                "project.optional-dependencies.dev must be a list.",
            )
        )

    project_pins: dict[str, list[str]] = {name: [] for name in TOOLS}
    for requirement in dev_requirements:
        if not isinstance(requirement, str):
            continue
        match = REQUIREMENT_PATTERN.fullmatch(requirement)
        if match is None:
            continue
        name, specifier = match.groups()
        normalized_name = name.casefold().replace("_", "-").replace(".", "-")
        if normalized_name not in TOOLS:
            continue
        pin_match = re.fullmatch(r"==\s*(\d+\.\d+\.\d+)", specifier)
        if pin_match is None:
            issues.append(
                _issue(
                    "PROJECT_PIN_NOT_EXACT",
                    "Quality tool must have an exact version pin in the dev extra.",
                    tool=normalized_name,
                    requirement=requirement,
                )
            )
            continue
        project_pins[normalized_name].append(pin_match.group(1))

    ratchet_tools = ratchet_data.get("tools", {})
    if not isinstance(ratchet_tools, Mapping):
        ratchet_tools = {}
        issues.append(_issue("RATCHET_TOOLS_INVALID", "ratchet.json tools must be an object."))

    versions: dict[str, str] = {}
    for name in TOOLS:
        pins = project_pins[name]
        if len(pins) != 1:
            issues.append(
                _issue(
                    "PROJECT_PIN_COUNT",
                    "Expected exactly one exact dev pin for the quality tool.",
                    tool=name,
                    count=len(pins),
                )
            )
        project_version = pins[0] if len(pins) == 1 else None
        ratchet_version = _extract_version(ratchet_tools.get(name))
        if ratchet_version is None:
            issues.append(
                _issue(
                    "RATCHET_VERSION_MISSING",
                    "ratchet.json does not contain a parseable frozen tool version.",
                    tool=name,
                )
            )
        if project_version is not None and ratchet_version is not None:
            if project_version != ratchet_version:
                issues.append(
                    _issue(
                        "FREEZE_CONFLICT",
                        "pyproject dev pin and ratchet tool version disagree.",
                        tool=name,
                        pyproject=project_version,
                        ratchet=ratchet_version,
                    )
                )
            versions[name] = project_version

    return versions, issues


def _is_within(path_value: str, parent_value: str) -> bool:
    """Return whether a resolved path is inside a resolved directory."""
    try:
        Path(path_value).resolve().relative_to(Path(parent_value).resolve())
    except (OSError, ValueError):
        return False
    return True


def evaluate_toolchain(
    project_data: Mapping[str, Any],
    ratchet_data: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate injectable metadata and runtime observations for PRE2-02."""
    expected, issues = expected_tool_versions(project_data, ratchet_data)
    project = project_data.get("project", {})
    requires_python = project.get("requires-python")
    if not isinstance(requires_python, str):
        issues.append(
            _issue(
                "PYTHON_REQUIREMENT_MISSING",
                "pyproject.toml project.requires-python is missing or invalid.",
            )
        )
    else:
        compatible, reason = python_requirement_supports_311(requires_python)
        if not compatible:
            issues.append(
                _issue(
                    "PYTHON_REQUIREMENT_CONFLICT",
                    "Project requirement does not admit the required Python 3.11 base.",
                    requires_python=requires_python,
                    reason=reason,
                )
            )

    interpreter = runtime.get("interpreter", {})
    if not isinstance(interpreter, Mapping):
        interpreter = {}
    python_version = interpreter.get("python_version")
    if tuple(python_version or ())[:2] != BASE_PYTHON:
        issues.append(
            _issue(
                "PYTHON_VERSION_MISMATCH",
                "The active base interpreter must be Python 3.11.",
                expected="3.11",
                actual=".".join(str(part) for part in (python_version or ())),
            )
        )
    if interpreter.get("conda_default_env") != "base":
        issues.append(
            _issue(
                "CONDA_ENV_MISMATCH",
                "The active interpreter is not running in the Anaconda base environment.",
                expected="base",
                actual=interpreter.get("conda_default_env"),
            )
        )

    prefix = interpreter.get("prefix")
    conda_prefix = interpreter.get("conda_prefix")
    executable = interpreter.get("executable")
    if not isinstance(prefix, str) or not isinstance(conda_prefix, str):
        issues.append(
            _issue(
                "BASE_PREFIX_MISSING",
                "Could not verify the active interpreter against CONDA_PREFIX.",
            )
        )
    elif Path(prefix).resolve() != Path(conda_prefix).resolve():
        issues.append(
            _issue(
                "BASE_PREFIX_MISMATCH",
                "sys.prefix and CONDA_PREFIX do not identify the same environment.",
                sys_prefix=prefix,
                conda_prefix=conda_prefix,
            )
        )
    if (
        not isinstance(executable, str)
        or not isinstance(prefix, str)
        or not _is_within(executable, prefix)
    ):
        issues.append(
            _issue(
                "PYTHON_EXECUTABLE_OUTSIDE_BASE",
                "sys.executable is not inside the active base prefix.",
                executable=executable,
                prefix=prefix,
            )
        )

    runtime_tools = runtime.get("tools", {})
    if not isinstance(runtime_tools, Mapping):
        runtime_tools = {}
    tool_report: dict[str, Any] = {}
    for name, spec in TOOLS.items():
        observation = runtime_tools.get(name, {})
        if not isinstance(observation, Mapping):
            observation = {}
        expected_version = expected.get(name)
        values = {
            "expected": expected_version,
            "distribution": observation.get("distribution_version"),
            "interpreter_cli": observation.get("interpreter_cli_version"),
            "path_cli": observation.get("path_cli_version"),
            "path_cli_executable": observation.get("path_cli_executable"),
            "module": spec["module"],
        }
        tool_report[name] = values
        if expected_version is None:
            continue
        for field, display in (
            ("distribution_version", "installed distribution"),
            ("interpreter_cli_version", "sys.executable module CLI"),
            ("path_cli_version", "PATH CLI"),
        ):
            observed_version = observation.get(field)
            if observed_version is None:
                issues.append(
                    _issue(
                        "TOOL_UNAVAILABLE",
                        f"{display} version could not be read.",
                        tool=name,
                        source=field,
                    )
                )
            elif observed_version != expected_version:
                issues.append(
                    _issue(
                        "TOOL_VERSION_MISMATCH",
                        f"{display} does not match the frozen version.",
                        tool=name,
                        source=field,
                        expected=expected_version,
                        actual=observed_version,
                    )
                )
        cli_path = observation.get("path_cli_executable")
        if (
            not isinstance(cli_path, str)
            or not isinstance(prefix, str)
            or not _is_within(cli_path, prefix)
        ):
            issues.append(
                _issue(
                    "TOOL_CLI_OUTSIDE_BASE",
                    "The PATH CLI is not installed inside the active base prefix.",
                    tool=name,
                    executable=cli_path,
                    prefix=prefix,
                )
            )

    return {
        "status": "PASS" if not issues else "ENV_BLOCKED",
        "interpreter": {
            "executable": executable,
            "python_version": python_version,
            "conda_default_env": interpreter.get("conda_default_env"),
            "conda_prefix": conda_prefix,
            "sys_prefix": prefix,
        },
        "requires_python": requires_python,
        "tools": tool_report,
        "issues": issues,
    }


def _resolved_file_within(path_value: str, prefix_value: str) -> Path | None:
    """Resolve a regular file and return it only when it is inside ``prefix_value``."""
    try:
        resolved_path = Path(path_value).resolve(strict=True)
        resolved_prefix = Path(prefix_value).resolve(strict=True)
        resolved_path.relative_to(resolved_prefix)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_path if resolved_path.is_file() else None


def _parse_command_version(
    command: Sequence[str],
    *,
    tool_name: str | None = None,
    discovered_path: str | None = None,
    prefix: str | None = None,
) -> str | None:
    """Run only fixed module or discovered in-base tool version commands."""
    arguments = tuple(command)
    active_prefix = sys.prefix if prefix is None else prefix
    python_executable = sys.executable
    python_path = _resolved_file_within(python_executable, active_prefix)
    module_names = {spec["module"] for spec in TOOLS.values()}
    is_module_version = (
        python_path is not None
        and len(arguments) == 4
        and arguments[0] == python_executable
        and arguments[1] == "-m"
        and arguments[2] in module_names
        and arguments[3] == "--version"
    )

    resolved_cli = None
    if (
        tool_name in TOOLS
        and isinstance(discovered_path, str)
        and len(arguments) == 2
        and arguments[0] == discovered_path
        and arguments[1] == "--version"
    ):
        resolved_cli = _resolved_file_within(discovered_path, active_prefix)
    is_discovered_cli_version = resolved_cli is not None and resolved_cli.name == tool_name
    if not (is_module_version or is_discovered_cli_version):
        return None

    try:
        # Only validated interpreter/tool paths and fixed --version argv reach this call.
        result = subprocess.run(  # noqa: S603  # nosec B603
            list(arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _extract_version(f"{result.stdout}\n{result.stderr}")


def capture_runtime() -> dict[str, Any]:
    """Read interpreter, installed distributions, module CLIs, and PATH CLIs."""
    conda_prefix = os.environ.get("CONDA_PREFIX")
    interpreter: dict[str, Any] = {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "python_version": list(sys.version_info[:3]),
        "conda_default_env": os.environ.get("CONDA_DEFAULT_ENV"),
        "conda_prefix": conda_prefix,
    }
    tools: dict[str, Any] = {}
    for name, spec in TOOLS.items():
        try:
            distribution_version = importlib.metadata.version(spec["distribution"])
        except importlib.metadata.PackageNotFoundError:
            distribution_version = None
        interpreter_cli_version = _parse_command_version(
            [sys.executable, "-m", spec["module"], "--version"]
        )
        path_cli = shutil.which(name)
        path_cli_version = (
            _parse_command_version(
                [path_cli, "--version"], tool_name=name, discovered_path=path_cli
            )
            if path_cli
            else None
        )
        tools[name] = {
            "distribution_version": distribution_version,
            "interpreter_cli_version": interpreter_cli_version,
            "path_cli_version": path_cli_version,
            "path_cli_executable": path_cli,
        }
    return {"interpreter": interpreter, "tools": tools}


def build_environment_report(project_path: Path, ratchet_path: Path) -> dict[str, Any]:
    """Load project freeze metadata and evaluate the current process environment."""
    with project_path.open("rb") as handle:
        project_data = tomllib.load(handle)
    with ratchet_path.open("r", encoding="utf-8") as handle:
        ratchet_data = json.load(handle)
    report = evaluate_toolchain(project_data, ratchet_data, capture_runtime())
    report["metadata"] = {
        "pyproject": str(project_path),
        "ratchet": str(ratchet_path),
    }
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Run the read-only local toolchain preflight."""
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyproject", type=Path, default=root / "pyproject.toml")
    parser.add_argument("--ratchet", type=Path, default=root / "docs/quality/ratchet.json")
    args = parser.parse_args(argv)
    try:
        report = build_environment_report(args.pyproject, args.ratchet)
    except (OSError, UnicodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        report = {
            "status": "ENV_BLOCKED",
            "metadata": {"pyproject": str(args.pyproject), "ratchet": str(args.ratchet)},
            "issues": [
                _issue(
                    "PREFLIGHT_METADATA_UNAVAILABLE",
                    "Could not read pyproject/ratchet toolchain metadata.",
                    error=type(exc).__name__,
                )
            ],
        }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
