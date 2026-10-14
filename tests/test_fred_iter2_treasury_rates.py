"""Independent contract and adapter checks for three fixed Treasury models."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.protocol import FetchContext
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.treasury_rates import (
    SelectedTreasuryBillFetcher,
    SelectedTreasuryConstantMaturityFetcher,
    TreasuryConstantMaturityFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)


@pytest.fixture(autouse=True)
def _fred_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "test-key")


@pytest.fixture
def treasury_constant_maturity_fixture() -> list[dict[str, str]]:
    """Hand-written DGS3MO observations, including FRED's missing sentinel."""
    return [
        {
            "date": "2026-10-01",
            "value": "3.51",
            "realtime_start": "2026-10-02",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2026-10-02",
            "value": ".",
            "realtime_start": "2026-10-03",
            "realtime_end": "9999-12-31",
        },
    ]


@pytest.fixture
def selected_treasury_bill_fixture() -> list[dict[str, str]]:
    """Hand-written DTB6 observations with bill-specific sample values."""
    return [
        {
            "date": "2026-09-28",
            "value": "3.62",
            "realtime_start": "2026-09-29",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2026-09-29",
            "value": "3.64",
            "realtime_start": "2026-09-30",
            "realtime_end": "9999-12-31",
        },
    ]


@pytest.fixture
def selected_treasury_constant_maturity_fixture() -> list[dict[str, str]]:
    """Hand-written DGS5 observations with constant-maturity sample values."""
    return [
        {
            "date": "2026-08-31",
            "value": "4.08",
            "realtime_start": "2026-09-01",
            "realtime_end": "9999-12-31",
        },
        {
            "date": "2026-09-01",
            "value": "4.11",
            "realtime_start": "2026-09-02",
            "realtime_end": "9999-12-31",
        },
    ]


def _page_response(observations: list[dict[str, str]], *, offset: int, page_size: int) -> str:
    page = observations[offset : offset + page_size]
    return json.dumps(
        {
            "count": len(observations),
            "offset": offset,
            "limit": page_size,
            "observations": page,
        }
    )


def _context(fetcher: Any) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model=fetcher.canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only synthetic FRED query grant",
        task_attempts=10,
        source_attempts=3,
        allowed_hosts={"api.stlouisfed.org"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        request_budget=RequestBudget(task_attempts=10, source_attempts=3, grants=(grant,))
    )


def _install_paged_response(
    monkeypatch: pytest.MonkeyPatch,
    observations: list[dict[str, str]],
    requests: list[tuple[str, dict[str, str]]],
) -> None:
    def http_get(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, dict(params)))
        return 200, _page_response(
            observations,
            offset=int(params["offset"]),
            page_size=int(params["limit"]),
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", http_get)


def test_treasury_constant_maturity_maps_pit_pages_and_output_shape(
    monkeypatch: pytest.MonkeyPatch,
    treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    fetcher = TreasuryConstantMaturityFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_paged_response(monkeypatch, treasury_constant_maturity_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher),
        maturity="3m",
        start_date="2026-10-01",
        end_date="2026-10-02",
        as_of="2026-10-04",
        page_size=1,
        max_pages=2,
    )

    assert [row.date.isoformat() for row in rows] == ["2026-10-01", "2026-10-02"]
    assert [row.rate for row in rows] == [3.51, None]
    assert [row._series_id for row in rows] == ["DGS3MO", "DGS3MO"]
    assert rows[0]._realtime_start.isoformat() == "2026-10-02"
    assert list(rows[0].model_dump()) == ["date", "rate"]
    assert list(rows[0].to_frame([rows[0]]).columns) == ["date", "rate"]
    assert [params["offset"] for _, params in requests] == ["0", "1"]
    assert all(params["series_id"] == "DGS3MO" for _, params in requests)
    assert all(params["units"] == "lin" and params["output_type"] == "1" for _, params in requests)
    assert all(params["observation_start"] == "2026-10-01" for _, params in requests)
    assert all(params["observation_end"] == "2026-10-02" for _, params in requests)
    assert all(
        params["realtime_start"] == params["realtime_end"] == "2026-10-04" for _, params in requests
    )


def test_treasury_constant_maturity_two_year_uses_dgs2(
    monkeypatch: pytest.MonkeyPatch,
    treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    requested: list[str] = []

    def one_observation(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del url, timeout
        requested.append(params["series_id"])
        return 200, _page_response(
            treasury_constant_maturity_fixture[:1], offset=0, page_size=100_000
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", one_observation)
    fetcher = TreasuryConstantMaturityFetcher()
    rows = fetcher.fetch(ctx=_context(fetcher), maturity="2y")
    assert requested == ["DGS2"]
    assert rows[0].rate == 3.51


def test_treasury_constant_maturity_rejects_bad_parameters_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid query must be rejected before I/O"),
    )
    fetcher = TreasuryConstantMaturityFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), maturity="1y")
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), start_date="2026-02-30")
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), start_date="2026-10-02", end_date="2026-10-01")
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), as_of="2026-10-01", realtime_start="2026-10-01")


def test_treasury_constant_maturity_rejects_bad_source_values_and_revision_shape(
    treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    fetcher = TreasuryConstantMaturityFetcher()
    params = fetcher.transform_query()
    for bad_value in ("", "garbage", "NaN", "inf"):
        row = {**treasury_constant_maturity_fixture[0], "value": bad_value}
        with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
            fetcher.transform_data([row], params)
    bad_date = {**treasury_constant_maturity_fixture[0], "date": "2026-10-01T00:00:00"}
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([bad_date], params)
    duplicate_revision = [
        treasury_constant_maturity_fixture[0],
        {
            **treasury_constant_maturity_fixture[0],
            "value": "3.52",
            "realtime_start": "2026-10-03",
        },
    ]
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(duplicate_revision, params)
    assert fetcher.transform_data([], params) == ()


def test_selected_treasury_bill_maps_dt_b_and_validates_vintage(
    monkeypatch: pytest.MonkeyPatch,
    selected_treasury_bill_fixture: list[dict[str, str]],
) -> None:
    fetcher = SelectedTreasuryBillFetcher()
    requests: list[tuple[str, dict[str, str]]] = []
    _install_paged_response(monkeypatch, selected_treasury_bill_fixture, requests)

    rows = fetcher.fetch(
        ctx=_context(fetcher), maturity="6m", vintage_dates=["2026-10-01"], page_size=1
    )

    assert [row.rate for row in rows] == [3.62, 3.64]
    assert [row._series_id for row in rows] == ["DTB6", "DTB6"]
    assert rows[0]._realtime_start.isoformat() == "2026-09-29"
    assert all(params["series_id"] == "DTB6" for _, params in requests)
    assert all(params["vintage_dates"] == "2026-10-01" for _, params in requests)
    assert [params["offset"] for _, params in requests] == ["0", "1"]


def test_selected_treasury_bill_rejects_bad_parameters_and_values_before_io(
    monkeypatch: pytest.MonkeyPatch,
    selected_treasury_bill_fixture: list[dict[str, str]],
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid query must be rejected before I/O"),
    )
    fetcher = SelectedTreasuryBillFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), maturity="2y")
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), vintage_dates=["2026-09-01", "2026-10-01"])
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), as_of="2026-10-01", realtime_end="2026-10-02")

    params = fetcher.transform_query()
    bad_revision = {**selected_treasury_bill_fixture[0], "realtime_end": "2026-09-01"}
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([bad_revision], params)
    out_of_window = fetcher.transform_query(start_date="2026-09-29")
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data([selected_treasury_bill_fixture[0]], out_of_window)
    assert fetcher.transform_data([], params) == ()


def test_selected_treasury_bill_rate_limit_does_not_leak_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "private-test-token")
    requests: list[tuple[str, dict[str, str]]] = []

    def rate_limited(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del timeout
        requests.append((url, params))
        return 429, "Too Many Requests"

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", rate_limited)
    with pytest.raises(FredProviderError) as exc_info:
        fetcher = SelectedTreasuryBillFetcher()
        fetcher.fetch(ctx=_context(fetcher))
    assert exc_info.value.code == "FRED_HTTP_ERROR"
    assert exc_info.value.status == 429
    assert "private-test-token" not in str(exc_info.value)
    assert requests[0][1]["series_id"] == "DTB3"


def test_selected_treasury_constant_maturity_maps_all_fixed_series(
    monkeypatch: pytest.MonkeyPatch,
    selected_treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    fetcher = SelectedTreasuryConstantMaturityFetcher()
    expected = {"10y": "DGS10", "5y": "DGS5", "1y": "DGS1", "6m": "DGS6MO", "3m": "DGS3MO"}
    requested_ids: list[str] = []

    def one_observation(
        url: str, params: dict[str, str], timeout: float | None, **_kwargs
    ) -> tuple[int, str]:
        del url, timeout
        requested_ids.append(params["series_id"])
        return 200, json.dumps(
            {
                "count": 1,
                "offset": int(params["offset"]),
                "limit": int(params["limit"]),
                "observations": [selected_treasury_constant_maturity_fixture[0]],
            }
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", one_observation)
    for maturity in expected:
        rows = fetcher.fetch(ctx=_context(fetcher), maturity=maturity)
        assert rows[0].rate == 4.08
        assert rows[0]._maturity == maturity
    assert requested_ids == list(expected.values())


def test_selected_treasury_constant_maturity_rejects_invalid_query_and_source_rows(
    monkeypatch: pytest.MonkeyPatch,
    selected_treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args, **_kwargs: pytest.fail("invalid query must be rejected before I/O"),
    )
    fetcher = SelectedTreasuryConstantMaturityFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), maturity="20y")
    with pytest.raises(ValidationError):
        fetcher.fetch(ctx=_context(fetcher), as_of="2026-10-01", vintage_dates=["2026-10-01"])

    params = fetcher.transform_query()
    for malformed in (
        {**selected_treasury_constant_maturity_fixture[0], "value": ""},
        {**selected_treasury_constant_maturity_fixture[0], "value": "inf"},
        {**selected_treasury_constant_maturity_fixture[0], "date": "not-a-date"},
        {**selected_treasury_constant_maturity_fixture[0], "realtime_start": "2026-09-01T12:00:00"},
        {
            **selected_treasury_constant_maturity_fixture[0],
            "realtime_start": "2026-10-01",
            "realtime_end": "2026-09-01",
        },
    ):
        with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
            fetcher.transform_data([malformed], params)
    assert fetcher.transform_data([], params) == ()


def test_selected_treasury_constant_maturity_429_and_vintage_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    selected_treasury_constant_maturity_fixture: list[dict[str, str]],
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda url, params, timeout, **_kwargs: (429, "Too Many Requests"),
    )
    with pytest.raises(FredProviderError) as exc_info:
        fetcher = SelectedTreasuryConstantMaturityFetcher()
        fetcher.fetch(ctx=_context(fetcher), maturity="5y")
    assert exc_info.value.status == 429
    assert exc_info.value.code == "FRED_HTTP_ERROR"

    fetcher = SelectedTreasuryConstantMaturityFetcher()
    params = fetcher.transform_query(vintage_dates=["2026-08-01"])
    with pytest.raises(FredProviderError, match="FRED_BAD_OBSERVATION"):
        fetcher.transform_data(selected_treasury_constant_maturity_fixture, params)
