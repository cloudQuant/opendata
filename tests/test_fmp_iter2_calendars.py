"""Offline contract tests for the first FMP Iteration 2 model slice."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fmp_iter2 import (
    AvailableIndex,
    DividendCalendarEvent,
    EarningsCalendarEvent,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp import provider as fmp_provider
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models.iter2 import (
    FMP_CALENDAR_PAGE_LIMIT,
    AvailableIndicesFetcher,
    CalendarDividendFetcher,
    CalendarEarningsFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)


class _Response:
    def __init__(self, *, status: int, body: object) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body, allow_nan=True)


class _HTTPClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
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
    client = _HTTPClient([])
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    return client


def _context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="fmp",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline FMP fixture test grant",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    budget = RequestBudget(
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
    )
    return FetchContext(timeout=1.0, request_budget=budget)


def _ok(body: object) -> _Response:
    return _Response(status=200, body=body)


def _official_dividend() -> dict[str, object]:
    return {
        "symbol": "5871.TW",
        "date": "2026-07-29",
        "recordDate": "2026-07-30",
        "paymentDate": "2026-09-01",
        "declarationDate": "",
        "adjDividend": 5.8,
        "dividend": 5.916,
        "yield": 5.155555555555556,
        "frequency": "Annual",
    }


def _official_earnings() -> dict[str, object]:
    return {
        "symbol": "GRG.L",
        "date": "2026-07-29",
        "epsActual": 0.549,
        "epsEstimated": 0.501,
        "revenueActual": 1_101_500_000,
        "revenueEstimated": 1_086_300_000,
        "lastUpdated": "2026-07-30",
    }


def test_available_indices_fetcher_uses_official_list_shape_and_route(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [
        _ok(
            [
                {
                    "symbol": "^TTIN",
                    "name": "S&P/TSX Capped Industrials Index",
                    "exchange": "TSX",
                    "currency": "CAD",
                }
            ]
        )
    ]

    rows = AvailableIndicesFetcher().fetch(ctx=_context("AvailableIndices"))

    assert len(rows) == 1
    assert type(rows[0]) is AvailableIndex
    assert rows[0].model_dump() == {
        "symbol": "^TTIN",
        "name": "S&P/TSX Capped Industrials Index",
        "exchange": "TSX",
        "currency": "CAD",
    }
    assert http_client.calls == [
        (
            "https://financialmodelingprep.com/stable/index-list",
            {"apikey": "test-secret-key"},
            "fmp",
        )
    ]


def test_available_indices_requires_documented_fields_and_honors_record_budget(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [_ok([{"symbol": "^TTIN", "name": "Index", "exchange": "TSX"}])]
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_BAD_SHAPE"):
        AvailableIndicesFetcher().fetch(ctx=_context("AvailableIndices"))

    http_client.calls.clear()
    valid_index = {"symbol": "^TTIN", "name": "Index", "exchange": "TSX", "currency": "CAD"}
    http_client.responses = [_ok([valid_index, {**valid_index, "symbol": "^TST"}])]
    with pytest.raises(ValidationError):
        AvailableIndicesFetcher().fetch(
            ctx=_context("AvailableIndices"),
            max_records=0,
        )
    assert http_client.calls == []

    with pytest.raises(fmp_client.FMPQueryLimitError) as error:
        AvailableIndicesFetcher().fetch(
            ctx=_context("AvailableIndices"),
            max_records=1,
        )
    assert error.value.code == "FMP_INCOMPLETE_RECORD_BUDGET"
    assert error.value.requests_made == 1


def test_calendar_dividend_fetcher_preserves_official_empty_declaration_date(
    http_client: _HTTPClient,
) -> None:
    row = _official_dividend()
    http_client.responses = [_ok([row]), _ok([])]

    rows = CalendarDividendFetcher().fetch(
        ctx=_context("CalendarDividend"),
        start_date="2026-07-01",
        end_date="2026-09-29",
    )

    assert len(rows) == 1
    assert type(rows[0]) is DividendCalendarEvent
    assert rows[0].symbol == "5871.TW"
    assert rows[0].date.isoformat() == "2026-07-29"
    assert rows[0].declaration_date == ""
    assert rows[0].adjusted_dividend == 5.8
    assert [call[0] for call in http_client.calls] == [
        "https://financialmodelingprep.com/stable/dividends-calendar",
        "https://financialmodelingprep.com/stable/dividends-calendar",
    ]
    assert [call[1] for call in http_client.calls] == [
        {"from": "2026-07-01", "to": "2026-09-29", "page": "0", "apikey": "test-secret-key"},
        {"from": "2026-07-01", "to": "2026-09-29", "page": "1", "apikey": "test-secret-key"},
    ]


def test_calendar_earnings_fetcher_uses_documented_route_fields_and_page_parameter(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [_ok([_official_earnings()]), _ok([])]

    rows = CalendarEarningsFetcher().fetch(
        ctx=_context("CalendarEarnings"),
        start_date="2026-07-01",
        end_date="2026-09-29",
    )

    assert len(rows) == 1
    assert type(rows[0]) is EarningsCalendarEvent
    assert rows[0].eps_actual == 0.549
    assert rows[0].revenue_actual == 1_101_500_000
    assert rows[0].last_updated.isoformat() == "2026-07-30"
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
    ]


@pytest.mark.parametrize(
    ("fetcher_type", "kwargs"),
    [
        (CalendarDividendFetcher, {"start_date": "2026-01-01", "end_date": "2026-04-02"}),
        (CalendarEarningsFetcher, {"start_date": "2026-07-01T00:00:00", "end_date": "2026-07-02"}),
        (CalendarDividendFetcher, {"start_date": "2026-07-02", "end_date": "2026-07-01"}),
        (
            CalendarEarningsFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-07-02", "symbol": "AAPL"},
        ),
        (
            CalendarDividendFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-07-02", "max_requests": 0},
        ),
    ],
    ids=[
        "window-over-90-days",
        "datetime-bound",
        "reversed-window",
        "unsupported-symbol",
        "zero-page-budget",
    ],
)
def test_calendar_invalid_query_is_rejected_before_io(
    http_client: _HTTPClient,
    fetcher_type: type[CalendarDividendFetcher] | type[CalendarEarningsFetcher],
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        fetcher_type().fetch(**kwargs)
    assert http_client.calls == []


def test_calendar_paging_uses_zero_based_pages_until_empty_not_short_page(
    http_client: _HTTPClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("opendata.data.providers.fmp.models.iter2.FMP_CALENDAR_PAGE_LIMIT", 2)
    first = _official_earnings()
    second = {**_official_earnings(), "symbol": "AAPL"}
    http_client.responses = [_ok([first]), _ok([second]), _ok([])]

    rows = CalendarEarningsFetcher().fetch(
        ctx=_context("CalendarEarnings"),
        start_date="2026-07-01",
        end_date="2026-09-29",
        max_requests=3,
    )

    assert [row.symbol for row in rows] == ["GRG.L", "AAPL"]
    assert [call[1]["page"] for call in http_client.calls] == ["0", "1", "2"]
    assert FMP_CALENDAR_PAGE_LIMIT == 4_000


def test_calendar_page_and_record_limits_raise_explicit_incomplete_errors(
    http_client: _HTTPClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("opendata.data.providers.fmp.models.iter2.FMP_CALENDAR_PAGE_LIMIT", 2)
    full_page = [_official_dividend(), {**_official_dividend(), "symbol": "AAPL"}]
    http_client.responses = [_ok(full_page)]
    with pytest.raises(fmp_client.FMPQueryLimitError) as page_error:
        CalendarDividendFetcher().fetch(
            ctx=_context("CalendarDividend"),
            start_date="2026-07-01",
            end_date="2026-09-29",
            max_requests=1,
        )
    assert page_error.value.code == "FMP_INCOMPLETE_PAGE_BUDGET"
    assert page_error.value.requests_made == 1

    http_client.calls.clear()
    http_client.responses = [_ok(full_page)]
    with pytest.raises(fmp_client.FMPQueryLimitError) as record_error:
        CalendarDividendFetcher().fetch(
            ctx=_context("CalendarDividend"),
            start_date="2026-07-01",
            end_date="2026-09-29",
            max_records=1,
        )
    assert record_error.value.code == "FMP_INCOMPLETE_RECORD_BUDGET"
    assert record_error.value.requests_made == 1


@pytest.mark.parametrize(
    ("fetcher_type", "kwargs", "rows"),
    [
        (
            CalendarDividendFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{"symbol": "5871.TW"}],
        ),
        (
            CalendarDividendFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_dividend(), "unexpected": "field"}],
        ),
        (
            CalendarDividendFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_dividend(), "date": "2026-07-29T00:00:00"}],
        ),
        (
            CalendarEarningsFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_earnings(), "epsActual": True}],
        ),
        (
            CalendarEarningsFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_earnings(), "revenueActual": float("nan")}],
        ),
        (
            CalendarEarningsFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_earnings(), "date": "2026-10-01"}],
        ),
    ],
    ids=[
        "dividend-missing-required",
        "dividend-extra-field",
        "dividend-time-date",
        "earnings-boolean-number",
        "earnings-nan",
        "earnings-out-of-window",
    ],
)
def test_calendar_rejects_bad_shape_dates_and_numbers(
    http_client: _HTTPClient,
    fetcher_type: type[CalendarDividendFetcher] | type[CalendarEarningsFetcher],
    kwargs: dict[str, Any],
    rows: list[dict[str, object]],
) -> None:
    http_client.responses = [_ok(rows), _ok([])]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        fetcher_type().fetch(ctx=_context(fetcher_type.canonical_model or ""), **kwargs)
    assert error.value.code in {"FMP_BAD_SHAPE", "FMP_OUT_OF_WINDOW_ROW"}
    assert "test-secret-key" not in str(error.value)


def test_calendar_empty_result_is_a_valid_empty_contract(http_client: _HTTPClient) -> None:
    http_client.responses = [_ok([])]
    rows = CalendarDividendFetcher().fetch(
        ctx=_context("CalendarDividend"),
        start_date="2026-07-01",
        end_date="2026-07-02",
    )
    assert rows == ()
    assert http_client.calls[0][1]["page"] == "0"


def test_calendar_rate_limit_is_sanitized(http_client: _HTTPClient) -> None:
    http_client.responses = [_Response(status=429, body={"message": "blocked secret query"})]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        CalendarEarningsFetcher().fetch(
            ctx=_context("CalendarEarnings", attempts=1),
            start_date="2026-07-01",
            end_date="2026-07-02",
        )
    assert error.value.code == "FMP_RATE_LIMITED"
    assert error.value.status == 429
    assert "blocked" not in str(error.value)
    assert "test-secret-key" not in str(error.value)


def test_dividend_frame_roundtrip_preserves_empty_date_cell() -> None:
    row = DividendCalendarEvent.model_validate(_official_dividend())
    frame = DividendCalendarEvent.to_frame([row])

    restored = DividendCalendarEvent.from_frame(frame)

    assert restored == [row]
    assert restored[0].declaration_date == ""


def test_fmp_provider_binds_the_three_iteration_two_models() -> None:
    bindings = fmp_provider.PROVIDER.fetcher_dict
    fetchers = fmp_provider.PROVIDER.fetchers

    assert bindings["AvailableIndices"].class_name == "AvailableIndicesFetcher"
    assert bindings["CalendarDividend"].class_name == "CalendarDividendFetcher"
    assert bindings["CalendarEarnings"].class_name == "CalendarEarningsFetcher"
    assert {
        fetcher.canonical_model: fetcher.capability.domain
        for fetcher in fetchers
        if fetcher.canonical_model in {"AvailableIndices", "CalendarDividend", "CalendarEarnings"}
    } == {
        "AvailableIndices": "fmp_index_directory",
        "CalendarDividend": "fmp_dividend_calendar",
        "CalendarEarnings": "fmp_earnings_calendar",
    }
