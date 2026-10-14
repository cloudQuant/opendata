"""Offline FMP market tests against official Stable and OpenBB public shapes."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from opendata.data.models import FmpIndexConstituent, IndexConstituent
from opendata.data.models.fmp_market import FmpIndexConstituent as FmpMarketIndexConstituent
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models import iter2_market
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def test_fmp_index_constituent_contract_keeps_core_export_distinct() -> None:
    assert FmpIndexConstituent is FmpMarketIndexConstituent
    assert FmpIndexConstituent is not IndexConstituent


class _Response:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body, allow_nan=True)


class _HTTPClient:
    def __init__(self, by_route: dict[str, list[object]]) -> None:
        self.by_route = {route: list(bodies) for route, bodies in by_route.items()}
        self.calls: list[tuple[str, dict[str, str], str]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        source: str,
    ) -> _Response:
        assert timeout > 0
        assert source == "fmp"
        self.calls.append((url, dict(params), source))
        route = url.split("/stable/", 1)[1]
        queue = self.by_route.get(route)
        if not queue:
            raise AssertionError(f"unexpected FMP route: {route}")
        item = queue.pop(0)
        if isinstance(item, tuple):
            status, body = item
            return _Response(status, body)
        return _Response(200, item)


def _context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="fmp",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline FMP Stable fixture grant",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=2.0,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
        ),
    )


def _install_http(
    monkeypatch: pytest.MonkeyPatch, by_route: dict[str, list[object]]
) -> _HTTPClient:
    client = _HTTPClient(by_route)
    monkeypatch.setenv("FMP_API_KEY", "offline-test-key")
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    return client


def _profile() -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "price": 331.85501,
        "marketCap": 4_874_072_686_740,
        "beta": 1.097,
        "lastDividend": 1.05,
        "range": "201.5-344.57",
        "change": -6.33498,
        "changePercentage": -1.8732,
        "volume": 28_718_014,
        "averageVolume": 55_309_000,
        "companyName": "Apple Inc.",
        "currency": "USD",
        "cik": "0000320193",
        "isin": "US0378331005",
        "cusip": "037833100",
        "exchangeFullName": "NASDAQ Global Select",
        "exchange": "NASDAQ",
        "industry": "Consumer Electronics",
        "website": "https://www.apple.com",
        "description": "Apple Inc. designs and sells consumer electronics.",
        "ceo": "Timothy D. Cook",
        "sector": "Technology",
        "country": "US",
        "fullTimeEmployees": "166000",
        "phone": "(408) 996-1010",
        "address": "One Apple Park Way",
        "city": "Cupertino",
        "state": "CA",
        "zip": "95014",
        "image": "https://images.financialmodelingprep.com/symbol/AAPL.png",
        "ipoDate": "1980-12-12",
        "defaultImage": False,
        "isEtf": False,
        "isActivelyTrading": True,
        "isAdr": False,
        "isFund": False,
    }


def _bar(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "date": "2026-07-30",
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 104.0,
        "volume": 1000,
        "change": 4.0,
        "changePercent": 4.0,
        "vwap": 102.5,
    }


def _mover(symbol: str, *, change: float, percent: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "name": f"{symbol} Corporation",
        "price": 104.0,
        "change": change,
        "changesPercentage": percent,
        "volume": 120_000,
        "exchange": "NASDAQ",
    }


_CASES: list[tuple[str, type, dict[str, object], dict[str, object], str, dict[str, str]]] = [
    (
        "CryptoHistorical",
        iter2_market.CryptoHistoricalFetcher,
        {"symbol": "BTCUSD", "start_date": "2026-07-30", "end_date": "2026-07-30"},
        {
            "date": "2026-07-30",
            "open": 63902.9,
            "high": 65038.38,
            "low": 63547.793,
            "close": 64766.98828,
            "volume": 32030003200,
            "vwap": 64451.05,
            "change": 864.09,
            "change_percent": 1.35219,
        },
        "historical-price-eod/full",
        {"symbol": "BTCUSD", "from": "2026-07-30", "to": "2026-07-30"},
    ),
    (
        "CryptoSearch",
        iter2_market.CryptoSearchFetcher,
        {"query": "MIOTA"},
        {
            "symbol": "MIOTAUSD",
            "name": "IOTA USD",
            "exchange": "CCC",
            "ico_date": "2017-11-09",
            "circulating_supply": 4232705124,
            "total_supply": 4788606639,
        },
        "cryptocurrency-list",
        {},
    ),
    (
        "CurrencyHistorical",
        iter2_market.CurrencyHistoricalFetcher,
        {"symbol": "EUR-USD", "start_date": "2026-07-30", "end_date": "2026-07-30"},
        {
            "date": "2026-07-30",
            "open": 1.16,
            "high": 1.17,
            "low": 1.15,
            "close": 1.165,
            "volume": 0,
            "vwap": 1.1625,
            "change": 0.005,
            "change_percent": 0.43,
        },
        "historical-price-eod/full",
        {"symbol": "EURUSD", "from": "2026-07-30", "to": "2026-07-30"},
    ),
    (
        "CurrencyPairs",
        iter2_market.CurrencyPairsFetcher,
        {"query": "ARS"},
        {
            "symbol": "ARSMXN",
            "name": "Argentine Peso / Mexican Peso",
            "from_currency": "ARS",
            "to_currency": "MXN",
            "from_name": "Argentine Peso",
            "to_name": "Mexican Peso",
        },
        "forex-list",
        {},
    ),
    (
        "CurrencySnapshots",
        iter2_market.CurrencySnapshotsFetcher,
        {"base": "USD", "counter_currencies": ["EUR"]},
        {
            "base_currency": "USD",
            "counter_currency": "EUR",
            "last_rate": 1.0 / 1.1,
            "open": 1.0 / 1.0,
            "high": 1.0 / 0.9,
            "low": 1.0 / 1.2,
            "close": 1.0 / 1.1,
            "volume": 123,
            "prev_close": 1.0 / 1.05,
            "change": (1.0 / 1.1) - (1.0 / 1.05),
            "change_percent": ((1.0 / 1.1) - (1.0 / 1.05)) / (1.0 / 1.05) * 100,
            "ma50": 1.0 / 1.04,
            "ma200": 1.0 / 1.03,
            "year_high": 1.0 / 0.8,
            "year_low": 1.0 / 1.3,
            "last_rate_timestamp": "2023-11-14T22:13:20Z",
        },
        "quote",
        {"symbol": "EURUSD"},
    ),
    (
        "EquityActive",
        iter2_market.EquityActiveFetcher,
        {"limit": 10},
        {
            "symbol": "AAPL",
            "name": "AAPL Corporation",
            "price": 104.0,
            "change": 4.0,
            "percent_change": 4.0,
            "volume": 120000,
            "exchange": "NASDAQ",
        },
        "most-actives",
        {},
    ),
    (
        "EquityPeers",
        iter2_market.EquityPeersFetcher,
        {"symbol": "AAPL"},
        {
            "symbol": "GOOGL",
            "name": "Alphabet Inc.",
            "price": 333.84,
            "market_cap": 4_040_168_831_718,
        },
        "stock-peers",
        {"symbol": "AAPL"},
    ),
    (
        "EquityInfo",
        iter2_market.EquityInfoFetcher,
        {"symbol": "AAPL"},
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "cik": "0000320193",
            "cusip": "037833100",
            "isin": "US0378331005",
            "stock_exchange": "NASDAQ Global Select",
            "long_description": "Apple Inc. designs and sells consumer electronics.",
            "company_url": "https://www.apple.com",
            "ceo": "Timothy D. Cook",
            "sector": "Technology",
            "industry_category": "Consumer Electronics",
            "business_address": "One Apple Park Way",
            "business_phone_no": "(408) 996-1010",
            "hq_address_city": "Cupertino",
            "hq_state": "CA",
            "hq_address_postal_code": "95014",
            "hq_country": "US",
            "employees": 166000,
            "image": "https://images.financialmodelingprep.com/symbol/AAPL.png",
            "currency": "USD",
            "market_cap": 4_874_072_686_740,
            "last_price": 331.85501,
            "year_high": 344.57,
            "year_low": 201.5,
            "volume_avg": 55_309_000,
            "annualized_dividend_amount": None,
            "beta": 1.097,
            "is_etf": False,
            "is_actively_trading": True,
            "is_adr": False,
            "is_fund": False,
        },
        "profile",
        {"symbol": "AAPL"},
    ),
    (
        "EquityGainers",
        iter2_market.EquityGainersFetcher,
        {"limit": 5},
        {
            "symbol": "GAIN",
            "name": "GAIN Corporation",
            "price": 104.0,
            "change": 4.0,
            "percent_change": 4.0,
            "volume": 120000,
            "exchange": "NASDAQ",
        },
        "biggest-gainers",
        {},
    ),
    (
        "EquityLosers",
        iter2_market.EquityLosersFetcher,
        {"limit": 5},
        {
            "symbol": "LOSS",
            "name": "LOSS Corporation",
            "price": 104.0,
            "change": -4.0,
            "percent_change": -4.0,
            "volume": 120000,
            "exchange": "NASDAQ",
        },
        "biggest-losers",
        {},
    ),
    (
        "EquityScreener",
        iter2_market.EquityScreenerFetcher,
        {"sector": "Technology", "limit": 500, "max_pages": 2},
        {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "market_cap": 4_885_602_246_714,
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "beta": 1.097,
            "price": 332.64001,
            "last_annual_dividend": 1.05,
            "volume": 29_909_012,
            "exchange": "NASDAQ",
            "exchange_name": "NASDAQ Global Select",
            "country": "US",
            "is_etf": False,
            "is_fund": False,
            "actively_trading": True,
        },
        "company-screener",
        {"limit": "500", "sector": "Technology", "page": "0"},
    ),
    (
        "MarketSnapshots",
        iter2_market.MarketSnapshotsFetcher,
        {"symbols": ["AAPL"]},
        {
            "exchange": "NASDAQ",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "open": 330.0,
            "high": 334.48,
            "low": 329.59,
            "close": 332.39,
            "volume": 29_207_295,
            "prev_close": 333.13,
            "change": -0.74,
            "change_percent": -0.2221355,
            "ma50": 320.0,
            "ma200": 300.0,
            "year_high": 344.57,
            "year_low": 201.5,
            "market_cap": 4_885_602_246_714,
            "last_price_timestamp": "2023-11-14T22:13:20Z",
        },
        "batch-quote",
        {"symbols": "AAPL"},
    ),
    (
        "PricePerformance",
        iter2_market.PricePerformanceFetcher,
        {"symbol": "AAPL"},
        {
            "symbol": "AAPL",
            "one_day": -1.8732,
            "wtd": None,
            "one_week": 3.12782,
            "mtd": None,
            "one_month": 14.68586,
            "qtd": None,
            "three_month": 22.29777,
            "six_month": 27.89233,
            "ytd": 22.06835,
            "one_year": 58.74432,
            "two_year": None,
            "three_year": 68.92594,
            "four_year": None,
            "five_year": 127.51612,
            "ten_year": 1151.81068,
            "max": 258454.74094,
        },
        "stock-price-change",
        {"symbol": "AAPL"},
    ),
    (
        "ShareStatistics",
        iter2_market.ShareStatisticsFetcher,
        {"symbol": "AAPL"},
        {
            "symbol": "AAPL",
            "date": "2026-07-30T15:48:00",
            "free_float": 99.83,
            "float_shares": 15_400_000_000,
            "outstanding_shares": 15_420_000_000,
            "url": "https://www.sec.gov/Archives/edgar/data/320193/float.htm",
        },
        "shares-float",
        {"symbol": "AAPL"},
    ),
    (
        "IndexConstituents",
        iter2_market.IndexConstituentsFetcher,
        {"symbol": "sp500"},
        {
            "symbol": "HONA",
            "name": "Honeywell Aerospace Inc",
            "sector": "Technology",
            "industry": "Aerospace & Defense",
            "headquarter": "Charlotte, North Carolina",
            "date_added": "2026-06-29",
            "cik": "0002089271",
            "founded": "1913-12-31",
            "removed_symbol": None,
            "removed_name": None,
            "reason": None,
            "date": None,
        },
        "sp500-constituent",
        {},
    ),
    (
        "IndexHistorical",
        iter2_market.IndexHistoricalFetcher,
        {"symbol": "^VIX", "start_date": "2026-07-30", "end_date": "2026-07-30"},
        {
            "symbol": "^VIX",
            "date": "2026-07-30",
            "open": 19.56,
            "high": 20.08,
            "low": 17.9,
            "close": 17.9,
            "volume": 0,
            "vwap": 18.63,
            "change": -1.66,
            "change_percent": -8.48671,
        },
        "historical-price-eod/full",
        {"symbol": "^VIX", "from": "2026-07-30", "to": "2026-07-30"},
    ),
]


def _official_row(model: str) -> dict[str, object]:
    if model == "CryptoHistorical":
        return {
            "symbol": "BTCUSD",
            "date": "2026-07-30",
            "open": 63902.9,
            "high": 65038.38,
            "low": 63547.793,
            "close": 64766.98828,
            "volume": 32030003200,
            "change": 864.09,
            "changePercent": 1.35219,
            "vwap": 64451.05,
        }
    if model == "CryptoSearch":
        return {
            "symbol": "MIOTAUSD",
            "name": "IOTA USD",
            "exchange": "CCC",
            "icoDate": "2017-11-09",
            "circulatingSupply": 4232705124,
            "totalSupply": 4788606639,
        }
    if model == "CurrencyHistorical":
        return {
            "symbol": "EURUSD",
            "date": "2026-07-30",
            "open": 1.16,
            "high": 1.17,
            "low": 1.15,
            "close": 1.165,
            "volume": 0,
            "change": 0.005,
            "changePercent": 0.43,
            "vwap": 1.1625,
        }
    if model == "CurrencyPairs":
        return {
            "symbol": "ARSMXN",
            "fromCurrency": "ARS",
            "toCurrency": "MXN",
            "fromName": "Argentine Peso",
            "toName": "Mexican Peso",
        }
    if model == "CurrencySnapshots":
        return {
            "symbol": "EURUSD",
            "price": 1.1,
            "change": 0.05,
            "changePercentage": 4.76,
            "volume": 123,
            "dayLow": 0.9,
            "dayHigh": 1.2,
            "yearHigh": 1.3,
            "yearLow": 0.8,
            "priceAvg50": 1.04,
            "priceAvg200": 1.03,
            "open": 1.0,
            "previousClose": 1.05,
            "timestamp": 1_700_000_000,
        }
    if model == "EquityActive":
        return _mover("AAPL", change=4.0, percent=4.0)
    if model == "EquityPeers":
        return {
            "symbol": "GOOGL",
            "companyName": "Alphabet Inc.",
            "price": 333.84,
            "mktCap": 4_040_168_831_718,
        }
    if model == "EquityInfo":
        return _profile()
    if model == "EquityGainers":
        return _mover("GAIN", change=4.0, percent=4.0)
    if model == "EquityLosers":
        return _mover("LOSS", change=-4.0, percent=-4.0)
    if model == "EquityScreener":
        return {
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "marketCap": 4_885_602_246_714,
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "beta": 1.097,
            "price": 332.64001,
            "lastAnnualDividend": 1.05,
            "volume": 29_909_012,
            "exchange": "NASDAQ Global Select",
            "exchangeShortName": "NASDAQ",
            "country": "US",
            "isEtf": False,
            "isFund": False,
            "isActivelyTrading": True,
        }
    if model == "MarketSnapshots":
        return {
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "price": 332.39,
            "changePercentage": -0.2221355,
            "change": -0.74,
            "volume": 29_207_295,
            "dayLow": 329.59,
            "dayHigh": 334.48,
            "yearHigh": 344.57,
            "yearLow": 201.5,
            "marketCap": 4_885_602_246_714,
            "priceAvg50": 320.0,
            "priceAvg200": 300.0,
            "exchange": "NASDAQ",
            "open": 330.0,
            "previousClose": 333.13,
            "timestamp": 1_700_000_000,
        }
    if model == "PricePerformance":
        return {
            "symbol": "AAPL",
            "1D": -1.8732,
            "5D": 3.12782,
            "1M": 14.68586,
            "3M": 22.29777,
            "6M": 27.89233,
            "ytd": 22.06835,
            "1Y": 58.74432,
            "3Y": 68.92594,
            "5Y": 127.51612,
            "10Y": 1151.81068,
            "max": 258454.74094,
        }
    if model == "ShareStatistics":
        return {
            "symbol": "AAPL",
            "date": "2026-07-30 15:48:00",
            "freeFloat": 99.83,
            "floatShares": 15_400_000_000,
            "outstandingShares": 15_420_000_000,
            "source": "https://www.sec.gov/Archives/edgar/data/320193/float.htm",
        }
    if model == "IndexConstituents":
        return {
            "symbol": "HONA",
            "name": "Honeywell Aerospace Inc",
            "sector": "Technology",
            "subSector": "Aerospace & Defense",
            "headQuarter": "Charlotte, North Carolina",
            "dateFirstAdded": "2026-06-29",
            "cik": "0002089271",
            "founded": "1913-12-31",
        }
    if model == "IndexHistorical":
        return {
            "symbol": "^VIX",
            "date": "2026-07-30",
            "open": 19.56,
            "high": 20.08,
            "low": 17.9,
            "close": 17.9,
            "volume": 0,
            "change": -1.66,
            "changePercent": -8.48671,
            "vwap": 18.63,
        }
    raise AssertionError(model)


_EXPECTED_URL_PARAMS = {"apikey": "offline-test-key"}


@pytest.mark.parametrize(
    "model,fetcher_type,query,expected,route,route_params", _CASES, ids=[case[0] for case in _CASES]
)
def test_each_model_executes_through_query_service_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    query: dict[str, object],
    expected: dict[str, object],
    route: str,
    route_params: dict[str, str],
) -> None:
    """Every canonical query validates the injected identity and returns typed JSON."""
    instance = fetcher_type()
    raw = _official_row(model)
    client = _install_http(monkeypatch, {route: [[raw]]})
    capability = instance.capability
    observed: dict[str, object] = {}
    original = instance.transform_query

    def capture_query(**kwargs: object) -> object:
        observed.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(instance, "transform_query", capture_query)
    descriptor = SimpleNamespace(
        source="fmp",
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
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, instance)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, instance.record_model),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model=model,
            query=query,
            principal=Principal(),
            ctx=_context(model),
        )
    )

    assert response["source"] == "fmp"
    assert response["model"] == model
    assert response["domain"] == capability.domain
    assert observed["source"] == "fmp"
    assert observed["market"] == capability.market
    assert response["results"] == [expected]
    assert response["pagination"] is None
    assert len(client.calls) == 1
    called_url, called_params, source = client.calls[0]
    assert called_url == f"https://financialmodelingprep.com/stable/{route}"
    assert called_params == {**route_params, **_EXPECTED_URL_PARAMS}
    assert source == "fmp"


@pytest.mark.parametrize(
    "model,fetcher_type,query,missing_field,route",
    [
        pytest.param(
            "CryptoSearch",
            iter2_market.CryptoSearchFetcher,
            {"query": "BTC"},
            "symbol",
            "cryptocurrency-list",
            id="CryptoSearch-required-symbol-is-validated-before-filtering",
        ),
        pytest.param(
            "CurrencyPairs",
            iter2_market.CurrencyPairsFetcher,
            {"query": "Euro"},
            "fromName",
            "forex-list",
            id="CurrencyPairs-required-from-name-is-validated-before-filtering",
        ),
    ],
)
def test_directory_fetchers_reject_malformed_rows_even_when_filter_would_skip_them(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    query: dict[str, object],
    missing_field: str,
    route: str,
) -> None:
    """A local search must not turn malformed nonempty source data into no matches."""
    row = _official_row(model)
    row.pop(missing_field)
    client = _install_http(monkeypatch, {route: [[row]]})

    with pytest.raises(fmp_client.FMPProviderError, match="FMP_BAD_FIELD"):
        fetcher_type().fetch(ctx=_context(model), **query)

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "model,fetcher_type,query,route",
    [
        pytest.param(
            "CryptoSearch",
            iter2_market.CryptoSearchFetcher,
            {"query": "not-present"},
            "cryptocurrency-list",
            id="CryptoSearch-no-match",
        ),
        pytest.param(
            "CurrencyPairs",
            iter2_market.CurrencyPairsFetcher,
            {"query": "not-present"},
            "forex-list",
            id="CurrencyPairs-no-match",
        ),
    ],
)
def test_directory_fetchers_keep_valid_no_match_rows_empty(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    query: dict[str, object],
    route: str,
) -> None:
    """Well-formed nonempty source rows that do not match remain a normal empty result."""
    client = _install_http(monkeypatch, {route: [[_official_row(model)]]})

    rows = fetcher_type().fetch(ctx=_context(model), **query)

    assert rows == ()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "model,fetcher_type,query,route",
    [
        pytest.param(
            "CryptoSearch",
            iter2_market.CryptoSearchFetcher,
            {"query": "BTC"},
            "cryptocurrency-list",
            id="CryptoSearch-empty-source",
        ),
        pytest.param(
            "CurrencyPairs",
            iter2_market.CurrencyPairsFetcher,
            {"query": "Euro"},
            "forex-list",
            id="CurrencyPairs-empty-source",
        ),
    ],
)
def test_directory_fetchers_keep_true_empty_source_empty(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type,
    query: dict[str, object],
    route: str,
) -> None:
    """An actually empty upstream directory response remains a normal empty result."""
    client = _install_http(monkeypatch, {route: [[]]})

    rows = fetcher_type().fetch(ctx=_context(model), **query)

    assert rows == ()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "fetcher_type,query",
    [
        pytest.param(
            iter2_market.CryptoHistoricalFetcher,
            {"symbol": "BTCUSD", "start_date": "2026-07-31", "end_date": "2026-07-30"},
            id="CryptoHistorical",
        ),
        pytest.param(iter2_market.CryptoSearchFetcher, {"query": "\x00"}, id="CryptoSearch"),
        pytest.param(
            iter2_market.CurrencyHistoricalFetcher,
            {"symbol": "EUR-X", "start_date": "2026-07-30", "end_date": "2026-07-30"},
            id="CurrencyHistorical",
        ),
        pytest.param(iter2_market.CurrencyPairsFetcher, {"query": "\x7f"}, id="CurrencyPairs"),
        pytest.param(
            iter2_market.CurrencySnapshotsFetcher,
            {"base": "USD", "counter_currencies": ["USD"]},
            id="CurrencySnapshots",
        ),
        pytest.param(iter2_market.EquityActiveFetcher, {"limit": 0}, id="EquityActive"),
        pytest.param(
            iter2_market.EquityPeersFetcher, {"symbol": "AAPL", "max_requests": 2}, id="EquityPeers"
        ),
        pytest.param(iter2_market.EquityInfoFetcher, {"symbol": ["AAPL", "AAPL"]}, id="EquityInfo"),
        pytest.param(iter2_market.EquityGainersFetcher, {"sort": "sideways"}, id="EquityGainers"),
        pytest.param(iter2_market.EquityLosersFetcher, {"limit": 501}, id="EquityLosers"),
        pytest.param(
            iter2_market.EquityScreenerFetcher,
            {"price_min": 20, "price_max": 10},
            id="EquityScreener",
        ),
        pytest.param(
            iter2_market.MarketSnapshotsFetcher,
            {"symbols": ["AAPL"], "exchange": "NASDAQ"},
            id="MarketSnapshots",
        ),
        pytest.param(
            iter2_market.PricePerformanceFetcher,
            {"symbol": ["AAPL", "MSFT"], "max_requests": 1},
            id="PricePerformance",
        ),
        pytest.param(
            iter2_market.ShareStatisticsFetcher, {"symbol": ["AAPL", "AAPL"]}, id="ShareStatistics"
        ),
        pytest.param(
            iter2_market.IndexConstituentsFetcher, {"symbol": "russell"}, id="IndexConstituents"
        ),
        pytest.param(
            iter2_market.IndexHistoricalFetcher,
            {"symbol": "^VIX", "start_date": "2026-01-01", "end_date": "2039-12-31"},
            id="IndexHistorical",
        ),
    ],
)
def test_invalid_query_is_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch, fetcher_type: type, query: dict[str, object]
) -> None:
    client = _install_http(monkeypatch, {})
    with pytest.raises(ValidationError):
        fetcher_type().fetch(**query)
    assert client.calls == []


def test_index_constituents_historical_route_maps_add_remove_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_http(
        monkeypatch,
        {
            "historical-sp500-constituent": [
                [
                    {
                        "dateAdded": "June 29, 2026",
                        "addedSecurity": "Honeywell Aerospace Inc",
                        "removedTicker": "CAG",
                        "removedSecurity": "Conagra Brands",
                        "date": "2026-06-29",
                        "symbol": "HONA",
                        "reason": "Market Capitalization Changes",
                    }
                ]
            ]
        },
    )
    rows = iter2_market.IndexConstituentsFetcher().fetch(
        ctx=_context("IndexConstituents"),
        symbol="sp500",
        historical=True,
    )
    assert len(rows) == 1
    assert rows[0].model_dump(mode="json") == {
        "symbol": "HONA",
        "name": "Honeywell Aerospace Inc",
        "sector": None,
        "industry": None,
        "headquarter": None,
        "date_added": "June 29, 2026",
        "cik": None,
        "founded": None,
        "removed_symbol": "CAG",
        "removed_name": "Conagra Brands",
        "reason": "Market Capitalization Changes",
        "date": "2026-06-29",
    }
    assert client.calls[0][0].endswith("/historical-sp500-constituent")


def test_missing_multi_symbol_row_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install_http(monkeypatch, {"profile": [[_profile()], []]})
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_MISSING_DATA"):
        iter2_market.EquityInfoFetcher().fetch(
            ctx=_context("EquityInfo"),
            symbol=["AAPL", "MSFT"],
        )
    assert len(client.calls) == 2


def test_snapshot_empty_quote_is_missing_data(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install_http(monkeypatch, {"quote": [[]]})
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_MISSING_DATA"):
        iter2_market.CurrencySnapshotsFetcher().fetch(
            ctx=_context("CurrencySnapshots"),
            base="USD",
            counter_currencies=["EUR"],
        )
    assert len(client.calls) == 1


def test_history_includes_exact_date_boundaries_and_rejects_out_of_window_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = _bar("^VIX")
    client = _install_http(monkeypatch, {"historical-price-eod/full": [[boundary]]})
    rows = iter2_market.IndexHistoricalFetcher().fetch(
        ctx=_context("IndexHistorical"),
        symbol="^VIX",
        start_date="2026-07-30",
        end_date="2026-07-30",
    )
    assert rows[0].date.isoformat() == "2026-07-30"
    assert client.calls[0][1]["from"] == client.calls[0][1]["to"] == "2026-07-30"

    outside = {**boundary, "date": "2026-07-31"}
    client = _install_http(monkeypatch, {"historical-price-eod/full": [[outside]]})
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_OUT_OF_WINDOW_ROW"):
        iter2_market.IndexHistoricalFetcher().fetch(
            ctx=_context("IndexHistorical"),
            symbol="^VIX",
            start_date="2026-07-30",
            end_date="2026-07-30",
        )


def test_bad_shape_bad_field_and_rate_limit_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(monkeypatch, {"stock-peers": [{"unexpected": "not an array"}]})
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_BAD_SHAPE"):
        iter2_market.EquityPeersFetcher().fetch(
            ctx=_context("EquityPeers"),
            symbol="AAPL",
        )

    _install_http(
        monkeypatch,
        {
            "stock-peers": [
                [{"symbol": "GOOGL", "companyName": "Alphabet", "price": "bad", "mktCap": 100}]
            ]
        },
    )
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_BAD_FIELD") as bad_field:
        iter2_market.EquityPeersFetcher().fetch(
            ctx=_context("EquityPeers"),
            symbol="AAPL",
        )
    assert "offline-test-key" not in str(bad_field.value)

    _install_http(monkeypatch, {"stock-peers": [(429, {"Error Message": "quota exceeded"})]})
    with pytest.raises(fmp_client.FMPProviderError, match="FMP_RATE_LIMITED") as limited:
        iter2_market.EquityPeersFetcher().fetch(
            ctx=_context("EquityPeers"),
            symbol="AAPL",
        )
    assert "quota exceeded" not in str(limited.value)
    assert "offline-test-key" not in str(limited.value)


def test_screener_full_page_budget_is_explicitly_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_http(
        monkeypatch,
        {
            "company-screener": [
                [
                    {
                        "symbol": "AAPL",
                        "companyName": "Apple Inc.",
                        "marketCap": 100,
                        "sector": "Technology",
                        "industry": "Consumer Electronics",
                        "beta": 1.0,
                        "price": 10.0,
                        "lastAnnualDividend": 1.0,
                        "volume": 10,
                        "exchange": "NASDAQ Global Select",
                        "exchangeShortName": "NASDAQ",
                        "country": "US",
                        "isEtf": False,
                        "isFund": False,
                        "isActivelyTrading": True,
                    }
                ]
            ]
        },
    )
    with pytest.raises(fmp_client.FMPQueryLimitError) as incomplete:
        iter2_market.EquityScreenerFetcher().fetch(
            ctx=_context("EquityScreener"),
            limit=1,
            max_pages=1,
        )
    assert incomplete.value.code == "FMP_INCOMPLETE_PAGE_BUDGET"
    assert len(client.calls) == 1
