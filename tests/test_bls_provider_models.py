"""Offline contract tests for the BLS search and series prototypes."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.providers.bls.models import _client
from opendata.data.providers.bls.models._client import BlsProviderError
from opendata.data.providers.bls.models._contracts import (
    BlsCatalogPage,
    BlsObservation,
)
from opendata.data.providers.bls.models.search import BlsSearchFetcher
from opendata.data.providers.bls.models.series import BlsSeriesFetcher
from tests.provider_budget_fixtures import offline_fixture_context


def _api_response(
    series_ids: list[str],
    start_year: int,
    end_year: int,
    *,
    observations: dict[str, list[dict[str, Any]]] | None = None,
    messages: list[str] | None = None,
    status: str = "REQUEST_SUCCEEDED",
    object_results: bool = False,
) -> str:
    """Build the official BLS response envelope for a fake request."""
    observations = observations or {series_id: [] for series_id in series_ids}
    result_group = {
        "series": [
            {"seriesID": series_id, "data": observations.get(series_id, [])}
            for series_id in series_ids
        ]
    }
    return json.dumps(
        {
            "status": status,
            "responseTime": 1,
            "message": messages or [],
            "Results": result_group if object_results else [result_group],
        }
    )


def _observation(year: int, period: str = "M01", **overrides: object) -> dict[str, object]:
    """Return a small source-shaped observation."""
    return {
        "year": str(year),
        "period": period,
        "periodName": "January" if period != "M13" else "Annual",
        "value": "123.5",
        "footnotes": [{}],
        **overrides,
    }


def test_search_reads_different_survey_headers_and_returns_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Survey-specific extra columns remain typed dimensions across a page."""
    bodies = {
        "cu": (
            b"series_id\tseries_title\tperiodicity_code\tseasonal\n"
            b"CU_A\tConsumer series A\tR\tU\n"
            b"CU_B\tConsumer series B\tR\tS\n"
            b"CU_C\tConsumer series C\tS\tU\n"
            b"CU_D\tConsumer series D\tR\tU\n"
        ),
        "ce": (
            b"series_id\tseries_title\texpenditure_category\treference_year\n"
            b"CE_A\tFood at home\tFood\t2023\n"
            b"CE_B\tHousing\tShelter\t2023\n"
        ),
    }
    urls: list[str] = []

    def fake_get(url: str, _timeout: float | None) -> tuple[int, bytes]:
        urls.append(url)
        return 200, bodies[url.split("/")[-2]]

    monkeypatch.setattr(_client, "_http_get", fake_get)
    fetcher = BlsSearchFetcher()
    page = fetcher.fetch(
        survey="cu",
        search_text="consumer series",
        offset=1,
        limit=2,
        ctx=offline_fixture_context("bls", "BlsSearch"),
    )

    assert isinstance(page, BlsCatalogPage)
    assert page.total == 4
    assert page.offset == 1
    assert page.limit == 2
    assert [row.series_id for row in page] == ["CU_B", "CU_C"]
    assert page[0].dimensions == {"periodicity_code": "R", "seasonal": "S"}
    assert page[0].frequency is None
    assert page[0].catalog_as_of is None
    assert urls == ["https://download.bls.gov/pub/time.series/cu/cu.series"]

    ce_page = cast(
        "BlsCatalogPage",
        fetcher.fetch(
            survey="ce",
            search_text="housing",
            limit=1,
            ctx=offline_fixture_context("bls", "BlsSearch"),
        ),
    )
    assert ce_page.total == 1
    assert ce_page[0].survey == "CE"
    assert ce_page[0].dimensions == {"expenditure_category": "Shelter", "reference_year": "2023"}


def test_search_allows_id_lookup_when_title_header_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog without titles still provides complete ID search."""
    monkeypatch.setattr(
        _client,
        "_http_get",
        lambda _url, _timeout: (200, b"series_id\tsegment\nLA_X\tarea-a\nLA_Y\tarea-b\n"),
    )

    page = cast(
        "BlsCatalogPage",
        BlsSearchFetcher().fetch(
            survey="la",
            search_text="LA_Y",
            ctx=offline_fixture_context("bls", "BlsSearch"),
        ),
    )
    assert page.total == 1
    assert page[0].series_id == "LA_Y"
    assert page[0].title is None
    assert page[0].dimensions == {"segment": "area-b"}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"", "BLS_CATALOG_EMPTY"),
        (b"\xef\xbb\xbfseries_id\tseries_title\nX\tTitle\n", "BLS_CATALOG_BOM"),
        (b"<html><body>not a catalog</body></html>", "BLS_CATALOG_HTML"),
        (b"series_title\tsegment\nTitle\ta\n", "BLS_CATALOG_BAD_HEADER"),
        (b"series_id\tseries_title\n\tMissing ID\n", "BLS_CATALOG_MISSING_ID"),
        (b"series_id\tseries_title\nX\tA\nX\tB\n", "BLS_CATALOG_DUPLICATE_CONFLICT"),
        (b"series_id\tseries_title\nX\tA\textra\n", "BLS_CATALOG_BAD_TSV"),
    ],
)
def test_search_rejects_bad_catalogs(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    code: str,
) -> None:
    """Malformed files fail with a stable provider code."""
    monkeypatch.setattr(_client, "_http_get", lambda _url, _timeout: (200, body))
    with pytest.raises(BlsProviderError) as exc_info:
        BlsSearchFetcher().fetch(
            survey="cu",
            ctx=offline_fixture_context("bls", "BlsSearch"),
        )
    assert exc_info.value.code == code


def test_search_enforces_catalog_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The configured file-size cap is checked before TSV parsing."""
    monkeypatch.setattr(_client, "MAX_CATALOG_BYTES", 8)
    monkeypatch.setattr(_client, "_http_get", lambda _url, _timeout: (200, b"series_id\tX\n"))
    with pytest.raises(BlsProviderError, match="BLS_CATALOG_TOO_LARGE"):
        BlsSearchFetcher().fetch(
            survey="cu",
            ctx=offline_fixture_context("bls", "BlsSearch"),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"survey": "CU"},
        {"survey": "../"},
        {"survey": "c/"},
        {"survey": "cu", "search_text": "   "},
    ],
)
def test_search_invalid_query_performs_no_io(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
) -> None:
    """Survey path and empty-search validation happen before transport."""
    calls: list[str] = []
    monkeypatch.setattr(_client, "_http_get", lambda url, _timeout: calls.append(url))
    with pytest.raises(ValidationError):
        BlsSearchFetcher().fetch(**kwargs)  # type: ignore[arg-type]
    assert calls == []


def test_series_uses_v2_key_and_splits_more_than_one_year_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple IDs and a 21-year range use two complete v2 requests."""
    secret = "bls-test-secret-never-log"
    monkeypatch.setenv("BLS_API_KEY", secret)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, payload: dict[str, object], _timeout: float | None) -> tuple[int, str]:
        calls.append((url, dict(payload)))
        ids = payload["seriesid"]
        assert isinstance(ids, list)
        start_year = int(str(payload["startyear"]))
        end_year = int(str(payload["endyear"]))
        rows = {
            series_id: [_observation(start_year, "M13"), _observation(end_year, "M02")]
            for series_id in ids
        }
        return 200, _api_response(ids, start_year, end_year, observations=rows)

    monkeypatch.setattr(_client, "_http_post_json", fake_post)
    result = cast(
        "tuple[BlsObservation, ...]",
        BlsSeriesFetcher().fetch(
            series_ids=["CUUR0000SA0", "CUSR0000SA0"],
            start_year=2000,
            end_year=2020,
            max_requests=2,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        ),
    )

    assert len(calls) == 2
    assert all(url == _client.BLS_API_V2_URL for url, _payload in calls)
    assert [(int(str(body["startyear"])), int(str(body["endyear"]))) for _, body in calls] == [
        (2000, 2019),
        (2020, 2020),
    ]
    assert all(body["registrationkey"] == secret for _, body in calls)
    assert len(result) == 8
    annual = [row for row in result if row.period == "M13"]
    assert len(annual) == 4
    assert all(row.period_name == "Annual" for row in annual)
    assert all(row.latest is None for row in result)
    assert all(row.value == 123.5 for row in result)


def test_series_v1_without_key_batches_ids_and_years(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key selects documented v1 caps and omits registrationkey."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, payload: dict[str, object], _timeout: float | None) -> tuple[int, str]:
        calls.append((url, dict(payload)))
        ids = payload["seriesid"]
        assert isinstance(ids, list)
        start_year = int(str(payload["startyear"]))
        end_year = int(str(payload["endyear"]))
        return 200, _api_response(ids, start_year, end_year)

    monkeypatch.setattr(_client, "_http_post_json", fake_post)
    result = BlsSeriesFetcher().fetch(
        series_ids=["SERIES_A", "SERIES_B"],
        start_year=2010,
        end_year=2020,
        max_requests=2,
        ctx=offline_fixture_context("bls", "BlsSeries"),
    )
    assert result == ()
    assert all(url == _client.BLS_API_V1_URL for url, _body in calls)
    assert [(body["startyear"], body["endyear"]) for _url, body in calls] == [
        ("2010", "2019"),
        ("2020", "2020"),
    ]
    assert all("registrationkey" not in body for _url, body in calls)


def test_series_splits_more_ids_than_v1_request_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 25-series v1 limit creates a second complete request batch."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    series_ids = [f"SERIES_{index:02d}" for index in range(26)]
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(url: str, payload: dict[str, object], _timeout: float | None) -> tuple[int, str]:
        calls.append((url, dict(payload)))
        ids = payload["seriesid"]
        assert isinstance(ids, list)
        return 200, _api_response(ids, 2024, 2024)

    monkeypatch.setattr(_client, "_http_post_json", fake_post)
    result = BlsSeriesFetcher().fetch(
        series_ids=series_ids,
        start_year=2024,
        end_year=2024,
        max_requests=2,
        ctx=offline_fixture_context("bls", "BlsSeries"),
    )

    assert result == ()
    assert [len(body["seriesid"]) for _url, body in calls] == [25, 1]  # type: ignore[arg-type]
    assert all(url == _client.BLS_API_V1_URL for url, _body in calls)


def test_series_date_query_uses_calendar_year_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Date compatibility maps to whole BLS calendar years."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    calls: list[dict[str, object]] = []

    def fake_post(_url: str, payload: dict[str, object], _timeout: float | None) -> tuple[int, str]:
        calls.append(dict(payload))
        ids = payload["seriesid"]
        assert isinstance(ids, list)
        year = int(str(payload["startyear"]))
        return 200, _api_response(ids, year, year)

    monkeypatch.setattr(_client, "_http_post_json", fake_post)
    result = cast(
        "tuple[BlsObservation, ...]",
        BlsSeriesFetcher().fetch(
            series_ids=["SERIES_A"],
            start_date="2022-03-02",
            end_date="2022-04-01",
            max_requests=1,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        ),
    )

    assert result == ()
    assert [(body["startyear"], body["endyear"]) for body in calls] == [("2022", "2022")]


def test_series_maps_footnotes_latest_and_preliminary_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Footnote code/text is retained and only P marks preliminary."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    raw = _observation(
        2024,
        "M13",
        latest="true",
        footnotes=[{"code": "P", "text": "Preliminary."}, {"code": "R", "text": "Revised."}],
    )
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024, observations={"SERIES_A": [raw]}),
        ),
    )

    result = BlsSeriesFetcher().fetch(
        series_ids=["SERIES_A"],
        start_year=2024,
        end_year=2024,
        max_requests=1,
        ctx=offline_fixture_context("bls", "BlsSeries"),
    )
    assert isinstance(result[0], BlsObservation)
    assert result[0].period == "M13"
    assert result[0].period_name == "Annual"
    assert result[0].latest is True
    assert result[0].preliminary is True
    assert [(note.code, note.text) for note in result[0].footnotes] == [
        ("P", "Preliminary."),
        ("R", "Revised."),
    ]


@pytest.mark.parametrize(("api_version", "api_key"), (("v1", None), ("v2", "offline-key")))
def test_series_results_object_and_group_array_are_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    api_version: str,
    api_key: str | None,
) -> None:
    """Both published Results envelopes preserve annual and footnote fields."""
    if api_key is None:
        monkeypatch.delenv("BLS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("BLS_API_KEY", api_key)

    observation = _observation(
        2024,
        "M13",
        latest="true",
        footnotes=[{"code": "P", "text": "Preliminary."}, {"code": "R", "text": "Revised."}],
    )
    outputs: list[tuple[BlsObservation, ...]] = []
    for object_results in (False, True):

        def fake_post(
            _url: str,
            payload: dict[str, object],
            _timeout: float | None,
            *,
            _object_results: bool = object_results,
        ) -> tuple[int, str]:
            ids = payload["seriesid"]
            assert ids == ["SERIES_A"]
            return (
                200,
                _api_response(
                    ["SERIES_A"],
                    2024,
                    2024,
                    observations={"SERIES_A": [observation]},
                    object_results=_object_results,
                ),
            )

        monkeypatch.setattr(_client, "_http_post_json", fake_post)
        result = cast(
            "tuple[BlsObservation, ...]",
            BlsSeriesFetcher().fetch(
                series_ids=["SERIES_A"],
                start_year=2024,
                end_year=2024,
                max_requests=1,
                ctx=offline_fixture_context("bls", "BlsSeries"),
            ),
        )
        outputs.append(result)

    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 1
    row = outputs[0][0]
    assert row.api_version == api_version
    assert row.period == "M13"
    assert row.period_name == "Annual"
    assert row.latest is True
    assert row.preliminary is True
    assert [(note.code, note.text) for note in row.footnotes] == [
        ("P", "Preliminary."),
        ("R", "Revised."),
    ]


@pytest.mark.parametrize(
    ("results", "expected_code"),
    (
        (None, "BLS_BAD_RESPONSE"),
        ({"not_series": []}, "BLS_BAD_RESPONSE"),
        ([], "BLS_MISSING_SERIES"),
        ([{}], "BLS_BAD_RESPONSE"),
        ([{"series": []}], "BLS_MISSING_SERIES"),
        ({"series": []}, "BLS_MISSING_SERIES"),
    ),
)
def test_series_rejects_malformed_or_empty_results_groups(
    results: object,
    expected_code: str,
) -> None:
    """Only the published list or object-with-series envelopes are accepted."""
    document = {
        "status": "REQUEST_SUCCEEDED",
        "message": [],
        "Results": results,
    }
    with pytest.raises(BlsProviderError) as exc_info:
        _client._parse_api_response(
            document,
            expected_ids=("SERIES_A",),
            start_year=2024,
            end_year=2024,
            api_version="v2",
            url=_client.BLS_API_V2_URL,
        )
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("status", "messages", "expected_code"),
    [
        ("REQUEST_FAILED", [], "BLS_API_STATUS"),
        ("REQUEST_SUCCEEDED", ["Invalid Series for Series SERIES_A!"], "BLS_INVALID_SERIES_ID"),
        ("REQUEST_SUCCEEDED", ["No data was returned"], "BLS_API_MESSAGE"),
    ],
)
def test_series_rejects_bad_status_and_ambiguous_empty_response(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    messages: list[str],
    expected_code: str,
) -> None:
    """Invalid-ID and other messages cannot become successful empty data."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(
                ["SERIES_A"],
                2024,
                2024,
                messages=messages,
                status=status,
            ),
        ),
    )
    with pytest.raises(BlsProviderError) as exc_info:
        BlsSeriesFetcher().fetch(
            series_ids=["SERIES_A"],
            start_year=2024,
            end_year=2024,
            max_requests=1,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        )
    assert exc_info.value.code == expected_code


def test_series_accepts_documented_empty_data_without_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present requested series with an empty data list is a valid empty window."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024),
        ),
    )
    result = BlsSeriesFetcher().fetch(
        series_ids=["SERIES_A"],
        start_year=2024,
        end_year=2024,
        max_requests=1,
        ctx=offline_fixture_context("bls", "BlsSeries"),
    )
    assert result == ()


def test_series_rejects_missing_requested_id_and_bad_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing requested IDs and malformed observation data fail closed."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024),
        ),
    )
    with pytest.raises(BlsProviderError, match="BLS_MISSING_SERIES"):
        BlsSeriesFetcher().fetch(
            series_ids=["SERIES_A", "SERIES_B"],
            start_year=2024,
            end_year=2024,
            max_requests=1,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        )

    malformed = _observation(2024)
    malformed["footnotes"] = "not-a-list"
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024, observations={"SERIES_A": [malformed]}),
        ),
    )
    with pytest.raises(BlsProviderError, match="BLS_BAD_OBSERVATION"):
        BlsSeriesFetcher().fetch(
            series_ids=["SERIES_A"],
            start_year=2024,
            end_year=2024,
            max_requests=1,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        )


@pytest.mark.parametrize("bad_value", ["", ".", "-", "NaN", "inf", "1_000"])
def test_series_rejects_undocumented_missing_or_nonfinite_values(
    monkeypatch: pytest.MonkeyPatch,
    bad_value: str,
) -> None:
    """Only finite numeric strings are accepted without a documented sentinel."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    raw = _observation(2024, value=bad_value)
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024, observations={"SERIES_A": [raw]}),
        ),
    )
    with pytest.raises(BlsProviderError, match="BLS_BAD_OBSERVATION"):
        BlsSeriesFetcher().fetch(
            series_ids=["SERIES_A"],
            start_year=2024,
            end_year=2024,
            max_requests=1,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        )


@pytest.mark.parametrize("conflicts", [False, True])
def test_series_deduplicates_identical_observations_and_rejects_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    conflicts: bool,
) -> None:
    """Repeated source keys deduplicate only when their full source rows agree."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    first = _observation(2024, "M01")
    second = dict(first)
    if conflicts:
        second["value"] = "124.0"
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, _payload, _timeout: (
            200,
            _api_response(["SERIES_A"], 2024, 2024, observations={"SERIES_A": [first, second]}),
        ),
    )

    if conflicts:
        with pytest.raises(BlsProviderError, match="BLS_OBSERVATION_CONFLICT"):
            BlsSeriesFetcher().fetch(
                series_ids=["SERIES_A"],
                start_year=2024,
                end_year=2024,
                max_requests=1,
                ctx=offline_fixture_context("bls", "BlsSeries"),
            )
    else:
        result = cast(
            "tuple[BlsObservation, ...]",
            BlsSeriesFetcher().fetch(
                series_ids=["SERIES_A"],
                start_year=2024,
                end_year=2024,
                max_requests=1,
                ctx=offline_fixture_context("bls", "BlsSeries"),
            ),
        )
        assert len(result) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"series_ids": [], "start_year": 2024, "end_year": 2024, "max_requests": 1},
        {"series_ids": ["A", "A"], "start_year": 2024, "end_year": 2024, "max_requests": 1},
        {"series_ids": ["A"], "start_year": 2025, "end_year": 2024, "max_requests": 1},
        {"series_ids": ["A"], "start_year": 2024, "max_requests": 1},
        {"series_ids": ["A"], "start_date": "2024-01-01", "max_requests": 1},
        {
            "series_ids": ["A"],
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "as_of": "2024-12-31",
            "max_requests": 1,
        },
        {"series_ids": ["A"], "start_year": 2024, "end_year": 2024},
    ],
)
def test_series_invalid_queries_perform_no_io(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
) -> None:
    """Invalid IDs, windows, as-of and missing budget fail before requests."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, payload, _timeout: calls.append(payload),
    )
    with pytest.raises(ValidationError):
        BlsSeriesFetcher().fetch(**kwargs)  # type: ignore[arg-type]
    assert calls == []


def test_series_budget_fails_before_any_partial_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request budget smaller than the complete year plan never truncates."""
    monkeypatch.delenv("BLS_API_KEY", raising=False)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        _client,
        "_http_post_json",
        lambda _url, payload, _timeout: calls.append(payload),
    )
    with pytest.raises(_client.BlsRequestBudgetError) as exc_info:
        BlsSeriesFetcher().fetch(
            series_ids=["A", "B"],
            start_year=2000,
            end_year=2020,
            max_requests=2,
            ctx=offline_fixture_context("bls", "BlsSeries"),
        )
    assert exc_info.value.required_requests == 3
    assert exc_info.value.incomplete is True
    assert calls == []


def test_series_key_is_redacted_from_transport_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an exception that embeds the request body does not expose the key."""
    secret = "do-not-print-bls-key"
    monkeypatch.setenv("BLS_API_KEY", secret)

    class _FakeClient:
        def request(self, *_args: object, **kwargs: object) -> object:
            assert kwargs["json_body"]["registrationkey"] == secret  # type: ignore[index]
            raise RuntimeError(f"transport echoed {secret}")

    monkeypatch.setattr(_client, "get_shared_http_client", lambda: _FakeClient())
    with pytest.raises(BlsProviderError) as exc_info:
        _client._http_post_json(
            _client.BLS_API_V2_URL,
            {"registrationkey": secret},
            None,
        )
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


def test_models_declare_unverified_bounded_capabilities() -> None:
    """The two prototype domains remain explicit and excluded from auto routing."""
    search = BlsSearchFetcher()
    series = BlsSeriesFetcher()
    assert search.async_mode == "bounded_thread"
    assert series.async_mode == "bounded_thread"
    assert search.capability == Capability(
        asset_class="macro",
        domain="bls_search",
        period="snapshot",
        market="us",
        source="bls",
        verified=False,
    )
    assert series.capability == Capability(
        asset_class="macro",
        domain="bls_series",
        period="variable",
        market="us",
        source="bls",
        verified=False,
    )
