"""Isolated error-path tests for the migration worklist command-line entrypoint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.ops import build_migration_worklist as cli

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _input_paths(tmp_path: Path) -> dict[str, Path]:
    """Create synthetic JSON inputs and a not-yet-created output path."""
    paths = {
        "plan": tmp_path / "plan.json",
        "runtime": tmp_path / "runtime.json",
        "catalog": tmp_path / "catalog.json",
        "output": tmp_path / "worklist.json",
    }
    for name in ("plan", "runtime", "catalog"):
        paths[name].write_text("{}", encoding="utf-8")
    return paths


def _argv(paths: dict[str, Path]) -> list[str]:
    """Build explicit CLI arguments from temporary fixture paths."""
    return [
        "--plan",
        str(paths["plan"]),
        "--runtime-inventory",
        str(paths["runtime"]),
        "--static-catalog",
        str(paths["catalog"]),
        "--output",
        str(paths["output"]),
    ]


def test_cli_reports_invalid_json_input_without_creating_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Translate strict JSON parse failures into a bounded CLI error."""
    paths = _input_paths(tmp_path)
    paths["plan"].write_text("{invalid", encoding="utf-8")

    result = cli.main(_argv(paths))
    captured = capsys.readouterr()

    assert result == 2
    assert json.loads(captured.err) == {"error": "migration_plan_not_strict_utf8_json"}
    assert captured.out == ""
    assert not paths["output"].exists()


def test_cli_reports_invalid_plan_and_does_not_write_worklist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a synthetic plan lacking its required integrity digest."""
    paths = _input_paths(tmp_path)

    result = cli.main(_argv(paths))
    captured = capsys.readouterr()

    assert result == 2
    assert json.loads(captured.err) == {"error": "migration_plan_sha256_invalid"}
    assert captured.out == ""
    assert not paths["output"].exists()


def test_cli_rejects_a_worklist_without_a_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when the worklist builder returns an invalid summary shape."""
    paths = _input_paths(tmp_path)
    monkeypatch.setattr(cli, "build_migration_worklist", lambda *_args: {"summary": None})

    result = cli.main(_argv(paths))
    captured = capsys.readouterr()

    assert result == 2
    assert json.loads(captured.err) == {"error": "worklist_summary_invalid"}
    assert captured.out == ""
    assert not paths["output"].exists()


def test_cli_translates_output_io_errors_without_exposing_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep operating-system output details out of the command response."""
    paths = _input_paths(tmp_path)
    monkeypatch.setattr(
        cli,
        "build_migration_worklist",
        lambda *_args: {"summary": {}, "queue_status": "PENDING"},
    )

    def fail_write(*_args: object, **_kwargs: object) -> Path:
        raise OSError("synthetic private path detail")

    monkeypatch.setattr(cli, "write_migration_worklist_new_file", fail_write)

    result = cli.main(_argv(paths))
    captured = capsys.readouterr()

    assert result == 2
    assert json.loads(captured.err) == {"error": "input_or_output_io_error"}
    assert "synthetic private path detail" not in captured.err
    assert captured.out == ""
    assert not paths["output"].exists()
