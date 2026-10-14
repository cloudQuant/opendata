"""Offline contract tests for the FMP CalendarEvents aggregate."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fmp_iter2 import FmpCalendarEvent
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp import provider as fmp_provider
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models.iter2 import (
    FMP_CALENDAR_PAGE_LIMIT,
    CalendarEventsFetcher,
    CalendarEventsQuery,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_FIXTURE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "evidence"
    / "C100"
    / "fmp"
    / "fixtures"
    / "calendar-events.json"
)


class _Response:
    def __init__(self, *, status: int, body: object) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body, allow_nan=True)


class _HTTPClient:
    def __init__(self) -> None:
        self.responses: list[_Response] = []
        self.calls: list[tuple[str, dict[str, str], str]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        source: str,
    ) -> _Response:
        del timeout
        assert source == "fmp"
        self.calls.append((url, dict(params), source))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


@pytest.fixture
def http_client(monkeypatch: pytest.MonkeyPatch) -> _HTTPClient:
    client = _HTTPClient()
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    return client


def _context(*, attempts: int = 16) -> FetchContext:
    grant = RequestGrant(
        source="fmp",
        canonical_model="CalendarEvents",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline official-shape fixture grant",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        timeout=1.0,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        ),
    )


def _ok(body: object) -> _Response:
    return _Response(status=200, body=body)


def _fixtures() -> dict[str, dict[str, Any]]:
    return json.loads(_FIXTURE_PATH.read_text())


def _complete_responses() -> list[_Response]:
    rows = _fixtures()
    return [
        _ok([rows["earnings"]]),
        _ok([]),
        _ok([rows["dividends"]]),
        _ok([]),
        _ok([rows["ipos"]]),
        _ok([rows["splits"]]),
        _ok([]),
    ]


def test_calendar_events_aggregates_documented_company_calendars(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = _complete_responses()

    rows = CalendarEventsFetcher().fetch(
        ctx=_context(),
        start_date="2026-07-01",
        end_date="2026-09-29",
    )

    assert all(type(row) is FmpCalendarEvent for row in rows)
    assert [row.model_dump(mode="json") for row in rows] == [
        {"date": "2026-07-28", "symbol": "WHLR"},
        {"date": "2026-07-29", "symbol": "5871.TW"},
        {"date": "2026-07-29", "symbol": "GRG.L"},
        {"date": "2026-07-29", "symbol": "IMC"},
    ]
    assert [call[0].rsplit("/", 1)[-1] for call in http_client.calls] == [
        "earnings-calendar",
        "earnings-calendar",
        "dividends-calendar",
        "dividends-calendar",
        "ipos-calendar",
        "splits-calendar",
        "splits-calendar",
    ]
    assert [call[1] for call in http_client.calls] == [
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "0",
            "includeReportTimes": "false",
            "apikey": "test-secret-key",
        },
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "1",
            "includeReportTimes": "false",
            "apikey": "test-secret-key",
        },
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "0",
            "apikey": "test-secret-key",
        },
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "1",
            "apikey": "test-secret-key",
        },
        {"from": "2026-07-01", "to": "2026-09-29", "apikey": "test-secret-key"},
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "0",
            "apikey": "test-secret-key",
        },
        {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "1",
            "apikey": "test-secret-key",
        },
    ]


def test_calendar_events_deduplicates_rows_not_distinguishable_by_public_contract(
    http_client: _HTTPClient,
) -> None:
    rows = _fixtures()
    same_identity = {**rows["earnings"], "symbol": "AAPL"}
    same_dividend_identity = {**rows["dividends"], "symbol": "AAPL"}
    http_client.responses = [
        _ok([same_identity]),
        _ok([]),
        _ok([same_dividend_identity]),
        _ok([]),
        _ok([]),
        _ok([]),
    ]

    result = CalendarEventsFetcher().fetch(
        ctx=_context(),
        start_date="2026-07-01",
        end_date="2026-09-29",
    )

    assert result == (FmpCalendarEvent(date="2026-07-29", symbol="AAPL"),)
    assert len(http_client.calls) == 6


@pytest.mark.parametrize(
    ("responses", "expected_calls", "expected_code"),
    [
        (
            [_ok([{**_fixtures()["earnings"], "unexpected": 1}])],
            1,
            "FMP_BAD_SHAPE",
        ),
        (
            [
                _ok([]),
                _ok([]),
                _ok(
                    [
                        {
                            key: value
                            for key, value in _fixtures()["ipos"].items()
                            if key != "dealSize"
                        }
                    ]
                ),
            ],
            3,
            "FMP_BAD_SHAPE",
        ),
        (
            [
                _ok([]),
                _ok([]),
                _ok([]),
                _ok([{**_fixtures()["splits"], "date": "2026-10-01"}]),
            ],
            4,
            "FMP_OUT_OF_WINDOW_ROW",
        ),
    ],
    ids=["strict-earnings-fields", "strict-ipo-fields", "reject-out-of-window"],
)
def test_calendar_events_fails_closed_on_bad_source_rows(
    http_client: _HTTPClient,
    responses: list[_Response],
    expected_calls: int,
    expected_code: str,
) -> None:
    http_client.responses = responses

    with pytest.raises(fmp_client.FMPProviderError) as error:
        CalendarEventsFetcher().fetch(
            ctx=_context(),
            start_date="2026-07-01",
            end_date="2026-09-29",
        )

    assert error.value.code == expected_code
    assert len(http_client.calls) == expected_calls
    assert "test-secret-key" not in str(error.value)


@pytest.mark.parametrize(
    "query",
    [
        {"start_date": "2026-07-01T00:00:00", "end_date": "2026-07-02"},
        {"start_date": "2026-07-02", "end_date": "2026-07-01"},
        {"start_date": "2026-07-01", "end_date": "2026-10-01"},
        {"start_date": "2026-07-01", "symbol": "AAPL"},
        {"max_requests": 3},
        {"max_records": 0},
        {"market": "us"},
    ],
    ids=[
        "datetime-bound",
        "reversed-window",
        "over-90-days",
        "unsupported-symbol",
        "too-few-requests",
        "zero-record-budget",
        "wrong-market",
    ],
)
def test_calendar_events_invalid_queries_fail_before_io(
    http_client: _HTTPClient,
    query: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CalendarEventsFetcher().fetch(**query)
    assert http_client.calls == []


@pytest.mark.parametrize(
    ("query", "expected_delta"),
    [
        ({}, 90),
        ({"start_date": "2026-07-01"}, 90),
        ({"end_date": "2026-07-01"}, 90),
    ],
    ids=["both-omitted", "end-defaulted", "start-defaulted"],
)
def test_calendar_events_completes_optional_public_date_window(
    query: dict[str, object], expected_delta: int
) -> None:
    params = CalendarEventsQuery.model_validate(query)

    assert params.start_date is not None
    assert params.end_date is not None
    assert (params.end_date - params.start_date).days == expected_delta


def test_calendar_events_transform_rejects_unvalidated_empty_window() -> None:
    fetcher = CalendarEventsFetcher()
    unvalidated = CalendarEventsQuery.model_construct(start_date=None, end_date=None)

    with pytest.raises(fmp_client.FMPProviderError, match="FMP_INVALID_QUERY"):
        fetcher.transform_data([], unvalidated)

    validated = fetcher.transform_query()
    assert validated.start_date is not None
    assert validated.end_date is not None
    assert fetcher.transform_data([], validated) == ()


def test_calendar_events_combined_budget_detects_unproven_page_tail(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [_ok([_fixtures()["earnings"]])]

    with pytest.raises(fmp_client.FMPQueryLimitError) as error:
        CalendarEventsFetcher().fetch(
            ctx=_context(),
            start_date="2026-07-01",
            end_date="2026-09-29",
            max_requests=4,
        )

    assert error.value.code == "FMP_INCOMPLETE_PAGE_BUDGET"
    assert error.value.requests_made == 1
    assert len(http_client.calls) == 1


def test_calendar_events_combined_budget_enforces_max_records(
    http_client: _HTTPClient,
) -> None:
    valid = _fixtures()["earnings"]
    http_client.responses = [_ok([valid, {**valid, "symbol": "AAPL"}])]

    with pytest.raises(fmp_client.FMPQueryLimitError) as error:
        CalendarEventsFetcher().fetch(
            ctx=_context(),
            start_date="2026-07-01",
            end_date="2026-09-29",
            max_records=1,
        )

    assert error.value.code == "FMP_INCOMPLETE_RECORD_BUDGET"
    assert error.value.requests_made == 1


def test_calendar_events_rejects_provider_page_limit_violation(
    http_client: _HTTPClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("opendata.data.providers.fmp.models.iter2.FMP_CALENDAR_PAGE_LIMIT", 1)
    valid = _fixtures()["earnings"]
    http_client.responses = [_ok([valid, {**valid, "symbol": "AAPL"}])]

    with pytest.raises(fmp_client.FMPProviderError) as error:
        CalendarEventsFetcher().fetch(
            ctx=_context(),
            start_date="2026-07-01",
            end_date="2026-09-29",
        )

    assert FMP_CALENDAR_PAGE_LIMIT == 4_000
    assert error.value.code == "FMP_PROVIDER_LIMIT_VIOLATION"
    assert error.value.requests_made == 1


def test_calendar_events_provider_model_query_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    http_client: _HTTPClient,
) -> None:
    http_client.responses = _complete_responses()
    fetcher = CalendarEventsFetcher()
    capability = fetcher.capability
    query = {"start_date": "2026-07-01", "end_date": "2026-09-29"}
    observed_query: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        observed_query.update(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    descriptor = SimpleNamespace(
        source="fmp",
        model="CalendarEvents",
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, FmpCalendarEvent),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model="CalendarEvents",
            query=query,
            principal=Principal(),
            ctx=_context(),
        )
    )

    assert response["source"] == "fmp"
    assert response["model"] == "CalendarEvents"
    assert response["domain"] == "fmp_calendar_events"
    assert observed_query["source"] == "fmp"
    assert observed_query["market"] == "global"
    expected = [
        {"date": "2026-07-28", "symbol": "WHLR"},
        {"date": "2026-07-29", "symbol": "5871.TW"},
        {"date": "2026-07-29", "symbol": "GRG.L"},
        {"date": "2026-07-29", "symbol": "IMC"},
    ]
    assert response["results"] == expected


def test_calendar_events_provider_binding_and_public_output_fields() -> None:
    binding = fmp_provider.PROVIDER.fetcher_dict["CalendarEvents"]
    fetchers = fmp_provider.PROVIDER.fetchers

    assert binding.class_name == "CalendarEventsFetcher"
    assert {
        fetcher.canonical_model: fetcher.capability.domain
        for fetcher in fetchers
        if fetcher.canonical_model == "CalendarEvents"
    } == {"CalendarEvents": "fmp_calendar_events"}
    assert set(FmpCalendarEvent.model_fields) == {"date", "symbol"}
