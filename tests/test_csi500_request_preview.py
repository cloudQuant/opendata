"""Boundary tests for the standalone, offline CSI 500 preview."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from opendata.pipeline import csi500_request_preview as preview
from scripts.ops import preview_csi500_request as preview_cli


def test_parse_business_date_accepts_only_canonical_iso_dates() -> None:
    """Input date parsing is strict and does not infer calendar membership."""
    assert preview.parse_business_date("2026-10-30") == date(2026, 10, 30)

    for invalid in ("2026-2-3", "2026-02-30", "2026-10-30T00:00:00", None):
        with pytest.raises(preview.Csi500PreviewInputError) as error:
            preview.parse_business_date(invalid)
        assert str(error.value) == "business_date_invalid"


def test_preview_matches_fixed_official_builder_query_digest() -> None:
    """The digest pins the endpoint builder's canonical one-day request input."""
    report = preview.preview_csi500_request(business_date=date(2026, 10, 30))

    assert report.status == "preview_only"
    assert report.authorization_status == "not_established"
    assert report.calendar_evidence_status == "not_verified"
    assert report.target_status == "not_required_not_accessed"
    assert report.gate_status == "not_accessed"
    assert report.execution_enabled is False
    assert report.dataset_id == "cn.csi500.index.daily"
    assert report.symbol == "000905.SH"
    assert report.business_date == date(2026, 10, 30)
    assert report.method == "GET"
    assert report.host == "fuyao.aicubes.cn"
    assert report.path == "/api/a-share-index/prices/historical"
    assert report.query_summary_sha256 == (
        "f96956fd2392b15d771468793660c475059d6fb7b830103cc2d910b4b6ce75fd"
    )


def test_preview_rejects_datetime_and_changed_default_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A preview only accepts a date and the fixed HTTPS Fuyao host."""
    with pytest.raises(preview.Csi500PreviewInputError) as date_error:
        preview.preview_csi500_request(business_date=datetime(2026, 10, 30))  # type: ignore[arg-type]
    assert str(date_error.value) == "business_date_invalid"

    monkeypatch.setattr(preview, "DEFAULT_FUYAO_API_BASE_URL", "http://fuyao.aicubes.cn")
    with pytest.raises(preview.Csi500PreviewInputError) as route_error:
        preview.preview_csi500_request(business_date=date(2026, 10, 30))
    assert str(route_error.value) == "fixed_route_invalid"


def test_preview_rejects_maximum_date_before_building_exclusive_window() -> None:
    """The next-day exclusive endpoint must fit in Python's date range."""
    with pytest.raises(preview.Csi500PreviewInputError) as error:
        preview.preview_csi500_request(business_date=date.max)
    assert str(error.value) == "business_date_invalid"


def test_standalone_cli_exposes_only_a_date_and_is_user_reachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The supported command needs no target, authorization, or calendar path."""
    monkeypatch.setenv("FUYAO_API_KEY", "preview-test-marker-do-not-use")
    parser = preview_cli.build_parser()
    assert [action.option_strings for action in parser._actions if action.option_strings] == [
        ["-h", "--help"],
        ["--business-date"],
    ]

    result = preview_cli.main(["--business-date", "2026-10-30"])

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert result == 0
    assert "preview-test-marker-do-not-use" not in output_text
    assert output["status"] == "preview_only"
    assert output["authorization_status"] == "not_established"
    assert output["calendar_evidence_status"] == "not_verified"
    assert output["target_status"] == "not_required_not_accessed"
    assert output["gate_status"] == "not_accessed"
    assert output["execution_enabled"] is False
    assert output["request"]["query_summary_sha256"] == (
        "f96956fd2392b15d771468793660c475059d6fb7b830103cc2d910b4b6ce75fd"
    )


def test_cli_invalid_date_returns_a_sanitized_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed dates stop before building a request and disclose no input."""
    result = preview_cli.main(["--business-date", "2026-02-30"])

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "business_date_invalid",
    }


def test_cli_maximum_date_returns_a_sanitized_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A valid ISO date without a representable next day is still rejected safely."""
    result = preview_cli.main(["--business-date", "9999-12-31"])

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "blocked",
        "code": "business_date_invalid",
    }
