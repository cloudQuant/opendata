"""Exercise the committed environment template with the real Gitleaks rules."""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path

import pytest

from scripts.quality.acceptance_item_probe import (
    config_has_no_global_path_exemption,
    scan_env_template,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _gitleaks() -> str:
    tool = shutil.which("gitleaks")
    if tool is None:
        pytest.skip("gitleaks is required for the environment-template security checks")
    return tool


def test_committed_env_template_passes_gitleaks() -> None:
    tool = _gitleaks()
    template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    assert config_has_no_global_path_exemption(config)
    code, summary = scan_env_template(template, config, tool=tool)

    assert code == 0
    assert "findings=0" in summary


def test_env_template_scan_rejects_a_slack_shaped_token() -> None:
    tool = _gitleaks()
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    fake_token = "xoxb-" + "123456789012-" + "123456789012-" + "abcdefghijklmnopqrstuvwx"

    code, summary = scan_env_template(
        f"SLACK_BOT_TOKEN={fake_token}\n",
        config,
        tool=tool,
    )

    assert code == 1
    assert "findings=1" in summary
    assert "slack-bot-token@" in summary
    assert ".env.example:1" in summary
    assert fake_token not in summary


def test_empty_pepper_does_not_hide_following_config_or_a_high_entropy_api_key() -> None:
    tool = _gitleaks()
    config = (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    template = "API_KEY_PEPPER=\nAPI_KEY_FAILURE_DELAY_SECONDS=0.05\n"

    code, summary = scan_env_template(template, config, tool=tool)

    assert code == 0
    assert "findings=0" in summary

    simulated_token = secrets.token_urlsafe(48)
    code, summary = scan_env_template(
        f"{template}SERVICE_API_KEY={simulated_token}\n",
        config,
        tool=tool,
    )

    assert code == 1
    assert "findings=1" in summary
    assert "generic-api-key@" in summary
    assert ".env.example:3" in summary
    assert simulated_token not in summary
