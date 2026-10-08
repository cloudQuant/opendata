"""Deterministic provider-inventory rendering from fixed and injected metadata."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.quality import provider_model_inventory
from scripts.quality.provider_model_inventory import (
    UPSTREAM_COMMIT,
    UpstreamUnavailableError,
    main,
    render_provider_inventory,
)


def _record() -> str:
    """Return a compact synthetic manifest with existing planning fields."""
    return f"""# OpenBB provider inventory
# 规模：2 个 provider 目录 / 4 个 fetcher 模型
provider_count: 2
local_provider_count: 1
version: 1
upstream_commit: {UPSTREAM_COMMIT}
providers:
  - provider: alpha
    batch: P0
    fetchers: 1
    models: [OldModel]
    sdk_dependencies: [optional-sdk]
    credentials: [old_key]
    scenario: synthetic scenario
    estimate_person_days: 1.0
    status: 待实现
  - provider: eia
    batch: P1
    fetchers: 0
    models: []
    sdk_dependencies: []
    credentials: []
    scenario: 待消费场景确认
    estimate_person_days: 1.0
    status: 待实现
local_providers:
  - provider: local
    scenario: local scenario
    status: 待实现
    rights_rows: [同花顺扶摇 API]
"""


def _inputs() -> tuple[
    dict[str, list[str]],
    dict[str, list[str]],
    list[dict[str, str]],
    dict[str, list[tuple[str, bool]]],
]:
    models = {"alpha": ["AlphaOne", "AlphaTwo"], "eia": ["EnergyOne", "EnergyTwo"]}
    credentials = {"alpha": ["api_key"], "eia": ["api_key"]}
    ledger = [
        {
            "provider": provider,
            "upstream_model": model,
            "implementation_task_status": "NOT_RUN",
            "live_verification_status": "NOT_RUN",
            "requester": "cloudQuant",
            "scenario": "",
            "scenario_status": "NOT_ASSESSED",
        }
        for provider, provider_models in models.items()
        for model in provider_models
    ]
    runtime = {"alpha": [("equity_daily", True)], "local": [("domestic_daily", False)]}
    return models, credentials, ledger, runtime


def test_generator_is_byte_deterministic_and_keeps_status_planes_separate() -> None:
    models, credentials, ledger, runtime = _inputs()
    original = _record()

    generated_first = render_provider_inventory(original, models, credentials, ledger, runtime)
    generated_second = render_provider_inventory(original, models, credentials, ledger, runtime)
    document = yaml.safe_load(generated_first)
    alpha = document["providers"][0]
    eia = document["providers"][1]

    assert generated_first.encode() == generated_second.encode()
    assert (
        hashlib.sha256(generated_first.encode()).hexdigest()
        == hashlib.sha256(generated_second.encode()).hexdigest()
    )
    assert document["upstream_model_count"] == 4
    assert document["unique_upstream_model_count"] == 4
    assert document["registered_source_count"] == 2
    assert document["registered_capability_count"] == 2
    assert document["auto_routable_capability_count"] == 1
    assert alpha["models"] == models["alpha"]
    assert alpha["credentials"] == ["api_key"]
    assert alpha["fetchers"] == alpha["model_count"] == 2
    assert alpha["status"] == "已对照转正"
    assert alpha["implementation_task_status"] == "NOT_RUN"
    assert alpha["live_verification_status"] == "NOT_RUN"
    assert alpha["model_scenario_status"] == "NOT_ASSESSED"
    assert alpha["requester_confirmation_status"] == "NOT_ASSESSED"
    assert eia["status"] == "待实现"
    assert eia["implementation_task_status"] == "NOT_RUN"
    assert alpha["sdk_dependencies"] == ["optional-sdk"]
    assert alpha["scenario"] == "synthetic scenario"
    assert "requester: cloudQuant" not in generated_first
    assert document["local_providers"][0]["registered_capability_count"] == 1
    assert document["local_providers"][0]["status"] == "已实现未对照"


def test_unavailable_upstream_does_not_overwrite_manifest(tmp_path: Any, capsys: Any) -> None:
    destination = tmp_path / "provider-inventory.yaml"
    original = "user-owned manifest bytes\n"
    destination.write_text(original, encoding="utf-8")

    exit_code = main(
        [
            "--upstream-path",
            str(tmp_path),
            "--task-ledger",
            str(tmp_path / "missing.csv"),
            "--manifest",
            str(destination),
        ]
    )

    assert exit_code != 0
    assert destination.read_text(encoding="utf-8") == original
    assert yaml.safe_load(capsys.readouterr().out)["status"] == "NOT_AVAILABLE"


@pytest.mark.parametrize(
    "arguments",
    [
        ("rev-parse", "--show-prefix"),
        ("cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}"),
        ("ls-tree", "-r", "--name-only", UPSTREAM_COMMIT),
        ("show", f"{UPSTREAM_COMMIT}:providers/alpha/openbb_alpha/__init__.py"),
        (
            "show",
            f"{UPSTREAM_COMMIT}:openbb_platform/providers/alpha/openbb_alpha/__init__.py",
        ),
    ],
)
def test_pinned_git_commands_use_only_read_argv_without_shell(
    arguments: tuple[str, ...], tmp_path: Any, monkeypatch: Any
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **options: Any) -> SimpleNamespace:
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=b"pinned output")

    monkeypatch.setattr(provider_model_inventory.shutil, "which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(provider_model_inventory.subprocess, "run", fake_run)

    result = provider_model_inventory._run_git(tmp_path / "upstream repo", *arguments)

    assert result == b"pinned output"
    assert len(calls) == 1
    command, options = calls[0]
    assert command == ["/usr/bin/git", "-C", str(tmp_path / "upstream repo"), *arguments]
    assert options["shell"] is False
    assert options["timeout"] == 30
    assert options["capture_output"] is True
    assert options["check"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        ("commit", "-m", "mutation"),
        ("rev-parse", "--show-prefix", "--git-dir"),
        ("ls-tree", "-r", "--name-only", "HEAD"),
        ("show", f"{UPSTREAM_COMMIT}:README.md"),
        ("show", f"{UPSTREAM_COMMIT}:providers/../secret"),
        (
            "show",
            f"{UPSTREAM_COMMIT}:another_platform/providers/alpha/openbb_alpha/__init__.py",
        ),
        (
            "show",
            f"{UPSTREAM_COMMIT}:providers/alpha/openbb_alpha/__init__.py",
            "--output=/tmp/untrusted",
        ),
    ],
)
def test_unapproved_git_command_is_rejected_before_subprocess(
    arguments: tuple[str, ...], tmp_path: Any, monkeypatch: Any
) -> None:
    def unexpected_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unapproved Git argv reached subprocess.run")

    monkeypatch.setattr(provider_model_inventory.subprocess, "run", unexpected_run)

    with pytest.raises(UpstreamUnavailableError, match="Unsupported Git command"):
        provider_model_inventory._run_git(tmp_path, *arguments)


def test_loader_reads_exact_monorepo_prefix_through_pinned_git_show(
    tmp_path: Any, monkeypatch: Any
) -> None:
    source_file = "providers/alpha/openbb_alpha/__init__.py"
    source = b"""provider = Provider(
    name="alpha",
    fetcher_dict={"AlphaDaily": AlphaDailyFetcher},
    credentials=["api_key"],
)
"""
    expected_calls = [
        ("rev-parse", "--show-prefix"),
        ("cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}"),
        ("ls-tree", "-r", "--name-only", UPSTREAM_COMMIT),
        (
            "show",
            f"{UPSTREAM_COMMIT}:openbb_platform/{source_file}",
        ),
    ]
    observed_calls: list[tuple[str, ...]] = []
    observed_options: list[dict[str, Any]] = []

    def fake_run(command: list[str], **options: Any) -> SimpleNamespace:
        assert command[:3] == [
            "/usr/bin/git",
            "-C",
            str(tmp_path / "monorepo" / "openbb_platform"),
        ]
        arguments = tuple(command[3:])
        observed_calls.append(arguments)
        observed_options.append(options)
        if arguments == expected_calls[0]:
            output = b"openbb_platform/\n"
        elif arguments == expected_calls[1]:
            output = b""
        elif arguments == expected_calls[2]:
            output = f"{source_file}\n".encode()
        elif arguments == expected_calls[3]:
            output = source
        else:
            pytest.fail(f"unexpected fixed Git call: {arguments!r}")
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(provider_model_inventory.shutil, "which", lambda _: "/usr/bin/git")
    monkeypatch.setattr(provider_model_inventory.subprocess, "run", fake_run)

    files, records, credentials, issues = provider_model_inventory.load_pinned_provider_metadata(
        tmp_path / "monorepo" / "openbb_platform"
    )

    assert observed_calls == expected_calls
    assert all(options["shell"] is False for options in observed_options)
    assert all(options["timeout"] == 30 for options in observed_options)
    assert files == [source_file]
    assert [(record.provider, record.model) for record in records] == [("alpha", "AlphaDaily")]
    assert records[0].upstream_commit == UPSTREAM_COMMIT
    assert records[0].upstream_shape_file == source_file
    assert records[0].upstream_shape_sha256 == hashlib.sha256(source).hexdigest()
    assert credentials == {"alpha": ["api_key"]}
    assert issues == []
