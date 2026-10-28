"""Fuyao settings keep a configured credential out of common representations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_FUYAO_TEST_KEY = "fuyao-settings-redaction-test-value"
_FUYAO_ENV_FILE_KEY = "fuyao-env-file-redaction-test-value"
_FUYAO_PROCESS_KEY = "fuyao-process-env-redaction-test-value"


def _settings_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Import settings while the working directory has no real project .env."""
    monkeypatch.chdir(tmp_path)
    from opendata.core.config import Settings

    return Settings


def _assert_hidden(configured, expected_key: str) -> None:
    assert configured.fuyao_api_key == expected_key
    assert isinstance(configured.fuyao_api_key, str)
    assert type(configured).model_fields["fuyao_api_key"].repr is False
    assert type(configured).model_fields["fuyao_api_key"].exclude is True
    assert expected_key not in repr(configured)
    assert expected_key not in str(configured)
    assert "fuyao_api_key" not in configured.model_dump(mode="python")
    assert "fuyao_api_key" not in configured.model_dump(mode="json")
    assert "fuyao_api_key" not in json.loads(configured.model_dump_json())
    assert expected_key not in configured.model_dump_json()


def test_fuyao_key_remains_string_compatible_and_is_not_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    settings_type = _settings_type(tmp_path, monkeypatch)
    configured = settings_type(_env_file=None, fuyao_api_key=_FUYAO_TEST_KEY)

    _assert_hidden(configured, _FUYAO_TEST_KEY)


def test_fuyao_key_loads_from_synthetic_env_file_with_process_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"FUYAO_API_KEY={_FUYAO_ENV_FILE_KEY}\n", encoding="utf-8")
    monkeypatch.delenv("FUYAO_API_KEY", raising=False)
    settings_type = _settings_type(tmp_path, monkeypatch)

    from_file = settings_type(_env_file=env_file)

    _assert_hidden(from_file, _FUYAO_ENV_FILE_KEY)

    monkeypatch.setenv("FUYAO_API_KEY", _FUYAO_PROCESS_KEY)
    from_process_environment = settings_type(_env_file=env_file)

    _assert_hidden(from_process_environment, _FUYAO_PROCESS_KEY)
