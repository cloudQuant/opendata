from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from scripts.ops import migrate_legacy_stock_daily as transfer

if TYPE_CHECKING:
    from pathlib import Path


def test_main_dry_run_writes_redacted_private_report_without_connecting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "dry-run.json"
    source_secret = "mysql+pymysql://reader:source-secret@localhost:3306/akshare_data"
    target_secret = "mysql+pymysql://writer:target-secret@localhost:33565/opendata_c65_legacy_test"
    monkeypatch.setenv(transfer.SOURCE_URL_ENV, source_secret)
    monkeypatch.setenv(transfer.TARGET_URL_ENV, target_secret)
    monkeypatch.setattr(transfer, "_provenance_hashes", lambda: {})
    monkeypatch.setattr(
        transfer,
        "_new_engine",
        lambda _url: pytest.fail("dry-run unexpectedly created a database engine"),
    )

    result = transfer.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert result == 0
    summary = json.loads(captured.out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert summary["status"] == report["status"] == "DRY_RUN"
    assert report["mode"] == "dry-run"
    assert report["connection_attempted"] is False
    assert report["source_endpoint"] is None
    assert report["target_endpoint"] is None
    assert report["command"][-2:] == ["--output", str(output)]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    serialized = captured.out + output.read_text(encoding="utf-8")
    assert source_secret not in serialized
    assert target_secret not in serialized
    assert "source-secret" not in serialized
    assert "target-secret" not in serialized


@pytest.mark.parametrize(
    ("source_url", "classification"),
    [
        (
            "mysql+pymysql://reader:source-secret@example.invalid:3306/akshare_data",
            "source_host_not_allowed",
        ),
        (
            "mysql+pymysql://reader:source-secret@localhost:3306/akshare_data"
            "?init_command=SELECT%201",
            "source_url_options_not_allowed",
        ),
    ],
)
def test_main_apply_rejects_unreviewed_source_before_engine_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    source_url: str,
    classification: str,
) -> None:
    output = tmp_path / "rejected-apply.json"
    target_url = "mysql+pymysql://writer:target-secret@localhost:33565/opendata_c65_legacy_test"
    monkeypatch.setenv(transfer.SOURCE_URL_ENV, source_url)
    monkeypatch.setenv(transfer.TARGET_URL_ENV, target_url)
    monkeypatch.setattr(transfer, "_provenance_hashes", lambda: {})
    monkeypatch.setattr(
        transfer,
        "_new_engine",
        lambda _url: pytest.fail("rejected endpoint reached engine creation"),
    )

    result = transfer.main(["--apply", "--output", str(output)])

    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    serialized = captured.out + output.read_text(encoding="utf-8")
    assert result == 1
    assert report["status"] == "FAIL"
    assert report["error_classification"] == classification
    assert report["connection_attempted"] is False
    assert report["source_endpoint"] is None
    assert report["target_endpoint"] is None
    assert source_url not in serialized
    assert target_url not in serialized
    assert "source-secret" not in serialized
    assert "target-secret" not in serialized
    assert "example.invalid" not in serialized


@pytest.mark.parametrize(
    "has_output_argument",
    [False, True],
)
def test_main_rejects_invalid_cli_before_running_transfer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    has_output_argument: bool,
) -> None:
    output = tmp_path / "private-path.json"
    argv = (
        ["--output", str(output), "--unrecognized", "private-argument"]
        if has_output_argument
        else []
    )
    monkeypatch.setattr(
        transfer,
        "run_transfer",
        lambda **_kwargs: pytest.fail("invalid CLI reached transfer execution"),
    )

    result = transfer.main(argv)

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "invalid_command_line\n"
    assert "private-path.json" not in captured.err
    assert "private-argument" not in captured.err
    assert not output.exists()


def test_main_refuses_to_overwrite_an_existing_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "existing.json"
    original = '{"owner":"keep"}\n'
    output.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        transfer,
        "run_transfer",
        lambda *, apply: {"status": "DRY_RUN", "error_classification": None},
    )

    result = transfer.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "report_output_refused: path already exists\n"
    assert str(output) not in captured.err
    assert output.read_text(encoding="utf-8") == original


def test_main_reports_write_failure_without_echoing_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "missing-parent" / "report.json"
    monkeypatch.setattr(
        transfer,
        "run_transfer",
        lambda *, apply: {"status": "DRY_RUN", "error_classification": None},
    )

    result = transfer.main(["--output", str(output)])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert captured.err == "report_output_failed\n"
    assert str(output) not in captured.err
    assert not output.exists()
