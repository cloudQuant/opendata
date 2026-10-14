"""Public-shape contract tests for the clean-room Multpl adapter."""

from __future__ import annotations

from datetime import date, datetime
from functools import partial

import pytest
from pydantic import ValidationError

from opendata.data.http_client import FailureCategory, HttpFetchError
from opendata.data.models.multpl import SP500Multiples
from opendata.data.protocol import FetchContext
from opendata.data.providers.multpl.models.sp500_multiples import (
    MultplProviderError,
    SP500MultiplesFetcher,
    parse_table,
)
from opendata.data.providers.multpl.specs import METRIC_PAGES, MULTPL_BASE_URL
from opendata.data.request_budget import RequestBudgetError

HTML_FIXTURE = """
<html><body><table class="historical-data">
<thead><tr><th>Date</th><th>Value</th></tr></thead>
<tbody>
<tr><td>Oct 9, 2026</td><td>26.66 †</td></tr>
<tr><td>Sep 1, 2026</td><td>26.04 †</td></tr>
</tbody></table></body></html>
"""

YIELD_FIXTURE = """
<table><tr><th>Date</th><th>Value</th></tr>
<tr><td>Oct 9, 2026</td><td>3.75% †</td></tr></table>
"""


def test_fixture_parses_published_date_value_and_estimate_marker() -> None:
    parsed = parse_table(HTML_FIXTURE, "pe_ratio", METRIC_PAGES["pe_ratio"])
    assert [(row.date, row.value, row.is_estimate) for row in parsed] == [
        (date(2026, 10, 9), 26.66, True),
        (date(2026, 9, 1), 26.04, True),
    ]
    output = SP500Multiples(
        metric=parsed[0].metric,
        frequency=parsed[0].frequency,
        date=parsed[0].date,
        value=parsed[0].value,
        value_unit=parsed[0].value_unit,
        is_estimate=parsed[0].is_estimate,
    )
    assert output.model_dump_json() == (
        '{"metric":"pe_ratio","frequency":"monthly","date":"2026-10-09",'
        '"value":26.66,"value_unit":"ratio","is_estimate":true}'
    )
    assert isinstance(output, SP500Multiples)


def test_percent_and_annual_specs_are_source_declared() -> None:
    yield_rows = parse_table(YIELD_FIXTURE, "earnings_yield", METRIC_PAGES["earnings_yield"])
    assert yield_rows[0].value == 3.75
    assert yield_rows[0].value_unit == "percent"
    assert yield_rows[0].frequency == "monthly"
    assert METRIC_PAGES["price_to_book"].frequency == "annual"
    assert METRIC_PAGES["price_to_sales"].frequency == "annual"


def test_fetcher_routes_single_metric_and_filters_inclusive_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    def get_html(url: str, timeout: float | None) -> tuple[int, str]:
        calls.append((url, timeout))
        return 200, HTML_FIXTURE

    monkeypatch.setattr(
        "opendata.data.providers.multpl.models.sp500_multiples._http_get_html",
        get_html,
    )
    fetcher = SP500MultiplesFetcher()
    params = fetcher.transform_query(
        source="multpl",
        market="us",
        metric="pe_ratio",
        start_date="2026-09-01",
        end_date="2026-10-09",
    )
    raw = fetcher.extract_data(params, FetchContext(timeout=2.5))
    rows = fetcher.transform_data(raw, params)

    assert calls == [(f"{MULTPL_BASE_URL}/s-p-500-pe-ratio/table/by-month", 2.5)]
    assert [row.date for row in rows] == [date(2026, 10, 9), date(2026, 9, 1)]
    assert all(isinstance(row, SP500Multiples) for row in rows)


def test_bad_query_shapes_fail_before_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def unexpected_request(url: str, timeout: float | None) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid query must fail before transport")

    monkeypatch.setattr(
        "opendata.data.providers.multpl.models.sp500_multiples._http_get_html",
        unexpected_request,
    )
    fetcher = SP500MultiplesFetcher()
    with pytest.raises(ValidationError):
        fetcher.transform_query(metric="unknown")
    with pytest.raises(ValidationError):
        fetcher.transform_query(start_date="2026-10-10", end_date="2026-10-09")
    with pytest.raises(ValidationError, match="include a time"):
        fetcher.transform_query(start_date=datetime(2026, 10, 9, 12))
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol="SPY")
    assert calls == 0


@pytest.mark.parametrize(
    ("html", "error_code"),
    [
        ("<html><p>no table</p></html>", "MULTPL_TABLE_MISSING"),
        ("<table><tr><th>Month</th><th>Close</th></tr></table>", "MULTPL_TABLE_MISSING"),
        (
            "<table><tr><th>Date</th><th>Value</th></tr><tr><td>Oct 9, 2026</td></tr></table>",
            "MULTPL_BAD_ROW",
        ),
        (
            "<table><tr><th>Date</th><th>Value</th></tr><tr><td>2026-10-09</td><td>26.2</td></tr></table>",
            "MULTPL_BAD_DATE",
        ),
        (
            "<table><tr><th>Date</th><th>Value</th></tr>"
            "<tr><td>Oct 9, 2026</td><td>NaN</td></tr></table>",
            "MULTPL_BAD_VALUE",
        ),
        (
            "<table><tr><th>Date</th><th>Value</th></tr>"
            "<tr><td>Oct 9, 2026</td><td>3.75%</td></tr></table>",
            "MULTPL_BAD_VALUE_UNIT",
        ),
    ],
)
def test_missing_and_bad_source_shapes_fail_closed(html: str, error_code: str) -> None:
    with pytest.raises(MultplProviderError) as error:
        parse_table(html, "pe_ratio", METRIC_PAGES["pe_ratio"])
    assert error.value.code == error_code


def test_empty_source_table_is_not_reported_as_success() -> None:
    html = "<table><tr><th>Date</th><th>Value</th></tr></table>"
    with pytest.raises(MultplProviderError, match="MULTPL_EMPTY_TABLE"):
        parse_table(html, "pe_ratio", METRIC_PAGES["pe_ratio"])


def test_rate_limit_and_access_block_are_specific_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_http_error(status: int) -> None:
        category = FailureCategory.RATE_LIMITED if status == 429 else FailureCategory.BLOCKED
        raise HttpFetchError(
            category,
            url="https://www.multpl.com/s-p-500-pe-ratio/table/by-month?token=secret",
            host="www.multpl.com",
            attempts=1,
            status=status,
        )

    def client_for_status(status_code: int) -> object:
        class Client:
            def get(self, *args: object, **kwargs: object) -> None:
                del args, kwargs
                raise_http_error(status_code)

        return Client()

    for status, code in ((429, "MULTPL_RATE_LIMITED"), (403, "MULTPL_BLOCKED")):
        monkeypatch.setattr(
            "opendata.data.providers.multpl.models.sp500_multiples.get_shared_http_client",
            partial(client_for_status, status),
        )
        with pytest.raises(MultplProviderError) as error:
            from opendata.data.providers.multpl.models.sp500_multiples import _http_get_html

            _http_get_html(f"{MULTPL_BASE_URL}/sample", None)
        assert error.value.code == code
        assert error.value.status == status
        assert "secret" not in str(error.value)


def test_request_budget_failures_propagate_without_relabeling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget_error = RequestBudgetError("scope budget exhausted")

    class Client:
        def get(self, *args: object, **kwargs: object) -> object:
            raise budget_error

    monkeypatch.setattr(
        "opendata.data.providers.multpl.models.sp500_multiples.get_shared_http_client",
        lambda: Client(),
    )
    from opendata.data.providers.multpl.models.sp500_multiples import _http_get_html

    with pytest.raises(RequestBudgetError) as error:
        _http_get_html(f"{MULTPL_BASE_URL}/sample", None)
    assert error.value is budget_error
