"""Synthetic tests for frozen toolchain and base-interpreter preflight."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from scripts.quality import toolchain_preflight
from scripts.quality.toolchain_preflight import evaluate_toolchain

if TYPE_CHECKING:
    from collections.abc import Mapping


def _metadata() -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Return internally consistent synthetic project and ratchet metadata."""
    versions = {"ruff": "7.1.2", "mypy": "8.2.3", "bandit": "6.4.5"}
    project = {
        "project": {
            "requires-python": ">=3.10",
            "optional-dependencies": {
                "dev": [f"{name}=={version}" for name, version in versions.items()]
            },
        }
    }
    ratchet = {
        "tools": {
            "ruff": f"ruff {versions['ruff']}",
            "mypy": f"mypy {versions['mypy']} (compiled: yes)",
            "bandit": f"__main__.py {versions['bandit']}",
        }
    }
    return project, ratchet, versions


def _runtime(versions: Mapping[str, str]) -> dict[str, Any]:
    """Return a synthetic Anaconda base runtime matching the injected pins."""
    prefix = "/opt/anaconda3"
    return {
        "interpreter": {
            "executable": f"{prefix}/bin/python",
            "prefix": prefix,
            "python_version": [3, 11, 8],
            "conda_default_env": "base",
            "conda_prefix": prefix,
        },
        "tools": {
            name: {
                "distribution_version": version,
                "interpreter_cli_version": version,
                "path_cli_version": version,
                "path_cli_executable": f"{prefix}/bin/{name}",
            }
            for name, version in versions.items()
        },
    }


def test_matching_injected_metadata_and_base_runtime_pass() -> None:
    project, ratchet, versions = _metadata()

    report = evaluate_toolchain(project, ratchet, _runtime(versions))

    assert report["status"] == "PASS"
    assert report["issues"] == []
    assert report["tools"]["ruff"]["expected"] == versions["ruff"]


def test_wrong_base_python_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    runtime = _runtime(versions)
    runtime["interpreter"]["python_version"] = [3, 13, 0]

    report = evaluate_toolchain(project, ratchet, runtime)

    assert report["status"] == "ENV_BLOCKED"
    assert any(issue["code"] == "PYTHON_VERSION_MISMATCH" for issue in report["issues"])


def test_missing_tool_distribution_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    runtime = _runtime(versions)
    runtime["tools"]["mypy"]["distribution_version"] = None

    report = evaluate_toolchain(project, ratchet, runtime)

    assert report["status"] == "ENV_BLOCKED"
    assert any(
        issue["code"] == "TOOL_UNAVAILABLE"
        and issue["tool"] == "mypy"
        and issue["source"] == "distribution_version"
        for issue in report["issues"]
    )


def test_cli_version_drift_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    runtime = _runtime(versions)
    runtime["tools"]["bandit"]["path_cli_version"] = "6.4.6"

    report = evaluate_toolchain(project, ratchet, runtime)

    assert report["status"] == "ENV_BLOCKED"
    assert any(
        issue["code"] == "TOOL_VERSION_MISMATCH"
        and issue["tool"] == "bandit"
        and issue["source"] == "path_cli_version"
        for issue in report["issues"]
    )


def test_pyproject_and_ratchet_freeze_conflict_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    conflicting_ratchet = deepcopy(ratchet)
    conflicting_ratchet["tools"]["ruff"] = "ruff 7.1.3"

    report = evaluate_toolchain(project, conflicting_ratchet, _runtime(versions))

    assert report["status"] == "ENV_BLOCKED"
    assert any(
        issue["code"] == "FREEZE_CONFLICT" and issue["tool"] == "ruff" for issue in report["issues"]
    )


def test_non_exact_dev_requirement_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    project["project"]["optional-dependencies"]["dev"][0] = "ruff>=7.1.2"

    report = evaluate_toolchain(project, ratchet, _runtime(versions))

    assert report["status"] == "ENV_BLOCKED"
    assert any(
        issue["code"] in {"PROJECT_PIN_NOT_EXACT", "PROJECT_PIN_COUNT"}
        and issue.get("tool") == "ruff"
        for issue in report["issues"]
    )


def test_project_requirement_that_excludes_python_311_is_blocked() -> None:
    project, ratchet, versions = _metadata()
    project["project"]["requires-python"] = ">=3.12"

    report = evaluate_toolchain(project, ratchet, _runtime(versions))

    assert report["status"] == "ENV_BLOCKED"
    assert any(issue["code"] == "PYTHON_REQUIREMENT_CONFLICT" for issue in report["issues"])


def test_non_base_or_external_cli_path_is_environment_blocked() -> None:
    project, ratchet, versions = _metadata()
    runtime = _runtime(versions)
    runtime["interpreter"]["conda_default_env"] = "other"
    runtime["tools"]["ruff"]["path_cli_executable"] = "/usr/local/bin/ruff"

    report = evaluate_toolchain(project, ratchet, runtime)

    assert report["status"] == "ENV_BLOCKED"
    codes = {issue["code"] for issue in report["issues"]}
    assert "CONDA_ENV_MISMATCH" in codes
    assert "TOOL_CLI_OUTSIDE_BASE" in codes


def test_fixed_module_version_command_is_non_shell_and_timed(monkeypatch: Any) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **options: Any) -> SimpleNamespace:
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout="ruff 7.1.2", stderr="")

    monkeypatch.setattr(toolchain_preflight.subprocess, "run", fake_run)
    command = [toolchain_preflight.sys.executable, "-m", "ruff", "--version"]

    assert toolchain_preflight._parse_command_version(command) == "7.1.2"
    assert len(calls) == 1
    assert calls[0][0] == command
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 10
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True


@pytest.mark.parametrize(
    "command",
    [
        [toolchain_preflight.sys.executable, "-m", "os", "--version"],
        [toolchain_preflight.sys.executable, "-m", "ruff", "--version", "--json"],
        [toolchain_preflight.sys.executable, "-c", "import os"],
    ],
)
def test_arbitrary_module_and_extra_arguments_are_rejected_before_run(
    command: list[str], monkeypatch: Any
) -> None:
    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unapproved version command reached subprocess.run")

    monkeypatch.setattr(toolchain_preflight.subprocess, "run", unexpected_run)

    assert toolchain_preflight._parse_command_version(command) is None


def test_path_cli_requires_discovered_in_prefix_file_and_exact_argv(
    tmp_path: Any, monkeypatch: Any
) -> None:
    prefix = tmp_path / "base"
    cli = prefix / "bin" / "ruff"
    cli.parent.mkdir(parents=True)
    cli.write_text("stub", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **options: Any) -> SimpleNamespace:
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout="ruff 7.1.2", stderr="")

    monkeypatch.setattr(toolchain_preflight.subprocess, "run", fake_run)
    command = [str(cli), "--version"]
    assert (
        toolchain_preflight._parse_command_version(
            command,
            tool_name="ruff",
            discovered_path=str(cli),
            prefix=str(prefix),
        )
        == "7.1.2"
    )
    assert calls[0][0] == command
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] == 10

    calls.clear()
    assert (
        toolchain_preflight._parse_command_version(
            [str(cli), "--version", "--verbose"],
            tool_name="ruff",
            discovered_path=str(cli),
            prefix=str(prefix),
        )
        is None
    )
    assert calls == []
    assert (
        toolchain_preflight._parse_command_version(
            command,
            tool_name="unknown",
            discovered_path=str(cli),
            prefix=str(prefix),
        )
        is None
    )
    assert calls == []


def test_path_cli_symlink_escape_is_rejected_before_run(tmp_path: Any, monkeypatch: Any) -> None:
    prefix = tmp_path / "base"
    external_cli = tmp_path / "outside" / "ruff"
    cli_link = prefix / "bin" / "ruff"
    external_cli.parent.mkdir(parents=True)
    cli_link.parent.mkdir(parents=True)
    external_cli.write_text("stub", encoding="utf-8")
    cli_link.symlink_to(external_cli)

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("prefix-escaping CLI symlink reached subprocess.run")

    monkeypatch.setattr(toolchain_preflight.subprocess, "run", unexpected_run)

    assert (
        toolchain_preflight._parse_command_version(
            [str(cli_link), "--version"],
            tool_name="ruff",
            discovered_path=str(cli_link),
            prefix=str(prefix),
        )
        is None
    )


def test_capture_runtime_records_but_does_not_execute_external_path_cli(
    tmp_path: Any, monkeypatch: Any
) -> None:
    external_cli = tmp_path / "outside" / "ruff"
    external_cli.parent.mkdir()
    external_cli.write_text("stub", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        return str(external_cli) if name == "ruff" else None

    def fake_run(command: list[str], **options: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="ruff 7.1.2", stderr="")

    monkeypatch.setattr(toolchain_preflight.shutil, "which", fake_which)
    monkeypatch.setattr(toolchain_preflight.subprocess, "run", fake_run)

    runtime = toolchain_preflight.capture_runtime()

    ruff = runtime["tools"]["ruff"]
    assert ruff["path_cli_executable"] == str(external_cli)
    assert ruff["path_cli_version"] is None
    assert all(command[0] != str(external_cli) for command in calls)
    assert calls

    project, ratchet, _ = _metadata()
    report = evaluate_toolchain(project, ratchet, runtime)
    assert report["status"] == "ENV_BLOCKED"
    assert any(
        issue["code"] == "TOOL_CLI_OUTSIDE_BASE" and issue["tool"] == "ruff"
        for issue in report["issues"]
    )
    assert any(
        issue["code"] == "TOOL_UNAVAILABLE"
        and issue["tool"] == "ruff"
        and issue["source"] == "path_cli_version"
        for issue in report["issues"]
    )


def test_unresolvable_path_cli_is_rejected_before_run(tmp_path: Any, monkeypatch: Any) -> None:
    prefix = tmp_path / "base"
    prefix.mkdir()
    missing_cli = prefix / "bin" / "ruff"

    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unresolvable CLI path reached subprocess.run")

    monkeypatch.setattr(toolchain_preflight.subprocess, "run", unexpected_run)

    assert (
        toolchain_preflight._parse_command_version(
            [str(missing_cli), "--version"],
            tool_name="ruff",
            discovered_path=str(missing_cli),
            prefix=str(prefix),
        )
        is None
    )
