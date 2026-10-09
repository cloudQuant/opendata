"""Offline fixtures for the unregistered FMP stable equity price models."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from opendata.data.providers.catalog import register_provider
from opendata.data.providers.fmp.models import (
    EquityHistorical,
    EquityHistoricalFetcher,
    EquityQuote,
    EquityQuoteFetcher,
    FMPProviderError,
    FMPQueryLimitError,
    _client,
)
from opendata.data.registry import ProviderRegistry
from tests.provider_budget_fixtures import offline_fixture_context

TEST_API_KEY = "offline-test-fmp-key"


def _historical_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "AAPL",
        "date": "2026-01-02",
        "open": 100.0,
        "high": 103.0,
        "low": 99.5,
        "close": 102.0,
        "volume": 1_234_567,
        "change": 2.0,
        "changePercent": 2.0,
        "vwap": 101.4,
    }
    row.update(updates)
    return row


def _quote_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "price": 102.0,
        "changePercentage": 2.0,
        "change": 2.0,
        "volume": 1_234_567,
        "dayLow": 99.5,
        "dayHigh": 103.0,
        "yearLow": 70.0,
        "yearHigh": 110.0,
        "marketCap": 3_000_000_000_000,
        "priceAvg50": 100.0,
        "priceAvg200": 95.0,
        "exchange": "NASDAQ",
        "open": 100.0,
        "previousClose": 100.0,
        "timestamp": 1_767_355_200,
    }
    row.update(updates)
    return row


def _historical_contract(**updates: object) -> EquityHistorical:
    row = _historical_row(query_window_scope="explicit")
    row.update(updates)
    return EquityHistorical.model_validate(row)


def _quote_contract(**updates: object) -> EquityQuote:
    row = _quote_row()
    row.update(updates)
    return EquityQuote.model_validate(row)


@dataclass
class FakeResponse:
    """Small response object implementing the fields consumed by the FMP client."""

    text: str
    status_code: int = 200
    headers: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"Content-Type": "application/json"}


class FakeHTTPClient:
    """Queued HTTP responses with captured governed-client calls."""

    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        source: str,
    ) -> FakeResponse:
        self.calls.append(
            {"url": url, "params": dict(params), "timeout": timeout, "source": source}
        )
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _response(rows: object, *, status: int = 200) -> FakeResponse:
    return FakeResponse(json.dumps(rows), status_code=status)


def _install_http(
    monkeypatch: pytest.MonkeyPatch,
    *responses: FakeResponse | BaseException,
) -> FakeHTTPClient:
    client = FakeHTTPClient(list(responses))
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: client)
    monkeypatch.setenv("FMP_API_KEY", TEST_API_KEY)
    return client


def test_historical_maps_every_stable_field_and_keeps_source_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_http(monkeypatch, _response([_historical_row()]))

    rows = EquityHistoricalFetcher().fetch(
        ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
    )

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, EquityHistorical)
    assert row.open == 100.0
    assert row.high == 103.0
    assert row.low == 99.5
    assert row.close == 102.0
    assert row.volume == 1_234_567
    assert row.change == 2.0
    assert row.change_percent == 2.0
    assert row.vwap == 101.4
    assert row.currency is None
    assert row.currency_semantics == "source_unverified"
    assert row.volume_unit is None
    assert row.volume_unit_semantics == "source_unverified"
    assert row.query_window_scope == "provider_default_unknown"
    assert row.window_boundary_semantics == "source_unverified"
    assert row.provider_default_window_semantics == "source_unverified"
    assert row.close_adjustment_semantics == "split_adjusted_per_source_faq"
    assert row.adj_close_provided is False
    assert not hasattr(row, "adj_close")
    assert (
        client.calls[0]["url"]
        == "https://financialmodelingprep.com/stable/historical-price-eod/full"
    )
    assert client.calls[0]["params"] == {"symbol": "AAPL", "apikey": TEST_API_KEY}
    assert client.calls[0]["source"] == "fmp"
    registry = ProviderRegistry()
    register_provider("fmp", registry)
    routed_capability = registry.resolve_model("fmp", "EquityHistorical").capability
    assert (
        routed_capability.asset_class,
        routed_capability.domain,
        routed_capability.period,
        routed_capability.market,
        routed_capability.source,
    ) == ("stock", "equity_historical", "1d", "us", "fmp")
    assert EquityHistoricalFetcher.capability.verified is False


def test_historical_optional_fields_may_be_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _historical_row()
    for field in ("volume", "change", "changePercent", "vwap"):
        source.pop(field)
    _install_http(monkeypatch, _response([source]))

    row = EquityHistoricalFetcher().fetch(
        ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
    )[0]

    assert row.volume is None
    assert row.change is None
    assert row.change_percent is None
    assert row.vwap is None


def test_historical_contract_roundtrips_full_and_nullable_frames() -> None:
    full = _historical_contract()
    nullable = _historical_contract(volume=None, change=None, changePercent=None, vwap=None)

    full_frame = EquityHistorical.to_frame([full])
    nullable_frame = EquityHistorical.to_frame([nullable])

    assert EquityHistorical.from_frame(full_frame) == [full]
    assert EquityHistorical.from_frame(nullable_frame) == [nullable]
    assert list(EquityHistorical.to_frame([]).columns) == list(EquityHistorical.model_fields)
    assert EquityHistorical.from_frame(EquityHistorical.to_frame([])) == []


def test_quote_contract_roundtrips_full_and_nullable_frames() -> None:
    full = _quote_contract()
    nullable = EquityQuote.model_validate({"symbol": "AAPL", "price": 102.0})

    full_frame = EquityQuote.to_frame([full])
    nullable_frame = EquityQuote.to_frame([nullable])

    assert EquityQuote.from_frame(full_frame) == [full]
    assert EquityQuote.from_frame(nullable_frame) == [nullable]
    assert list(EquityQuote.to_frame([]).columns) == list(EquityQuote.model_fields)
    assert EquityQuote.from_frame(EquityQuote.to_frame([])) == []


def test_quote_mixed_timestamp_frame_preserves_large_strict_integer() -> None:
    large_timestamp = 2**53 + 1
    rows = [
        _quote_contract(timestamp=large_timestamp),
        EquityQuote.model_validate({"symbol": "MSFT", "price": 200.0, "timestamp": None}),
    ]

    frame = EquityQuote.to_frame(rows)

    assert frame["provider_timestamp"].dtype == object
    assert frame["provider_timestamp"].iloc[0] == large_timestamp
    assert type(frame["provider_timestamp"].iloc[0]) is int
    assert frame["provider_timestamp"].iloc[1] is None
    assert EquityQuote.from_frame(frame) == rows


def test_quote_mixed_volume_frame_preserves_large_integer_float_and_none() -> None:
    large_volume = 2**53 + 1
    rows = [
        _quote_contract(volume=large_volume),
        _quote_contract(volume=None),
        _quote_contract(volume=1.5),
    ]

    frame = EquityQuote.to_frame(rows)

    assert frame["volume"].dtype == object
    assert frame["volume"].iloc[0] == large_volume
    assert type(frame["volume"].iloc[0]) is int
    assert frame["volume"].iloc[1] is None
    assert frame["volume"].iloc[2] == 1.5
    assert type(frame["volume"].iloc[2]) is float
    assert EquityQuote.from_frame(frame) == rows


def test_historical_mixed_volume_frame_preserves_large_integer() -> None:
    large_volume = 2**53 + 1
    rows = [
        _historical_contract(volume=large_volume),
        _historical_contract(date="2026-01-03", volume=None),
    ]

    frame = EquityHistorical.to_frame(rows)

    assert frame["volume"].dtype == object
    assert frame["volume"].iloc[0] == large_volume
    assert type(frame["volume"].iloc[0]) is int
    assert frame["volume"].iloc[1] is None
    assert EquityHistorical.from_frame(frame) == rows


def test_contract_models_accept_source_aliases_and_reject_unknown_columns() -> None:
    historical = EquityHistorical.model_validate(
        {**_historical_row(), "query_window_scope": "explicit"}
    )
    quote = EquityQuote.model_validate(_quote_row())

    assert historical.change_percent == 2.0
    assert quote.change_percentage == 2.0
    historical_canonical = _historical_row()
    historical_canonical.pop("changePercent")
    historical_canonical["change_percent"] = 3.0
    assert (
        EquityHistorical.model_validate(
            {**historical_canonical, "query_window_scope": "explicit"}
        ).change_percent
        == 3.0
    )
    quote_canonical = _quote_row()
    quote_canonical.pop("changePercentage")
    quote_canonical["change_percentage"] = 3.0
    assert EquityQuote.model_validate(quote_canonical).change_percentage == 3.0

    with pytest.raises(ValidationError, match="extra_forbidden"):
        EquityHistorical.model_validate(
            {
                **_historical_row(),
                "query_window_scope": "explicit",
                "unknown_column": "reject me",
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        EquityQuote.model_validate({**_quote_row(), "unknown_column": "reject me"})
    with pytest.raises(ValidationError):
        EquityQuote.model_validate({"symbol": "AAPL", "price": 1.0, "timestamp": 1.0})


def test_historical_missing_mandatory_field_is_bad_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _historical_row()
    source.pop("close")
    _install_http(monkeypatch, _response([source]))

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_historical_rejects_non_finite_prices(value: float) -> None:
    with pytest.raises(ValidationError):
        EquityHistorical.model_validate(
            {
                **_historical_row(open=value),
                "query_window_scope": "explicit",
            }
        )


def test_historical_rejects_integer_that_overflows_float_validation() -> None:
    with pytest.raises(ValidationError, match="source numeric field must be finite"):
        EquityHistorical.model_validate(
            {
                **_historical_row(open=10**1000),
                "query_window_scope": "explicit",
            }
        )


def test_historical_rejects_wrong_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _response([_historical_row(symbol="MSFT")]))

    with pytest.raises(FMPProviderError, match="FMP_WRONG_SYMBOL"):
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
        )


def test_historical_recursively_retries_a_5000_row_window_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = _historical_row(date="2026-01-01")
    left_rows = [
        _historical_row(date="2026-01-01"),
        _historical_row(date="2026-01-02"),
    ]
    right_rows = [
        _historical_row(date="2026-01-02"),
        _historical_row(date="2026-01-03"),
        _historical_row(date="2026-01-04"),
    ]
    client = _install_http(
        monkeypatch,
        _response([full] * 5_000),
        _response(left_rows),
        _response(right_rows),
    )

    rows = EquityHistoricalFetcher().fetch(
        ctx=offline_fixture_context("fmp", "EquityHistorical"),
        symbol="AAPL",
        start_date="2026-01-01",
        end_date="2026-01-04",
    )

    assert [row.date.isoformat() for row in rows] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
        "2026-01-04",
    ]
    assert all(row.query_window_scope == "explicit" for row in rows)
    assert len(client.calls) == 3
    assert client.calls[0]["params"] == {
        "symbol": "AAPL",
        "from": "2026-01-01",
        "to": "2026-01-04",
        "apikey": TEST_API_KEY,
    }
    assert client.calls[1]["params"]["from"] == "2026-01-01"
    assert client.calls[1]["params"]["to"] == "2026-01-02"
    assert client.calls[2]["params"]["from"] == "2026-01-02"
    assert client.calls[2]["params"]["to"] == "2026-01-04"


def test_historical_request_budget_returns_incomplete_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, _response([_historical_row()] * 5_000))

    with pytest.raises(FMPQueryLimitError, match="FMP_INCOMPLETE_REQUEST_BUDGET") as caught:
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"),
            symbol="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-04",
            max_requests=1,
        )

    assert caught.value.partial is True
    assert caught.value.requests_made == 1
    assert caught.value.limit_kind == "max_requests"


def test_historical_provider_cap_on_unknown_default_window_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, _response([_historical_row()] * 5_000))

    with pytest.raises(FMPQueryLimitError, match="FMP_INCOMPLETE_PROVIDER_LIMIT"):
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
        )


def test_historical_record_budget_does_not_return_partial_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, _response([_historical_row(), _historical_row(date="2026-01-03")]))

    with pytest.raises(FMPQueryLimitError, match="FMP_INCOMPLETE_RECORD_BUDGET") as caught:
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"),
            symbol="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-04",
            max_records=1,
        )

    assert caught.value.partial is True
    assert caught.value.limit_kind == "max_records"


def test_historical_conflicting_duplicate_date_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate_left = _historical_row(date="2026-01-02", close=100.0)
    duplicate_right = _historical_row(date="2026-01-02", close=101.0)
    _install_http(
        monkeypatch,
        _response([_historical_row()] * 5_000),
        _response([_historical_row(date="2026-01-01"), duplicate_left]),
        _response([duplicate_right, _historical_row(date="2026-01-03")]),
    )

    with pytest.raises(FMPProviderError, match="FMP_CONFLICTING_DUPLICATE_DATE"):
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"),
            symbol="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-03",
        )


def test_historical_out_of_window_date_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _response([_historical_row(date="2026-01-05")]))

    with pytest.raises(FMPProviderError, match="FMP_OUT_OF_WINDOW_ROW"):
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"),
            symbol="AAPL",
            start_date="2026-01-01",
            end_date="2026-01-04",
        )


def test_historical_legal_empty_response_is_empty_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _response([]))

    assert (
        EquityHistoricalFetcher().fetch(
            ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL"
        )
        == ()
    )


def test_symbol_is_required_nonempty_and_rejects_controls() -> None:
    fetcher = EquityHistoricalFetcher()

    for symbol in ("", " \t ", "AAPL\nBAD"):
        with pytest.raises(ValidationError):
            fetcher.transform_query(symbol=symbol)
    with pytest.raises(ValidationError):
        fetcher.transform_query()


def test_missing_key_fails_before_governed_http_io(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeHTTPClient([])
    monkeypatch.setattr(_client, "get_shared_http_client", lambda: client)
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    with pytest.raises(FMPProviderError, match="FMP_API_KEY_MISSING"):
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")

    assert client.calls == []


def test_transport_exception_and_cause_do_not_expose_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "never-print-this-key"
    client = _install_http(
        monkeypatch,
        RuntimeError(f"GET https://example.invalid/?apikey={secret} failed"),
    )

    with pytest.raises(FMPProviderError) as caught:
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")

    assert caught.value.code == "FMP_TRANSPORT_ERROR"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert client.calls[0]["params"]["apikey"] == TEST_API_KEY


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "FMP_AUTH_ERROR"),
        (403, "FMP_FORBIDDEN"),
        (429, "FMP_RATE_LIMITED"),
        (503, "FMP_UPSTREAM_ERROR"),
    ],
)
def test_http_statuses_have_stable_classification(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    _install_http(monkeypatch, _response({"message": "provider error"}, status=status))

    with pytest.raises(FMPProviderError) as caught:
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")

    assert caught.value.code == code
    assert caught.value.status == status


@pytest.mark.parametrize(
    ("body", "content_type", "code"),
    [
        ("<html><body>gateway</body></html>", "text/html", "FMP_HTML_RESPONSE"),
        ('[{"symbol":"AAPL"', "application/json", "FMP_TRUNCATED_RESPONSE"),
        ("not-json", "application/json", "FMP_BAD_RESPONSE"),
        ('{"Error Message":"invalid key"}', "application/json", "FMP_API_ERROR"),
    ],
)
def test_html_truncated_malformed_and_api_errors_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    content_type: str,
    code: str,
) -> None:
    client = _install_http(
        monkeypatch,
        FakeResponse(body, headers={"Content-Type": content_type}),
    )

    with pytest.raises(FMPProviderError) as caught:
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")

    assert caught.value.code == code
    assert client.calls


def test_quote_maps_all_stable_fields_and_keeps_provider_time_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_http(monkeypatch, _response([_quote_row()]))

    rows = EquityQuoteFetcher().fetch(
        ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL"
    )

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, EquityQuote)
    assert row.name == "Apple Inc."
    assert row.price == 102.0
    assert row.change_percentage == 2.0
    assert row.change == 2.0
    assert row.volume == 1_234_567
    assert row.day_low == 99.5
    assert row.day_high == 103.0
    assert row.year_low == 70.0
    assert row.year_high == 110.0
    assert row.market_cap == 3_000_000_000_000
    assert row.price_avg_50 == 100.0
    assert row.price_avg_200 == 95.0
    assert row.exchange == "NASDAQ"
    assert row.open == 100.0
    assert row.previous_close == 100.0
    assert row.provider_timestamp == 1_767_355_200
    assert row.timestamp_unit is None
    assert row.timestamp_timezone is None
    assert row.timestamp_semantics == "source_unverified"
    assert not hasattr(row, "as_of")
    assert client.calls[0]["url"] == "https://financialmodelingprep.com/stable/quote"
    registry = ProviderRegistry()
    register_provider("fmp", registry)
    routed_capability = registry.resolve_model("fmp", "EquityQuote").capability
    assert (
        routed_capability.asset_class,
        routed_capability.domain,
        routed_capability.period,
        routed_capability.market,
        routed_capability.source,
    ) == ("stock", "equity_quote", "snapshot", "us", "fmp")
    assert EquityQuoteFetcher.capability.verified is False


def test_quote_optional_fields_may_be_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _response([{"symbol": "AAPL", "price": 102.0}]))

    row = EquityQuoteFetcher().fetch(
        ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL"
    )[0]

    assert row.name is None
    assert row.change_percentage is None
    assert row.provider_timestamp is None
    assert row.currency is None
    assert row.volume_unit is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_quote_rejects_non_finite_price(value: float) -> None:
    with pytest.raises(ValidationError):
        EquityQuote.model_validate({"symbol": "AAPL", "price": value})


def test_quote_rejects_integer_that_overflows_float_validation() -> None:
    with pytest.raises(ValidationError, match="source numeric field must be finite"):
        EquityQuote.model_validate({"symbol": "AAPL", "price": 10**1000})


def test_quote_requires_price_and_rejects_wrong_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, _response([{"symbol": "AAPL"}]))
    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")

    _install_http(monkeypatch, _response([{"symbol": "MSFT", "price": 10.0}]))
    with pytest.raises(FMPProviderError, match="FMP_WRONG_SYMBOL"):
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")


def test_quote_accepts_empty_and_rejects_multi_symbol_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_http(monkeypatch, _response([]))
    assert (
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")
        == ()
    )

    _install_http(monkeypatch, _response([_quote_row(), _quote_row(symbol="MSFT")]))
    with pytest.raises(FMPProviderError, match="FMP_QUOTE_EXPECTED_ONE_ROW"):
        EquityQuoteFetcher().fetch(ctx=offline_fixture_context("fmp", "EquityQuote"), symbol="AAPL")


def test_sync_and_bounded_async_fetch_return_the_same_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _response([_historical_row()])
    client = _install_http(monkeypatch, response, response)
    fetcher = EquityHistoricalFetcher()

    sync_rows = fetcher.fetch(ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL")
    async_rows = asyncio.run(
        fetcher.fetch_async(ctx=offline_fixture_context("fmp", "EquityHistorical"), symbol="AAPL")
    )

    assert sync_rows == async_rows
    assert len(client.calls) == 2
    assert fetcher.async_mode == "bounded_thread"
