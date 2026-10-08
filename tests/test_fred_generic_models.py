"""Offline contract and pagination tests for the generic FRED models."""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from opendata.data.models import SeriesCatalogItem, SeriesObservation
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._paged import FredQueryLimitError
from opendata.data.providers.fred.models.search import FredSearchFetcher
from opendata.data.providers.fred.models.series import FredSeriesFetcher
from tests.provider_budget_fixtures import offline_fixture_context

if TYPE_CHECKING:
    from collections.abc import Callable


def _catalog_item(**updates: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "GDP.Q",
        "title": "Gross Domestic Product",
        "frequency": "Quarterly",
        "units": "Billions of Dollars",
        "seasonal_adjustment": "Seasonally Adjusted Annual Rate",
        "observation_start": "1947-01-01",
        "observation_end": "2026-04-01",
        "last_updated": "2026-09-25 07:30:00-05",
        "notes": "Official FRED catalog note",
        "popularity": 95,
    }
    item.update(updates)
    return item


def _observation(
    value: str = "123.5",
    *,
    realtime_start: str = "2026-01-01",
    realtime_end: str = "9999-12-31",
    **updates: object,
) -> dict[str, object]:
    item: dict[str, object] = {
        "date": "2026-01-01",
        "value": value,
        "realtime_start": realtime_start,
        "realtime_end": realtime_end,
    }
    item.update(updates)
    return item


def _envelope(
    collection: str,
    items: list[dict[str, object]],
    *,
    count: int,
    offset: object = 0,
    limit: object = 1,
) -> str:
    return json.dumps({"count": count, "offset": offset, "limit": limit, collection: items})


def _fetch_with_dynamic_query(fetch: Callable[..., object], query: dict[str, object]) -> object:
    """Call the deliberately dynamic validation-case query dictionaries."""
    return fetch(**query)


def test_search_fetches_all_pages_and_normalizes_typed_catalog_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    responses = [
        _envelope("seriess", [_catalog_item()], count=2, offset=0),
        _envelope(
            "seriess",
            [_catalog_item(id="UNEMPLOYMENT")],
            count=2,
            offset=1,
        ),
    ]
    requests: list[tuple[str, dict[str, str], float | None]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append((url, params, timeout))
        return 200, responses.pop(0)

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    fetcher = FredSearchFetcher()
    result = cast(
        "tuple[SeriesCatalogItem, ...]",
        fetcher.fetch(
            search_text="gross domestic product",
            search_type="full_text",
            realtime_start=date(2020, 1, 1),
            realtime_end=date(2026, 1, 1),
            filter_variable="frequency",
            filter_value="Quarterly",
            tag_names=["gdp", "quarterly"],
            exclude_tag_names=["annual"],
            page_size=1,
            max_records=2,
            max_pages=2,
            sort_order="asc",
            ctx=offline_fixture_context("fred", "FredSearch", timeout=7),
        ),
    )

    assert len(result) == 2
    assert all(isinstance(row, SeriesCatalogItem) for row in result)
    assert result[0].series_id == "GDP.Q"
    assert result[0].last_updated.utcoffset() is not None
    assert result[0].popularity == 95
    assert [call[1]["offset"] for call in requests] == ["0", "1"]
    assert all(call[0].endswith("/fred/series/search") for call in requests)
    assert all(call[1]["search_text"] == "gross domestic product" for call in requests)
    assert all(call[1]["sort_order"] == "asc" for call in requests)
    assert all(call[1]["realtime_start"] == "2020-01-01" for call in requests)
    assert all(call[1]["realtime_end"] == "2026-01-01" for call in requests)
    assert all(call[1]["filter_variable"] == "frequency" for call in requests)
    assert all(call[1]["filter_value"] == "Quarterly" for call in requests)
    assert all(call[1]["tag_names"] == "gdp;quarterly" for call in requests)
    assert all(call[1]["exclude_tag_names"] == "annual" for call in requests)
    assert all(call[1]["api_key"] == "offline-test-key" for call in requests)
    assert all(call[2] == 7 for call in requests)
    assert fetcher.capability.domain == "fred_search"
    assert fetcher.capability.asset_class == "macro"
    assert fetcher.capability.source == "fred"
    assert fetcher.capability.verified is False
    assert fetcher.capability.period == "snapshot"
    assert fetcher.async_mode == "bounded_thread"


def test_series_pages_preserve_revisions_and_keep_transform_separate_from_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    responses = [
        _envelope(
            "observations",
            [_observation("100.0", realtime_start="2026-01-02", realtime_end="2026-02-10")],
            count=2,
            offset=0,
        ),
        _envelope(
            "observations",
            [_observation("101.0", realtime_start="2026-02-11")],
            count=2,
            offset=1,
        ),
    ]
    requests: list[tuple[str, dict[str, str], float | None]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append((url, params, timeout))
        return 200, responses.pop(0)

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    fetcher = FredSeriesFetcher()
    result = cast(
        "tuple[SeriesObservation, ...]",
        fetcher.fetch(
            series_id=" GDP.Q ",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            transform_units="pc1",
            output_type=4,
            page_size=1,
            max_records=2,
            max_pages=2,
            ctx=offline_fixture_context("fred", "FredSeries"),
        ),
    )

    assert len(result) == 2
    assert all(isinstance(row, SeriesObservation) for row in result)
    assert [row.value for row in result] == [100.0, 101.0]
    assert [row.realtime_start for row in result] == [date(2026, 1, 2), date(2026, 2, 11)]
    assert all(row.date == date(2026, 1, 1) for row in result)
    assert all(row.transform_units == "pc1" for row in result)
    assert all(row.output_type == 4 for row in result)
    assert all(call[0].endswith("/fred/series/observations") for call in requests)
    assert all(call[1]["series_id"] == "GDP.Q" for call in requests)
    assert all(call[1]["units"] == "pc1" for call in requests)
    assert all(call[1]["output_type"] == "4" for call in requests)
    assert all(call[1]["observation_start"] == "2026-01-01" for call in requests)
    assert all(call[1]["observation_end"] == "2026-01-31" for call in requests)
    assert fetcher.capability.domain == "fred_series"
    assert fetcher.capability.period == "variable"
    assert fetcher.capability.verified is False


def test_series_vintage_dates_map_to_comma_list_without_realtime_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    requests: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append(params)
        return 200, _envelope(
            "observations",
            [_observation("123.5", realtime_start="2026-01-15", realtime_end="2026-01-31")],
            count=1,
            limit=100_000,
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    result = cast(
        "tuple[SeriesObservation, ...]",
        FredSeriesFetcher().fetch(
            series_id="GDP",
            vintage_dates=[date(2026, 1, 31), date(2026, 1, 15)],
            aggregation_method="sum",
            ctx=offline_fixture_context("fred", "FredSeries"),
        ),
    )

    assert len(result) == 1
    assert requests[0]["vintage_dates"] == "2026-01-31,2026-01-15"
    assert "realtime_start" not in requests[0]
    assert "realtime_end" not in requests[0]
    assert requests[0]["output_type"] == "1"
    assert requests[0]["sort_order"] == "asc"
    assert requests[0]["aggregation_method"] == "sum"
    assert "frequency" not in requests[0]


def test_series_frequency_aggregation_and_descending_response_order_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    requests: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append(params)
        return 200, _envelope(
            "observations",
            [
                _observation("3", **{"date": "2026-03-01"}),
                _observation("2", **{"date": "2026-02-01"}),
            ],
            count=2,
            limit=2,
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    result = cast(
        "tuple[SeriesObservation, ...]",
        FredSeriesFetcher().fetch(
            series_id="GDP",
            frequency="m",
            aggregation_method="eop",
            sort_order="desc",
            page_size=2,
            max_records=2,
            ctx=offline_fixture_context("fred", "FredSeries"),
        ),
    )

    assert [row.date for row in result] == [date(2026, 3, 1), date(2026, 2, 1)]
    assert requests[0]["frequency"] == "m"
    assert requests[0]["aggregation_method"] == "eop"
    assert requests[0]["sort_order"] == "desc"


@pytest.mark.parametrize(
    "frequency",
    [
        "d",
        "w",
        "bw",
        "m",
        "q",
        "sa",
        "a",
        "wef",
        "weth",
        "wew",
        "wetu",
        "wem",
        "wesu",
        "wesa",
        "bwew",
        "bwem",
    ],
)
def test_series_accepts_each_official_frequency_code(frequency: str) -> None:
    query = FredSeriesFetcher().transform_query(series_id="GDP", frequency=frequency)
    assert query.frequency == frequency


@pytest.mark.parametrize("output_type", [2, 3])
def test_series_unknown_alternate_json_rows_fail_closed_without_disabling_type(
    monkeypatch: pytest.MonkeyPatch, output_type: int
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    requests: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append(params)
        return 200, _envelope(
            "observations",
            [{"id": "GDP", "observation": "unknown wire shape"}],
            count=1,
            limit=100_000,
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    with pytest.raises(FredProviderError) as error:
        FredSeriesFetcher().fetch(
            series_id="GDP",
            output_type=output_type,
            ctx=offline_fixture_context("fred", "FredSeries"),
        )

    assert error.value.code == "FRED_BAD_OBSERVATION"
    assert requests[0]["output_type"] == str(output_type)


def test_as_of_is_sent_as_a_single_day_vintage_and_dot_is_nullable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    requests: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append(params)
        return 200, _envelope(
            "observations",
            [_observation(".", realtime_start="2024-06-15")],
            count=1,
            limit=100_000,
        )

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    result = cast(
        "tuple[SeriesObservation, ...]",
        FredSeriesFetcher().fetch(
            series_id="CPIAUCSL",
            as_of=date(2024, 6, 15),
            ctx=offline_fixture_context("fred", "FredSeries"),
        ),
    )

    assert result[0].value is None
    assert requests[0]["realtime_start"] == "2024-06-15"
    assert requests[0]["realtime_end"] == "2024-06-15"
    assert requests[0]["aggregation_method"] == "avg"


@pytest.mark.parametrize("raw", [{"date": "2026-01-01"}, _observation("")])
def test_missing_or_empty_observation_value_fails_but_dot_is_the_null_sentinel(
    raw: dict[str, object],
) -> None:
    fetcher = FredSeriesFetcher()
    query = fetcher.transform_query(series_id="CPIAUCSL")
    with pytest.raises(FredProviderError) as error:
        fetcher.transform_data([raw], query)
    assert error.value.code == "FRED_BAD_OBSERVATION"


def test_search_rejects_bad_query_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args: pytest.fail("invalid query must not perform HTTP"),
    )
    fetcher = FredSearchFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(search_text="   ")
    with pytest.raises(ValidationError):
        fetcher.fetch(search_text="GDP", order_by="release_date")


@pytest.mark.parametrize(
    "query",
    [
        {"realtime_start": "2026-02-01", "realtime_end": "2026-01-01"},
        {"exclude_tag_names": ["annual"]},
        {"tag_names": []},
        {"tag_names": ["  "]},
        {"tag_names": ["gdp;quarterly"]},
        {"filter_variable": "release"},
        {"filter_value": "  "},
    ],
)
def test_search_rejects_invalid_filter_query_before_transport(
    monkeypatch: pytest.MonkeyPatch, query: dict[str, object]
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args: pytest.fail("invalid query must not perform HTTP"),
    )
    with pytest.raises(ValidationError):
        _fetch_with_dynamic_query(FredSearchFetcher().fetch, {"search_text": "GDP", **query})


@pytest.mark.parametrize(
    "query",
    [{"filter_variable": "frequency"}, {"filter_value": "Quarterly"}],
)
def test_search_filter_fields_are_independently_optional_and_transmitted(
    monkeypatch: pytest.MonkeyPatch, query: dict[str, object]
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    requests: list[dict[str, str]] = []

    def fake_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append(params)
        return 200, _envelope("seriess", [], count=0, limit=1000)

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", fake_get)
    _fetch_with_dynamic_query(
        FredSearchFetcher().fetch,
        {
            "ctx": offline_fixture_context("fred", "FredSearch"),
            "search_text": "GDP",
            **query,
        },
    )
    key, value = next(iter(query.items()))
    assert requests[0][key] == value


@pytest.mark.parametrize("filter_variable", ["frequency", "units", "seasonal_adjustment"])
def test_search_accepts_each_official_filter_variable(filter_variable: str) -> None:
    query = FredSearchFetcher().transform_query(search_text="GDP", filter_variable=filter_variable)
    assert query.filter_variable == filter_variable


@pytest.mark.parametrize(
    "query",
    [
        {"series_id": "GDP", "start_date": "2026-02-01", "end_date": "2026-01-01"},
        {"series_id": "GDP", "as_of": "2026-01-01", "realtime_start": "2026-01-01"},
        {"series_id": "GDP", "as_of": "2026-01-01", "vintage_dates": ["2026-01-01"]},
        {
            "series_id": "GDP",
            "realtime_start": "2026-01-01",
            "vintage_dates": ["2026-01-01"],
        },
        {"series_id": "GDP", "vintage_dates": []},
        {"series_id": "   "},
        {"series_id": "GDP\nBAD"},
        {"series_id": "GDP", "transform_units": "percent"},
        {"series_id": "GDP", "output_type": 5},
        {"series_id": "GDP", "frequency": "quarterly"},
        {"series_id": "GDP", "aggregation_method": "median"},
        {"series_id": "GDP", "sort_order": "descending"},
        {
            "series_id": "GDP",
            "realtime_start": "2026-02-01",
            "realtime_end": "2026-01-01",
        },
    ],
)
def test_series_rejects_invalid_query_before_transport(
    monkeypatch: pytest.MonkeyPatch, query: dict[str, object]
) -> None:
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args: pytest.fail("invalid query must not perform HTTP"),
    )
    with pytest.raises(ValidationError):
        _fetch_with_dynamic_query(FredSeriesFetcher().fetch, query)


def test_catalog_missing_or_wrong_raw_fields_fail_closed() -> None:
    fetcher = FredSearchFetcher()
    query = fetcher.transform_query(search_text="GDP")
    missing_required = _catalog_item()
    missing_required.pop("title")
    with pytest.raises(FredProviderError) as missing:
        fetcher.transform_data([missing_required], query)
    assert missing.value.code == "FRED_BAD_SERIES"

    with pytest.raises(FredProviderError) as wrong_type:
        fetcher.transform_data([_catalog_item(popularity="95")], query)
    assert wrong_type.value.code == "FRED_BAD_SERIES"


def test_catalog_optional_notes_preserve_missing_and_explicit_null() -> None:
    fetcher = FredSearchFetcher()
    query = fetcher.transform_query(search_text="GDP")
    missing_notes = _catalog_item()
    missing_notes.pop("notes")

    without_notes = fetcher.transform_data([missing_notes], query)
    explicit_null = fetcher.transform_data([_catalog_item(notes=None)], query)

    assert isinstance(without_notes[0], SeriesCatalogItem)
    assert isinstance(explicit_null[0], SeriesCatalogItem)
    assert without_notes[0].notes is None
    assert explicit_null[0].notes is None


@pytest.mark.parametrize(
    ("responses", "expected_code"),
    [
        ([json.dumps({"count": 1, "offset": 0, "limit": 1})], "FRED_PAGINATION_ERROR"),
        (
            [_envelope("seriess", [_catalog_item()], count=1, offset="0")],
            "FRED_PAGINATION_ERROR",
        ),
        (
            [_envelope("seriess", [], count=1, offset=0)],
            "FRED_PAGINATION_ERROR",
        ),
        (
            [
                _envelope("seriess", [_catalog_item()], count=2, offset=0),
                _envelope("seriess", [_catalog_item()], count=2, offset=0),
            ],
            "FRED_PAGINATION_ERROR",
        ),
        (
            [
                _envelope("seriess", [_catalog_item()], count=2, offset=0),
                _envelope("seriess", [_catalog_item(title="Conflicting title")], count=2, offset=1),
            ],
            "FRED_PAGINATION_CONFLICT",
        ),
        (
            [
                _envelope("seriess", [_catalog_item()], count=2, offset=0),
                _envelope("seriess", [_catalog_item()], count=3, offset=1),
            ],
            "FRED_PAGINATION_ERROR",
        ),
    ],
)
def test_search_rejects_incomplete_or_inconsistent_pages(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[str],
    expected_code: str,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    response_iter = iter(responses)
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda url, params, timeout: (200, next(response_iter)),
    )
    kwargs: dict[str, object] = {"search_text": "GDP", "page_size": 1, "max_records": 5}
    if expected_code in {"FRED_PAGINATION_CONFLICT"} or len(responses) == 2:
        kwargs["max_pages"] = 3
    with pytest.raises(FredProviderError) as error:
        _fetch_with_dynamic_query(
            FredSearchFetcher().fetch,
            {"ctx": offline_fixture_context("fred", "FredSearch"), **kwargs},
        )
    assert error.value.code == expected_code


@pytest.mark.parametrize(
    ("count", "max_records", "max_pages", "expected_limit"),
    [(3, 2, 5, "max_records"), (3, 5, 2, "max_pages")],
)
def test_search_reports_query_budget_limit_instead_of_returning_partial_rows(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    max_records: int,
    max_pages: int,
    expected_limit: str,
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "offline-test-key")
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda url, params, timeout: (
            200,
            _envelope("seriess", [_catalog_item()], count=count, offset=0),
        ),
    )
    with pytest.raises(FredQueryLimitError) as error:
        FredSearchFetcher().fetch(
            search_text="GDP",
            page_size=1,
            max_records=max_records,
            max_pages=max_pages,
            ctx=offline_fixture_context("fred", "FredSearch"),
        )
    assert error.value.code == "FRED_QUERY_LIMIT"
    assert error.value.partial is True
    assert error.value.limit_kind == expected_limit


def test_missing_key_and_http_errors_fail_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(
        "opendata.core.config.get_settings",
        lambda: type("Settings", (), {"fred_api_key": None, "fred_api_base_url": None})(),
    )
    monkeypatch.setattr(
        "opendata.data.providers.fred.models._client._http_get",
        lambda *args: pytest.fail("missing key must fail before HTTP"),
    )
    with pytest.raises(FredProviderError) as missing:
        FredSeriesFetcher().fetch(
            series_id="GDP",
            ctx=offline_fixture_context("fred", "FredSeries"),
        )
    assert missing.value.code == "FRED_API_KEY_MISSING"

    monkeypatch.setenv("FRED_API_KEY", "must-not-leak")
    requests: list[tuple[str, dict[str, str]]] = []

    def rate_limited(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
        requests.append((url, params))
        return 429, "rate limited"

    monkeypatch.setattr("opendata.data.providers.fred.models._client._http_get", rate_limited)
    with pytest.raises(FredProviderError) as http_error:
        FredSearchFetcher().fetch(
            search_text="GDP",
            ctx=offline_fixture_context("fred", "FredSearch"),
        )
    assert http_error.value.code == "FRED_HTTP_ERROR"
    assert http_error.value.status == 429
    assert requests[0][1]["api_key"] == "must-not-leak"
    assert "must-not-leak" not in str(http_error.value)


def test_contracts_reject_naive_catalog_timestamp_and_nonfinite_observation() -> None:
    with pytest.raises(ValidationError):
        SeriesCatalogItem.model_validate(
            {
                "series_id": "GDP",
                "title": "GDP",
                "frequency": "Quarterly",
                "units": "Billions",
                "seasonal_adjustment": "Adjusted",
                "observation_start": "1947-01-01",
                "observation_end": "2026-01-01",
                "last_updated": "2026-01-01 12:00:00",
                "notes": "",
            }
        )
    with pytest.raises(ValidationError, match="finite"):
        SeriesObservation(
            series_id="GDP",
            date=date(2026, 1, 1),
            value=float("inf"),
            realtime_start=date(2026, 1, 1),
            realtime_end=date(9999, 12, 31),
            transform_units="lin",
            output_type=1,
            requested_frequency=None,
            requested_aggregation_method="avg",
        )
