"""Offline tests for the bounded Intrinio market clean-room slice."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data.http_client import GovernedHttpClient
from opendata.data.models.intrinio_market_clean import (
    IntrinioEquityHistorical,
    IntrinioEquityInfo,
    IntrinioEquityQuote,
    IntrinioEtfHistorical,
    IntrinioEtfHolding,
    IntrinioEtfInfo,
    IntrinioEtfSearch,
    IntrinioHistoricalMarketCap,
    IntrinioIndexHistorical,
    IntrinioOptionsChain,
    IntrinioOptionsUnusual,
    IntrinioPriceTargetConsensus,
    IntrinioShareStatistics,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.intrinio.models import iter2_market_clean as market
from opendata.data.providers.intrinio.provider import PROVIDER
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)


class _JsonSession(requests.Session):
    """requests-compatible, offline transport that preserves governed-client behavior."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra transport request")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = "utf-8"
        response._content = json.dumps(self.responses.pop(0), allow_nan=False).encode("utf-8")
        return response


def _grant_context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="intrinio",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:governed-fake-transport",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=frozenset({market.INTRINIO_API_BASE.split("//", 1)[1]}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
        ),
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict[str, object]],
) -> _JsonSession:
    session = _JsonSession(responses)
    client = GovernedHttpClient(session=session)
    monkeypatch.setattr(market, "get_shared_http_client", lambda: client)
    return session


def _fetcher_rows() -> list[tuple[str, object, dict[str, object], object, type[object]]]:
    """Handwritten positive and malformed source records for every bound identity."""
    return [
        (
            "EquityInfo",
            market.IntrinioEquityInfoFetcher(),
            {"symbol": "AAPL"},
            {"ticker": "AAPL", "name": "Apple Inc.", "cik": "0000320193", "employees": 164000},
            IntrinioEquityInfo,
        ),
        (
            "EquityHistorical",
            market.IntrinioEquityHistoricalFetcher(),
            {"symbol": "AAPL", "start_date": date(2025, 1, 1), "end_date": date(2025, 1, 2)},
            [
                {
                    "date": "2025-01-02",
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 100,
                }
            ],
            IntrinioEquityHistorical,
        ),
        (
            "EtfInfo",
            market.IntrinioEtfInfoFetcher(),
            {"symbol": "SPY"},
            {
                "ticker": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "sponsor": "SPDR",
                "isin": "US78462F1030",
            },
            IntrinioEtfInfo,
        ),
        (
            "HistoricalMarketCap",
            market.IntrinioHistoricalMarketCapFetcher(),
            {"symbol": "AAPL", "start_date": date(2025, 1, 1), "interval": "day"},
            [{"date": "2025-01-02", "value": 3_000_000_000_000}],
            IntrinioHistoricalMarketCap,
        ),
        (
            "IndexHistorical",
            market.IntrinioIndexHistoricalFetcher(),
            {"symbol": "$DJI", "start_date": date(2025, 1, 1)},
            [{"date": "2025-01-02", "value": 42_000.0}],
            IntrinioIndexHistorical,
        ),
        (
            "EquityQuote",
            market.IntrinioEquityQuoteFetcher(),
            {"symbol": "AAPL", "quote_source": "iex"},
            {
                "security": {
                    "ticker": "AAPL",
                    "name": "Apple Inc.",
                    "exchange": "XNAS",
                    "currency": "USD",
                },
                "last": 225.0,
                "last_time": "2025-01-02T21:00:00Z",
                "source": "iex",
                "open": 220.0,
                "high": 226.0,
                "low": 219.0,
                "exchange_volume": 1000,
                "market_volume": 3000,
                "previous_close": 221.0,
                "change": 4.0,
                "change_percent": 1.81,
            },
            IntrinioEquityQuote,
        ),
        (
            "EtfHistorical",
            market.IntrinioEtfHistoricalFetcher(),
            {"symbol": "SPY", "interval": "1d"},
            [
                {
                    "date": "2025-01-02",
                    "open": 600.0,
                    "high": 605.0,
                    "low": 599.0,
                    "close": 604.0,
                    "volume": 1000000,
                }
            ],
            IntrinioEtfHistorical,
        ),
        (
            "EtfHoldings",
            market.IntrinioEtfHoldingsFetcher(),
            {"symbol": "SPY", "date": date(2025, 1, 2)},
            [
                {
                    "ticker": "AAPL",
                    "name": "Apple Inc.",
                    "type": "EQUI",
                    "as_of_date": "2025-01-02",
                    "quantity_held": 100,
                    "market_value_held": 22500.0,
                    "weighting": 0.015,
                }
            ],
            IntrinioEtfHolding,
        ),
        (
            "EtfSearch",
            market.IntrinioEtfSearchFetcher(),
            {"query": "SPDR"},
            [
                {
                    "id": "etf_1",
                    "ticker": "SPY",
                    "name": "SPDR S&P 500 ETF Trust",
                    "exchange_mic": "ARCX",
                }
            ],
            IntrinioEtfSearch,
        ),
        (
            "OptionsUnusual",
            market.IntrinioOptionsUnusualFetcher(),
            {"symbol": "AAPL", "option_source": "delayed"},
            [
                {
                    "contract": "AAPL__250117C00225000",
                    "symbol": "AAPL",
                    "timestamp": "2025-01-02T20:00:00Z",
                    "type": "large",
                    "sentiment": "bullish",
                    "bid_at_execution": 3.1,
                    "ask_at_execution": 3.2,
                    "average_price": 3.2,
                    "total_size": 100,
                    "total_value": 32000,
                }
            ],
            IntrinioOptionsUnusual,
        ),
        (
            "OptionsChains",
            market.IntrinioOptionsChainsFetcher(),
            {"symbol": "AAPL", "option_source": "delayed"},
            [
                {
                    "option": {
                        "code": "AAPL__250117C00225000",
                        "ticker": "AAPL",
                        "expiration": "2025-01-17",
                        "strike": 225.0,
                        "type": "call",
                    },
                    "price": {
                        "last": 3.2,
                        "last_size": 5,
                        "last_timestamp": "2025-01-02T20:00:00Z",
                        "volume": 100,
                        "open_interest": 1000,
                        "bid": 3.1,
                        "bid_size": 8,
                        "bid_timestamp": "2025-01-02T19:59:59Z",
                        "ask": 3.3,
                        "ask_size": 10,
                        "ask_timestamp": "2025-01-02T19:59:59Z",
                    },
                    "stats": {
                        "implied_volatility": 0.25,
                        "delta": 0.5,
                        "gamma": 0.01,
                        "theta": -0.02,
                        "vega": 0.1,
                        "underlying_price": 225.5,
                    },
                    "extended_price": {
                        "trade_open": 3.0,
                        "bid_open": 2.9,
                        "ask_open": 3.1,
                        "trade_high": 3.5,
                        "bid_high": 3.4,
                        "ask_high": 3.6,
                        "trade_low": 2.8,
                        "bid_low": 2.7,
                        "ask_low": 2.9,
                        "trade_close": 3.2,
                        "bid_close": 3.1,
                        "ask_close": 3.3,
                        "mark": 3.2,
                    },
                }
            ],
            IntrinioOptionsChain,
        ),
        (
            "PriceTargetConsensus",
            market.IntrinioPriceTargetConsensusFetcher(),
            {"symbol": "AAPL", "industry_group_number": 199},
            [
                {
                    "ticker": "AAPL",
                    "company_name": "Apple Inc.",
                    "high": 300.0,
                    "low": 200.0,
                    "mean": 250.0,
                    "median": 251.0,
                    "standard_deviation": 10.0,
                    "total": 25,
                    "most_recent_date": "2025-01-01",
                    "raised": 4,
                    "lowered": 3,
                    "industry_group_number": "199",
                }
            ],
            IntrinioPriceTargetConsensus,
        ),
        (
            "ShareStatistics",
            market.IntrinioShareStatisticsFetcher(),
            {"symbol": "AAPL", "start_date": date(2025, 1, 1)},
            [
                {
                    "trading_symbol": "AAPL",
                    "end_date": "2025-01-02",
                    "shares_outstanding": 15_000_000_000,
                    "adj_shares_outstanding": 15_000_000_000,
                }
            ],
            IntrinioShareStatistics,
        ),
    ]


@pytest.mark.parametrize(
    "model,fetcher,query,raw,contract",
    _fetcher_rows(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_handwritten_source_fixtures_map_to_strict_typed_contracts(
    model: str,
    fetcher: Any,
    query: dict[str, object],
    raw: object,
    contract: type[object],
) -> None:
    params = fetcher.transform_query(**query)
    rows = fetcher.transform_data(raw, params)
    assert len(rows) == 1
    assert type(rows[0]) is contract
    assert isinstance(rows[0].model_dump(mode="json"), dict)


@pytest.mark.parametrize(
    "model,fetcher,query,raw,contract",
    _fetcher_rows(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_handwritten_malformed_source_fixture_is_rejected(
    model: str,
    fetcher: Any,
    query: dict[str, object],
    raw: object,
    contract: type[object],
) -> None:
    params = fetcher.transform_query(**query)
    if model in {"EquityInfo", "EtfInfo", "EquityQuote"}:
        malformed: object = []
    elif model == "EtfHoldings":
        malformed = [{"ticker": "AAPL", "weighting": True}]
    elif model == "EtfSearch":
        malformed = [{"name": "missing ticker"}]
    elif model == "OptionsUnusual":
        malformed = [{"contract": "AAPL", "timestamp": "2025-01-02T20:00:00Z", "type": "mystery"}]
    elif model == "OptionsChains":
        malformed = [{"option": [], "price": None}]
    elif model == "ShareStatistics":
        malformed = [{"end_date": "2025-01-02", "shares_outstanding": 100}]
    else:
        malformed = [{"date": "not-a-date", "value": float("nan")}]
    with pytest.raises((ValidationError, ValueError, market.IntrinioResponseShapeError)):
        fetcher.transform_data(malformed, params)


@pytest.mark.parametrize(
    "model,fetcher,query,raw,contract",
    _fetcher_rows(),
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_invalid_identity_query_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher: Any,
    query: dict[str, object],
    raw: object,
    contract: type[object],
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [])
    bad_query = {"query": "   "} if model == "EtfSearch" else {"symbol": " \n "}
    with pytest.raises((ValidationError, ValueError)):
        fetcher.fetch(ctx=_grant_context(model), **bad_query)
    assert session.calls == []


def test_required_api_key_fails_before_governed_http(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = market.IntrinioEquityInfoFetcher()
    session = _install_transport(monkeypatch, [])
    monkeypatch.delenv("INTRINIO_API_KEY", raising=False)
    with pytest.raises(market.IntrinioMissingApiKeyError):
        fetcher.fetch(ctx=_grant_context("EquityInfo"), symbol="AAPL")
    assert session.calls == []


def test_realtime_options_chain_uses_paged_ticker_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(
        monkeypatch,
        [
            {
                "contracts": [
                    {
                        "option": {
                            "code": "AAPL__250117C00225000",
                            "ticker": "AAPL",
                            "expiration": "2025-01-17",
                            "strike": 225.0,
                            "type": "call",
                        },
                        "price": {
                            "last": 3.2,
                            "last_timestamp": "2025-01-02T20:00:00Z",
                            "volume": 100,
                            "open_interest": 1000,
                            "bid": 3.1,
                            "bid_size": 8,
                            "bid_timestamp": "2025-01-02T19:59:59Z",
                            "ask": 3.3,
                            "ask_size": 10,
                            "ask_timestamp": "2025-01-02T19:59:59Z",
                        },
                        "stats": {"implied_volatility": 0.25, "underlying_price": 225.5},
                    }
                ],
                "next_page": None,
            }
        ],
    )
    rows = market.IntrinioOptionsChainsFetcher().fetch(
        ctx=_grant_context("OptionsChains"),
        symbol="AAPL",
        option_source="delayed",
        option_type="call",
    )
    assert len(rows) == 1
    assert rows[0].contract_symbol == "AAPL__250117C00225000"
    assert rows[0].underlying_price == 225.5
    assert session.calls[0]["url"].endswith("/options/prices/by_ticker/AAPL/realtime")
    assert session.calls[0]["params"]["source"] == "delayed"
    assert session.calls[0]["params"]["type"] == "call"
    assert session.calls[0]["params"]["page_size"] == "100"


def test_options_chain_historical_date_rejected_before_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [])
    with pytest.raises(ValueError, match="historical option chains"):
        market.IntrinioOptionsChainsFetcher().fetch(
            ctx=_grant_context("OptionsChains"),
            symbol="AAPL",
            date=date(2025, 1, 2),
        )
    assert session.calls == []


def test_stock_price_cursor_pagination_is_finite_and_maps_both_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(
        monkeypatch,
        [
            {
                "stock_prices": [
                    {
                        "date": "2025-01-02",
                        "open": 1.0,
                        "high": 2.0,
                        "low": 0.5,
                        "close": 1.5,
                        "volume": 10,
                    }
                ],
                "next_page": "cursor-1",
            },
            {
                "stock_prices": [
                    {
                        "date": "2025-01-03",
                        "open": 1.5,
                        "high": 2.5,
                        "low": 1.0,
                        "close": 2.0,
                        "volume": 20,
                    }
                ],
                "next_page": None,
            },
        ],
    )
    fetcher = market.IntrinioEquityHistoricalFetcher()
    rows = fetcher.fetch(
        ctx=_grant_context("EquityHistorical", attempts=3),
        symbol="AAPL",
        interval="1d",
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 3),
    )
    assert len(rows) == 2
    assert rows[0].date == date(2025, 1, 2)
    assert rows[1].close == 2.0
    assert len(session.calls) == 2
    assert session.calls[0]["params"] == {
        "frequency": "daily",
        "sort_order": "asc",
        "start_date": "2025-01-02",
        "end_date": "2025-01-03",
        "page_size": "100",
    }
    assert session.calls[1]["params"]["next_page"] == "cursor-1"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer offline-test-key"


def test_cursor_pagination_refuses_missing_or_repeated_cursor_and_no_partial_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    repeated = _install_transport(
        monkeypatch,
        [
            {"stock_prices": [], "next_page": "same"},
            {"stock_prices": [], "next_page": "same"},
        ],
    )
    fetcher = market.IntrinioEquityHistoricalFetcher()
    with pytest.raises(market.IntrinioResponseShapeError, match="cursor repeated"):
        fetcher.fetch(ctx=_grant_context("EquityHistorical"), symbol="AAPL")
    assert len(repeated.calls) == 2

    missing = _install_transport(monkeypatch, [{"stock_prices": []}])
    with pytest.raises(market.IntrinioResponseShapeError, match="include next_page"):
        fetcher.fetch(ctx=_grant_context("EquityHistorical"), symbol="AAPL")
    assert len(missing.calls) == 1


def test_request_budget_exhaustion_does_not_return_truncated_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [{"stock_prices": [], "next_page": "cursor-1"}])
    fetcher = market.IntrinioEquityHistoricalFetcher()
    with pytest.raises(RequestAttemptLimitError):
        fetcher.fetch(ctx=_grant_context("EquityHistorical", attempts=1), symbol="AAPL")
    assert len(session.calls) == 1


def test_provider_model_query_injects_source_market_and_serializes_typed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.services import provider_model_query as service

    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(
        monkeypatch,
        [{"ticker": "AAPL", "name": "Apple Inc.", "employees": 164000}],
    )
    fetcher = market.IntrinioEquityInfoFetcher()
    capability = fetcher.capability
    identity = (
        capability.asset_class,
        capability.domain,
        capability.period,
        capability.market,
        capability.source,
    )
    descriptor = SimpleNamespace(
        source="intrinio",
        model="EquityInfo",
        domain=capability.domain,
        verified=False,
        full_capability_identity=identity,
    )
    monkeypatch.setattr(
        service, "_resolve_fetcher", lambda registry, source, model: (descriptor, fetcher)
    )
    monkeypatch.setattr(
        service,
        "_reviewed_query_contract",
        lambda domain: (object(), IntrinioEquityInfo),
    )
    seen: dict[str, object] = {}
    original = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    result = asyncio.run(
        service.query_provider_model(
            registry=object(),
            source="intrinio",
            model="EquityInfo",
            query={"symbol": "AAPL", "source": "auto"},
            principal=SimpleNamespace(allows_domain=lambda domain: domain == capability.domain),
            ctx=_grant_context("EquityInfo"),
        )
    )
    assert seen["source"] == "intrinio"
    assert seen["market"] == "us"
    assert result["results"] == [
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "cik": None,
            "lei": None,
            "legal_name": None,
            "stock_exchange": None,
            "sic": None,
            "short_description": None,
            "long_description": None,
            "ceo": None,
            "company_url": None,
            "business_address": None,
            "mailing_address": None,
            "business_phone_no": None,
            "hq_address1": None,
            "hq_address2": None,
            "hq_address_city": None,
            "hq_address_postal_code": None,
            "hq_state": None,
            "hq_country": None,
            "inc_state": None,
            "inc_country": None,
            "employees": 164000,
            "entity_legal_form": None,
            "entity_status": None,
            "latest_filing_date": None,
            "sector": None,
            "industry_category": None,
            "industry_group": None,
            "template": None,
            "standardized_active": None,
            "first_fundamental_date": None,
            "last_fundamental_date": None,
            "first_stock_price_date": None,
            "last_stock_price_date": None,
        }
    ]
    assert session.calls[0]["url"].endswith("/companies/AAPL")


def test_exact_endpoint_gap_bindings_match_documented_coverage() -> None:
    declared = {
        model for binding in PROVIDER.fetcher_bindings for model in binding.canonical_model_ids
    }
    assert set(market.GAP_MODELS) == {
        "EtfPricePerformance",
        "MarketSnapshots",
    }
    assert declared & set(market.GAP_MODELS) == {
        "EtfPricePerformance",
        "MarketSnapshots",
    }
    assert "OptionsSnapshots" in declared
    assert all(reason.strip() for reason in market.GAP_MODELS.values())
