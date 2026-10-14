"""Offline contract and transport tests for the Alpha Vantage Iteration 2 slice."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError

from opendata.data.models.alpha_vantage import (
    AlphaVantageEquityHistorical,
    AlphaVantageEtfHistorical,
    AlphaVantageHistoricalEps,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.alpha_vantage.client import (
    AlphaVantageClient,
    AlphaVantageMissingKeyError,
    AlphaVantageRateLimitError,
    AlphaVantageResponseError,
)
from opendata.data.providers.alpha_vantage.models.equity_historical import (
    AlphaVantageEquityHistoricalFetcher,
)
from opendata.data.providers.alpha_vantage.models.etf_historical import (
    AlphaVantageEtfHistoricalFetcher,
)
from opendata.data.providers.alpha_vantage.models.historical_eps import (
    AlphaVantageHistoricalEpsFetcher,
)
from opendata.data.providers.alpha_vantage.provider import PROVIDER
from opendata.data.raw_response_cache import RawResponseCache
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

FIXTURES = Path(__file__).parent / "fixtures" / "alpha_vantage"


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)


class _Transport:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float | tuple[float, float] | None,
        source: str,
        cache_context: dict[str, str],
    ) -> _Response:
        self.requests.append(
            {
                "url": url,
                "params": dict(params),
                "timeout": timeout,
                "source": source,
                "cache_context": dict(cache_context),
            }
        )
        if not self.responses:
            raise AssertionError("unexpected extra transport request")
        return self.responses.pop(0)


def _fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(
    payload: object, *, status_code: int = 200, key: str = "fixture-key"
) -> tuple[AlphaVantageClient, _Transport]:
    transport = _Transport(_Response(payload, status_code))
    return AlphaVantageClient(api_key=key, http_client=transport), transport


def _context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="alpha_vantage",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=frozenset({"www.alphavantage.co"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    budget = RequestBudget(task_attempts=attempts, source_attempts=attempts, grants=(grant,))
    return FetchContext(timeout=2.0, request_budget=budget)


def test_provider_has_three_explicit_canonical_bindings() -> None:
    assert tuple(PROVIDER.fetcher_dict) == ("EquityHistorical", "EtfHistorical", "HistoricalEps")
    assert PROVIDER.source == "alpha_vantage"
    assert [fetcher.capability.domain for fetcher in PROVIDER.fetchers] == [
        "alpha_vantage_equity_historical",
        "alpha_vantage_etf_historical",
        "alpha_vantage_historical_eps",
    ]


@pytest.mark.parametrize(
    ("model", "fetcher", "contract", "query", "fixture", "expected"),
    [
        (
            "EquityHistorical",
            AlphaVantageEquityHistoricalFetcher,
            AlphaVantageEquityHistorical,
            {
                "symbol": "IBM",
                "interval": "1d",
                "adjustment": "splits_and_dividends",
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
            },
            "equity_daily_adjusted.json",
            [
                {
                    "symbol": "IBM",
                    "date": "2024-01-02",
                    "open": 158.0,
                    "high": 161.0,
                    "low": 157.0,
                    "close": 160.0,
                    "volume": 1111111,
                    "vwap": None,
                    "adj_close": 158.51,
                    "dividend": 0.0,
                    "split_ratio": 1.0,
                }
            ],
        ),
        (
            "EtfHistorical",
            AlphaVantageEtfHistoricalFetcher,
            AlphaVantageEtfHistorical,
            {
                "symbol": "SPY",
                "interval": "5m",
                "adjustment": "unadjusted",
                "start_date": "2024-01-02",
                "end_date": "2024-01-02",
            },
            "etf_intraday.json",
            [
                {
                    "symbol": "SPY",
                    "date": "2024-01-02T09:30:00",
                    "open": 472.0,
                    "high": 472.7,
                    "low": 471.9,
                    "close": 472.5,
                    "volume": 456700,
                    "vwap": None,
                    "adj_close": None,
                    "dividend": None,
                    "split_ratio": None,
                },
                {
                    "symbol": "SPY",
                    "date": "2024-01-02T09:35:00",
                    "open": 472.5,
                    "high": 473.0,
                    "low": 472.25,
                    "close": 472.8,
                    "volume": 123400,
                    "vwap": None,
                    "adj_close": None,
                    "dividend": None,
                    "split_ratio": None,
                },
                {
                    "symbol": "SPY",
                    "date": "2024-01-02T09:40:00",
                    "open": 472.8,
                    "high": 473.1,
                    "low": 472.6,
                    "close": 473.0,
                    "volume": 98500,
                    "vwap": None,
                    "adj_close": None,
                    "dividend": None,
                    "split_ratio": None,
                },
            ],
        ),
        (
            "HistoricalEps",
            AlphaVantageHistoricalEpsFetcher,
            AlphaVantageHistoricalEps,
            {"symbol": "IBM", "period": "quarter", "limit": 1},
            "historical_eps.json",
            [
                {
                    "symbol": "IBM",
                    "date": "2024-03-31",
                    "reported_date": "2024-04-24",
                    "eps_actual": 1.68,
                    "eps_estimated": 1.6,
                    "surprise": 0.08,
                    "surprise_percent": 0.05,
                    "report_time": "post-market",
                }
            ],
        ),
    ],
)
def test_provider_model_query_injects_identity_and_serializes_strict_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher: type,
    contract: type,
    query: dict[str, object],
    fixture: str,
    expected: list[dict[str, object]],
) -> None:
    instance = fetcher(
        client=AlphaVantageClient(
            api_key="fixture-key",
            http_client=_Transport(_Response(_fixture(fixture))),
        )
    )
    capability = instance.capability
    descriptor = SimpleNamespace(
        source=capability.source,
        model=model,
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
        lambda registry, source, requested_model: (descriptor, instance),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda domain: (None, contract),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="alpha_vantage",
            model=model,
            query=query,
            principal=Principal(),
            ctx=_context(model),
        )
    )

    assert response["source"] == "alpha_vantage"
    assert response["model"] == model
    assert response["domain"] == capability.domain
    assert response["results"] == expected


def test_provider_model_query_maps_invalid_query_after_identity_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = AlphaVantageEtfHistoricalFetcher(
        client=AlphaVantageClient(api_key="fixture-key", http_client=_Transport())
    )
    capability = instance.capability
    descriptor = SimpleNamespace(
        source=capability.source,
        model="EtfHistorical",
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
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, instance)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda domain: (None, AlphaVantageEtfHistorical),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    with pytest.raises(provider_model_query.ProviderModelQueryValidationError):
        asyncio.run(
            provider_model_query.query_provider_model(
                registry=object(),
                source="alpha_vantage",
                model="EtfHistorical",
                query={"symbol": "SPY", "interval": "2m", "adjustment": "unadjusted"},
                principal=Principal(),
                ctx=_context("EtfHistorical"),
            )
        )


def test_equity_historical_positive_contract_and_date_filter() -> None:
    client, transport = _client(_fixture("equity_daily_adjusted.json"))
    fetcher = AlphaVantageEquityHistoricalFetcher(client=client)

    rows = fetcher.fetch(
        ctx=_context("EquityHistorical"),
        source="alpha_vantage",
        symbol="IBM",
        interval="1d",
        adjustment="splits_and_dividends",
        start_date="2024-01-03",
        end_date="2024-01-03",
    )

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, AlphaVantageEquityHistorical)
    assert row.model_dump() == {
        "symbol": "IBM",
        "date": row.date,
        "open": 160.0,
        "high": 162.0,
        "low": 159.0,
        "close": 161.0,
        "volume": 1234567,
        "vwap": None,
        "adj_close": 159.5,
        "dividend": 0.0,
        "split_ratio": 1.0,
    }
    assert transport.requests[0]["params"] == {
        "symbol": "IBM",
        "datatype": "json",
        "function": "TIME_SERIES_DAILY_ADJUSTED",
        "outputsize": "full",
        "apikey": "fixture-key",
    }
    assert "apikey" not in transport.requests[0]["cache_context"]
    assert "page" not in transport.requests[0]["params"]


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"Meta Data": {}, "Wrong Series": {}},
        {
            "Time Series (Daily)": {
                "2024-01-03": {
                    "1. open": "NaN",
                    "2. high": "162.0",
                    "3. low": "159.0",
                    "4. close": "161.0",
                    "5. adjusted close": "159.5",
                    "6. volume": "123",
                    "7. dividend amount": "0.0",
                    "8. split coefficient": "1.0",
                }
            }
        },
        {
            "Time Series (Daily)": {
                "2024-01-03": {
                    "1. open": "160.0",
                    "2. high": "162.0",
                    "3. low": "159.0",
                    "4. close": "161.0",
                    "5. adjusted close": "159.5",
                    "6. volume": "123",
                    "7. dividend amount": "0.0",
                    "8. split coefficient": "1.0",
                    "unexpected": "value",
                }
            }
        },
    ],
)
def test_equity_historical_rejects_wrong_keys_and_values(bad_payload: object) -> None:
    client, _ = _client(bad_payload)
    with pytest.raises(AlphaVantageResponseError):
        AlphaVantageEquityHistoricalFetcher(client=client).fetch(
            ctx=_context("EquityHistorical"),
            symbol="IBM",
            adjustment="splits_and_dividends",
        )


def test_equity_historical_rejects_unsupported_adjustment_and_invalid_window() -> None:
    client, transport = _client(_fixture("equity_daily_adjusted.json"))
    fetcher = AlphaVantageEquityHistoricalFetcher(client=client)
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="IBM", adjustment="splits_only")
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="IBM")
    with pytest.raises(ValidationError):
        fetcher.fetch(
            symbol="IBM",
            adjustment="unadjusted",
            start_date="2024-02-01",
            end_date="2024-01-01",
        )
    assert transport.requests == []


def test_equity_historical_limit_and_missing_key_are_errors() -> None:
    client, _ = _client({"Information": "API call frequency exceeded"})
    with pytest.raises(AlphaVantageRateLimitError):
        AlphaVantageEquityHistoricalFetcher(client=client).fetch(
            ctx=_context("EquityHistorical"), symbol="IBM", adjustment="unadjusted"
        )

    transport = _Transport(_Response(_fixture("equity_daily_adjusted.json")))
    no_key = AlphaVantageClient(api_key=" ", http_client=transport)
    with pytest.raises(AlphaVantageMissingKeyError):
        AlphaVantageEquityHistoricalFetcher(client=no_key).fetch(
            ctx=_context("EquityHistorical"), symbol="IBM", adjustment="unadjusted"
        )
    assert transport.requests == []


def test_etf_historical_positive_intraday_window_fans_out_by_month() -> None:
    first_month = _fixture("etf_intraday.json")
    second_month = {
        "Meta Data": {
            "1. Information": "Intraday (5min) open, high, low, close prices and volume",
            "2. Symbol": "SPY",
            "3. Last Refreshed": "2024-02-01 09:30:00",
            "4. Interval": "5min",
            "5. Output Size": "Full size",
            "6. Time Zone": "US/Eastern",
        },
        "Time Series (5min)": {
            "2024-02-01 09:30:00": {
                "1. open": "480.00",
                "2. high": "480.20",
                "3. low": "479.90",
                "4. close": "480.10",
                "5. volume": "300000",
            }
        },
    }
    transport = _Transport(_Response(first_month), _Response(second_month))
    fetcher = AlphaVantageEtfHistoricalFetcher(
        client=AlphaVantageClient(api_key="fixture-key", http_client=transport)
    )

    rows = fetcher.fetch(
        ctx=_context("EtfHistorical"),
        symbol="SPY",
        interval="5m",
        adjustment="unadjusted",
        start_date="2024-01-02",
        end_date="2024-02-01",
    )

    assert len(rows) == 4
    assert all(isinstance(row, AlphaVantageEtfHistorical) for row in rows)
    assert [row.date.strftime("%Y-%m-%d %H:%M:%S") for row in rows] == [
        "2024-01-02 09:30:00",
        "2024-01-02 09:35:00",
        "2024-01-02 09:40:00",
        "2024-02-01 09:30:00",
    ]
    assert [request["params"]["month"] for request in transport.requests] == ["2024-01", "2024-02"]
    assert all(request["params"]["outputsize"] == "full" for request in transport.requests)
    assert all("page" not in request["params"] for request in transport.requests)


@pytest.mark.parametrize(
    ("model", "fetcher_type", "fixture", "query"),
    [
        (
            "EquityHistorical",
            AlphaVantageEquityHistoricalFetcher,
            "equity_daily_adjusted.json",
            {"symbol": "IBM", "interval": "1d", "adjustment": "splits_and_dividends"},
        ),
        (
            "EtfHistorical",
            AlphaVantageEtfHistoricalFetcher,
            "etf_intraday.json",
            {"symbol": "SPY", "interval": "5m", "adjustment": "unadjusted"},
        ),
    ],
)
def test_price_history_rejects_response_for_a_different_symbol(
    model: str,
    fetcher_type: type[Any],
    fixture: str,
    query: dict[str, str],
) -> None:
    payload = _fixture(fixture)
    assert isinstance(payload, dict)
    metadata = payload["Meta Data"]
    assert isinstance(metadata, dict)
    metadata["2. Symbol"] = "MSFT"
    client, transport = _client(payload)

    with pytest.raises(AlphaVantageResponseError, match="symbol"):
        fetcher_type(client=client).fetch(ctx=_context(model), **query)

    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("model", "fetcher_type", "fixture", "query", "requested_symbol", "response_symbol"),
    [
        (
            "EquityHistorical",
            AlphaVantageEquityHistoricalFetcher,
            "equity_daily_adjusted.json",
            {"interval": "1d", "adjustment": "splits_and_dividends"},
            "ibm",
            "IBM",
        ),
        (
            "EquityHistorical",
            AlphaVantageEquityHistoricalFetcher,
            "equity_daily_adjusted.json",
            {"interval": "1d", "adjustment": "splits_and_dividends"},
            "TSCO.LON",
            "TSCO.LON",
        ),
        (
            "EtfHistorical",
            AlphaVantageEtfHistoricalFetcher,
            "etf_intraday.json",
            {"interval": "5m", "adjustment": "unadjusted"},
            "spy",
            "SPY",
        ),
    ],
)
def test_price_history_accepts_casefolded_or_official_symbol_format(
    model: str,
    fetcher_type: type[Any],
    fixture: str,
    query: dict[str, str],
    requested_symbol: str,
    response_symbol: str,
) -> None:
    payload = _fixture(fixture)
    assert isinstance(payload, dict)
    metadata = payload["Meta Data"]
    assert isinstance(metadata, dict)
    metadata["2. Symbol"] = response_symbol
    client, _ = _client(payload)

    rows = fetcher_type(client=client).fetch(ctx=_context(model), symbol=requested_symbol, **query)

    assert rows
    assert all(row.symbol == response_symbol for row in rows)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {"2. Symbol": "IBM"},
        {"2. Symbol": "IBM", "5. Time Zone": " "},
        {"2. Symbol": "IBM", "5. Time Zone": "not a timezone"},
    ],
)
def test_price_history_rejects_missing_or_malformed_identity_metadata(
    metadata: dict[str, str] | None,
) -> None:
    payload = _fixture("equity_daily_adjusted.json")
    assert isinstance(payload, dict)
    if metadata is None:
        payload.pop("Meta Data")
    else:
        payload["Meta Data"] = metadata
    client, _ = _client(payload)

    with pytest.raises(AlphaVantageResponseError):
        AlphaVantageEquityHistoricalFetcher(client=client).fetch(
            ctx=_context("EquityHistorical"),
            symbol="IBM",
            interval="1d",
            adjustment="splits_and_dividends",
        )


def test_etf_intraday_rejects_time_zone_changes_across_months() -> None:
    first_month = _fixture("etf_intraday.json")
    second_month = {
        "Meta Data": {
            "1. Information": "Intraday (5min) open, high, low, close prices and volume",
            "2. Symbol": "SPY",
            "3. Last Refreshed": "2024-02-01 09:30:00",
            "4. Interval": "5min",
            "5. Output Size": "Full size",
            "6. Time Zone": "Europe/London",
        },
        "Time Series (5min)": {},
    }
    transport = _Transport(_Response(first_month), _Response(second_month))
    fetcher = AlphaVantageEtfHistoricalFetcher(
        client=AlphaVantageClient(api_key="fixture-key", http_client=transport)
    )

    with pytest.raises(AlphaVantageResponseError, match="time zone changed"):
        fetcher.fetch(
            ctx=_context("EtfHistorical"),
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
            start_date="2024-01-02",
            end_date="2024-02-01",
        )


def test_shared_raw_cache_fingerprints_all_alpha_vantage_query_parameters(
    tmp_path: Path,
) -> None:
    cache = RawResponseCache(tmp_path, ttl_seconds=60)
    base_params = {
        "function": "TIME_SERIES_INTRADAY",
        "symbol": "IBM",
        "interval": "5min",
        "month": "2024-01",
        "outputsize": "full",
        "adjusted": "true",
        "extended_hours": "false",
        "apikey": "fixture-key",
    }
    context = {
        key: base_params[key] for key in ("function", "symbol", "interval", "month", "outputsize")
    }

    def cache_key(params: dict[str, str]) -> str:
        endpoint = "https://www.alphavantage.co/query?" + urlencode(params)
        return cache.key_for(source="alpha_vantage", endpoint=endpoint, context=context)

    adjusted_key = cache_key(base_params)
    raw_key = cache_key({**base_params, "adjusted": "false"})
    extended_hours_key = cache_key({**base_params, "extended_hours": "true"})

    assert adjusted_key != raw_key
    assert adjusted_key != extended_hours_key


@pytest.mark.parametrize(
    ("payload", "exception"),
    [
        ({"Time Series (1min)": {}}, AlphaVantageResponseError),
        (
            {"Note": "API call frequency exceeded"},
            AlphaVantageRateLimitError,
        ),
    ],
)
def test_etf_historical_rejects_bad_keys_and_provider_limit(
    payload: object,
    exception: type[Exception],
) -> None:
    client, _ = _client(payload)
    with pytest.raises(exception):
        AlphaVantageEtfHistoricalFetcher(client=client).fetch(
            ctx=_context("EtfHistorical"),
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
        )


def test_etf_historical_rejects_invalid_source_values_and_time_ranges() -> None:
    invalid_row = {
        "Time Series (5min)": {
            "2024-01-02 09:30:00": {
                "1. open": "1.0",
                "2. high": "2.0",
                "3. low": "0.5",
                "4. close": "1.5",
                "5. volume": "12.5",
            }
        }
    }
    client, _ = _client(invalid_row)
    with pytest.raises(AlphaVantageResponseError):
        AlphaVantageEtfHistoricalFetcher(client=client).fetch(
            ctx=_context("EtfHistorical"),
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
        )

    client, transport = _client(_fixture("etf_intraday.json"))
    fetcher = AlphaVantageEtfHistoricalFetcher(client=client)
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="SPY", interval="2m", adjustment="unadjusted")
    with pytest.raises(ValidationError):
        fetcher.fetch(
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
            start_date="1999-12-31",
        )
    with pytest.raises(ValidationError):
        fetcher.fetch(
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
            end_date="2024-01-02",
        )
    assert transport.requests == []


def test_etf_historical_http_429_is_rate_limit() -> None:
    client, _ = _client("quota", status_code=429)
    with pytest.raises(AlphaVantageRateLimitError):
        AlphaVantageEtfHistoricalFetcher(client=client).fetch(
            ctx=_context("EtfHistorical"),
            symbol="SPY",
            interval="5m",
            adjustment="unadjusted",
        )


def test_historical_eps_positive_period_filter_limit_and_percent_conversion() -> None:
    client, transport = _client(_fixture("historical_eps.json"))
    fetcher = AlphaVantageHistoricalEpsFetcher(client=client)

    rows = fetcher.fetch(
        ctx=_context("HistoricalEps"),
        symbol="IBM",
        period="quarter",
        start_date="2023-12-31",
        end_date="2024-03-31",
        limit=1,
    )

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, AlphaVantageHistoricalEps)
    assert row.date.isoformat() == "2024-03-31"
    assert row.reported_date is not None and row.reported_date.isoformat() == "2024-04-24"
    assert row.eps_actual == 1.68
    assert row.surprise_percent == 0.05
    assert transport.requests[0]["params"] == {
        "function": "EARNINGS",
        "symbol": "IBM",
        "datatype": "json",
        "apikey": "fixture-key",
    }
    assert "page" not in transport.requests[0]["params"]


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "IBM", "quarterly": []},
        {
            "symbol": "IBM",
            "quarterlyEarnings": [
                {
                    "fiscalDateEnding": "2024-03-31",
                    "reportedEPS": "1.0",
                    "unexpected": "key",
                }
            ],
            "annualEarnings": [],
        },
        {
            "symbol": "IBM",
            "quarterlyEarnings": [{"fiscalDateEnding": "2024-03-31", "reportedEPS": "NaN"}],
            "annualEarnings": [],
        },
    ],
)
def test_historical_eps_rejects_wrong_keys_and_values(payload: object) -> None:
    client, _ = _client(payload)
    with pytest.raises(AlphaVantageResponseError):
        AlphaVantageHistoricalEpsFetcher(client=client).fetch(
            ctx=_context("HistoricalEps"), symbol="IBM"
        )


def test_historical_eps_rejects_invalid_inputs_and_time_window() -> None:
    client, transport = _client(_fixture("historical_eps.json"))
    fetcher = AlphaVantageHistoricalEpsFetcher(client=client)
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="IBM", period="monthly")
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="IBM", limit=0)
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="IBM", start_date="2024-05-01", end_date="2024-04-01")
    assert transport.requests == []


def test_historical_eps_provider_information_and_missing_key_fail_closed() -> None:
    client, _ = _client({"Information": "API call frequency exceeded"})
    with pytest.raises(AlphaVantageRateLimitError):
        AlphaVantageHistoricalEpsFetcher(client=client).fetch(
            ctx=_context("HistoricalEps"), symbol="IBM"
        )

    transport = _Transport(_Response(_fixture("historical_eps.json")))
    client = AlphaVantageClient(api_key="", http_client=transport)
    with pytest.raises(AlphaVantageMissingKeyError):
        AlphaVantageHistoricalEpsFetcher(client=client).fetch(
            ctx=_context("HistoricalEps"), symbol="IBM"
        )
    assert transport.requests == []
