"""Offline hand-authored contract fixtures for the TMX Money adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data import domains
from opendata.data.domains import DomainSpec
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models.tmx import TmxEquityHistorical, TmxEquitySearch
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.tmx.models import client as tmx_client
from opendata.data.providers.tmx.models.client import TmxProviderError
from opendata.data.providers.tmx.models.fetchers import (
    CompanyFilingsFetcher,
    EquityHistoricalFetcher,
    EquityInfoFetcher,
    EquityQuoteFetcher,
    EquitySearchFetcher,
    HistoricalDividendsFetcher,
    IndexConstituentsFetcher,
    PriceTargetConsensusFetcher,
)
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import query_provider_model


def _authorized_context(model: str) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="tmx",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-synthetic-fixture-only",
        task_attempts=8,
        source_attempts=8,
        allowed_hosts={"app-money.tmx.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    return FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=8,
            source_attempts=8,
            grants=(grant,),
            profile=profile,
        ),
    )


class _FixtureTransport:
    """Serve explicit GraphQL documents without permitting network access."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, documents: list[object]) -> None:
        self.documents = list(documents)
        self.requests: list[dict[str, object]] = []
        session = requests.Session()

        def request(method: str, url: str, **kwargs: object) -> requests.Response:
            assert method == "POST"
            assert url == tmx_client.GRAPHQL_URL
            body = kwargs.get("json")
            assert isinstance(body, dict)
            self.requests.append(body)
            assert self.documents, "fetcher made more requests than fixture pages"
            document = self.documents.pop(0)
            response = requests.Response()
            response.status_code = 200
            response.url = url
            response.encoding = "utf-8"
            response._content = json.dumps(document).encode("utf-8")
            return response

        monkeypatch.setattr(session, "request", request)
        governed = GovernedHttpClient(session=session, sleep=lambda _seconds: None)
        monkeypatch.setattr(tmx_client, "get_shared_http_client", lambda: governed)


def _data(root: str, value: object) -> dict[str, object]:
    return {"data": {root: value}}


def test_tmx_equity_historical_preserves_iso_date_and_datetime_validation() -> None:
    common_fields = {"symbol": "RY", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
    calendar_date = TmxEquityHistorical.model_validate({**common_fields, "date": "2026-10-09"})
    timestamp = TmxEquityHistorical.model_validate(
        {**common_fields, "date": "2026-10-09T14:30:00Z"}
    )

    assert type(calendar_date.date) is date
    assert timestamp.date == datetime(2026, 10, 9, 14, 30, tzinfo=timezone.utc)
    assert isinstance(timestamp.date, datetime)


@pytest.mark.parametrize("value", ("2026-02-30", "2026-10-09T25:00:00Z", "2026/10/09"))
def test_tmx_equity_historical_rejects_invalid_iso_dates(value: str) -> None:
    with pytest.raises(ValidationError):
        TmxEquityHistorical.model_validate(
            {"symbol": "RY", "date": value, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}
        )


CASES: tuple[tuple[str, Fetcher[Any, Any], dict[str, object], object, object], ...] = (
    (
        "EquitySearch",
        EquitySearchFetcher(),
        {"keywords": "Toronto-Dominion"},
        _data(
            "findTmxCompaniesByKeywords",
            [
                {
                    "symbol": "TD",
                    "name": "The Toronto-Dominion Bank",
                    "exchange": "XTSE",
                    "instrumentType": "Common Shares",
                }
            ],
        ),
        _data(
            "findTmxCompaniesByKeywords",
            [
                {
                    "symbol": 7,
                    "name": "The Toronto-Dominion Bank",
                    "exchange": "XTSE",
                    "instrumentType": "Common Shares",
                }
            ],
        ),
    ),
    (
        "EquityHistorical",
        EquityHistoricalFetcher(),
        {"symbol": "RY", "start_date": date(2026, 10, 9), "end_date": date(2026, 10, 9)},
        _data(
            "getCompanyPriceHistory",
            [
                {
                    "datetime": "2026-10-09",
                    "openPrice": 136.1,
                    "high": 137.2,
                    "low": 135.9,
                    "closePrice": 136.8,
                    "volume": 923400,
                    "tradeValue": 126388320.0,
                    "numberOfTrade": 1850,
                    "change": 0.7,
                    "changePercent": 0.0051,
                    "vwap": 136.6,
                }
            ],
        ),
        _data(
            "getCompanyPriceHistory",
            [
                {
                    "datetime": "2026-10-09",
                    "openPrice": "136.1",
                    "high": 137.2,
                    "low": 135.9,
                    "closePrice": 136.8,
                }
            ],
        ),
    ),
    (
        "EquityQuote",
        EquityQuoteFetcher(),
        {"symbol": "RY"},
        _data(
            "getQuoteBySymbol",
            {
                "symbol": "RY",
                "name": "Royal Bank of Canada",
                "price": 145.75,
                "priceChange": 0.55,
                "percentChange": 0.0038,
                "exchangeName": "Toronto Stock Exchange",
                "exchangeCode": "XTSX",
                "openPrice": 145.1,
                "dayHigh": 146.0,
                "dayLow": 144.95,
                "volume": 560000,
                "prevClose": 145.2,
                "weeks52high": 150.0,
                "weeks52low": 118.0,
                "MarketCap": 204000000000,
                "peRatio": 13.4,
                "eps": 10.87,
                "vwap": 145.6,
                "currency": "CAD",
            },
        ),
        _data(
            "getQuoteBySymbol",
            {"symbol": "RY", "name": "Royal Bank of Canada", "price": "145.75"},
        ),
    ),
    (
        "EquityInfo",
        EquityInfoFetcher(),
        {"symbol": "RY"},
        _data(
            "getQuoteBySymbol",
            {
                "symbol": "RY",
                "name": "Royal Bank of Canada",
                "exchangeName": "Toronto Stock Exchange",
                "longDescription": "A synthetic description for fixture validation.",
                "website": "https://example.invalid/",
                "fullAddress": "Synthetic address",
                "phoneNumber": "+1-000-000-0000",
                "email": "fixture@example.invalid",
                "employees": 95000,
                "sector": "Financials",
                "issueType": "Common Shares",
                "shareOutStanding": 1400000000,
                "sharesESCROW": 0,
                "totalSharesOutStanding": 1400000000,
                "dividendFrequency": "Quarterly",
            },
        ),
        _data("getQuoteBySymbol", {"symbol": "RY", "employees": "95000"}),
    ),
    (
        "CompanyFilings",
        CompanyFilingsFetcher(),
        {"symbol": "RY", "start_date": date(2026, 1, 1), "end_date": date(2026, 10, 1)},
        _data(
            "filings",
            [
                {
                    "filingDate": "2026-09-29",
                    "size": 42100,
                    "description": "Synthetic annual filing fixture",
                    "urlToPdf": "https://example.invalid/filing.pdf",
                }
            ],
        ),
        _data(
            "filings",
            [{"filingDate": "2026-09-29", "size": 42100, "description": "missing URL"}],
        ),
    ),
    (
        "HistoricalDividends",
        HistoricalDividendsFetcher(),
        {"symbol": "RY", "start_date": date(2026, 1, 1), "end_date": date(2026, 12, 31)},
        [
            _data(
                "dividendHistory",
                {
                    "pageNumber": 1,
                    "hasNextPage": True,
                    "dividends": [
                        {
                            "exDate": "2026-01-25",
                            "amount": 1.48,
                            "currency": "CAD",
                            "payableDate": "2026-02-24",
                            "declarationDate": "2025-12-03",
                            "recordDate": "2026-01-26",
                        }
                    ],
                },
            ),
            _data(
                "dividendHistory",
                {"pageNumber": 2, "hasNextPage": False, "dividends": []},
            ),
        ],
        _data(
            "dividendHistory",
            {
                "pageNumber": 1,
                "hasNextPage": False,
                "dividends": [{"exDate": "2026-01-25", "amount": "1.48"}],
            },
        ),
    ),
    (
        "IndexConstituents",
        IndexConstituentsFetcher(),
        {"symbol": "^TSX"},
        _data(
            "constituents",
            [
                {
                    "symbol": "RY",
                    "longName": "Royal Bank of Canada",
                    "quotedMarketValue": 204000000000.0,
                    "weight": 0.057,
                    "exchange": "TSX",
                }
            ],
        ),
        _data("constituents", [{"symbol": "RY", "quotedMarketValue": "204000000000"}]),
    ),
    (
        "PriceTargetConsensus",
        PriceTargetConsensusFetcher(),
        {"symbol": "RY"},
        _data(
            "getCompanyAnalysts",
            {
                "totalAnalysts": 12,
                "priceTarget": {
                    "lowPriceTarget": 132.0,
                    "highPriceTarget": 167.0,
                    "priceTarget": 151.5,
                    "priceTargetUpside": 0.04,
                },
                "consensusAnalysts": {"consensus": "Buy", "buy": 7, "sell": 1, "hold": 4},
            },
        ),
        _data(
            "getCompanyAnalysts",
            {
                "totalAnalysts": 12,
                "priceTarget": {
                    "lowPriceTarget": 132.0,
                    "highPriceTarget": 167.0,
                    "priceTarget": 151.5,
                },
                "consensusAnalysts": {"consensus": "Buy", "buy": "7", "sell": 1, "hold": 4},
            },
        ),
    ),
)


def _fetch(fetcher: Fetcher[Any, Any], query: dict[str, object]) -> FetchResult:
    model = fetcher.canonical_model
    assert model is not None
    return fetcher.fetch(ctx=_authorized_context(model), **query)


@pytest.mark.parametrize("model,fetcher,query,positive,negative", CASES)
def test_tmx_fetcher_parses_manual_contract_fixture_and_rejects_corruption(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    positive: object,
    negative: object,
) -> None:
    """Each READY model has an independent positive and destructive negative."""
    positive_documents = positive if isinstance(positive, list) else [positive]
    transport = _FixtureTransport(monkeypatch, positive_documents)
    rows = _fetch(fetcher, query)
    assert rows
    assert not transport.documents
    assert all(request.get("operationName") for request in transport.requests)
    row = rows[0]
    if model == "EquitySearch":
        assert row.model_dump() == {
            "symbol": "TD",
            "name": "The Toronto-Dominion Bank",
        }
        query_text = transport.requests[0].get("query")
        assert isinstance(query_text, str)
        assert "symbol name exchange instrumentType" in query_text
    elif model == "EquityHistorical":
        assert row.date == date(2026, 10, 9) and row.close == 136.8
        assert row.change_percent is None
    elif model == "EquityQuote":
        assert row.exchange == "XTSX" and row.last_price == 145.75
        assert row.change_percent is None
    elif model == "EquityInfo":
        assert row.stock_exchange == "Toronto Stock Exchange" and row.issue_type == "Common Shares"
    elif model == "CompanyFilings":
        assert row.symbol == "RY" and row.report_url == "https://example.invalid/filing.pdf"
        assert row.report_type is None
    elif model == "HistoricalDividends":
        assert row.ex_dividend_date == date(2026, 1, 25)
        assert row.payment_date == date(2026, 2, 24)
    elif model == "IndexConstituents":
        assert row.market_value == 204000000000.0 and row.weight == 0.057
    elif model == "PriceTargetConsensus":
        assert row.target_consensus == 151.5 and row.target_upside is None

    transport = _FixtureTransport(monkeypatch, [negative])
    with pytest.raises(TmxProviderError):
        _fetch(fetcher, query)
    assert not transport.documents


@pytest.mark.parametrize(
    "bad_row",
    [
        {
            "symbol": "TD",
            "name": "The Toronto-Dominion Bank",
            "exchange": 7,
            "instrumentType": "Common Shares",
        },
        {
            "symbol": "TD",
            "name": "The Toronto-Dominion Bank",
            "exchange": "XTSE",
            "instrumentType": 7,
        },
        {
            "symbol": "TD",
            "name": "The Toronto-Dominion Bank",
            "exchange": "XTSE",
        },
    ],
)
def test_tmx_equity_search_validates_every_selected_source_field(
    monkeypatch: pytest.MonkeyPatch,
    bad_row: dict[str, object],
) -> None:
    transport = _FixtureTransport(
        monkeypatch,
        [_data("findTmxCompaniesByKeywords", [bad_row])],
    )
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _fetch(EquitySearchFetcher(), {"keywords": "Toronto-Dominion"})
    assert not transport.documents


def test_tmx_dividend_paging_stops_only_on_explicit_terminal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _data(
        "dividendHistory",
        {"pageNumber": 1, "hasNextPage": True, "dividends": []},
    )
    second = _data(
        "dividendHistory",
        {"pageNumber": 2, "hasNextPage": False, "dividends": []},
    )
    transport = _FixtureTransport(monkeypatch, [first, second])
    rows = _fetch(HistoricalDividendsFetcher(), {"symbol": "RY"})
    assert rows == ()
    assert [request["variables"]["page"] for request in transport.requests] == [1, 2]

    transport = _FixtureTransport(
        monkeypatch,
        [
            _data(
                "dividendHistory",
                {"pageNumber": 1, "hasNextPage": "false", "dividends": []},
            )
        ],
    )
    with pytest.raises(TmxProviderError, match="TMX_BAD_PAGE"):
        _fetch(HistoricalDividendsFetcher(), {"symbol": "RY"})


def test_tmx_query_validation_rejects_ambiguous_symbol_and_history_window() -> None:
    with pytest.raises(ValidationError):
        EquityQuoteFetcher().transform_query(symbol=" RY ")
    with pytest.raises(ValidationError):
        EquityQuoteFetcher().transform_query(symbol="RY", market="US")
    with pytest.raises(ValidationError):
        EquityHistoricalFetcher().transform_query(
            symbol="RY",
            start_date=date(2026, 1, 2),
            end_date=date(2026, 1, 1),
        )


def test_query_service_pins_market_and_source_and_serializes_typed_tmx_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = EquitySearchFetcher()
    registry = ProviderRegistry()
    registry.register_provider_models("tmx", ("EquitySearch",), fetcher)
    spec = DomainSpec(
        display_name="TMX equity search fixture",
        rest_path="market/tmx/equity-search",
        contract="TmxEquitySearch",
        temporal_kind="snapshot",
        time_field=None,
        natural_key=("symbol",),
        filter_dims=("symbol", "name"),
        storage_mode="transient",
        permissions=("query",),
    )
    monkeypatch.setattr(domains, "require_domain_semantics", lambda _domain: spec)
    monkeypatch.setattr(domains, "contract_model", lambda _domain: TmxEquitySearch)

    captured_query: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        captured_query.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    transport = _FixtureTransport(
        monkeypatch,
        [
            _data(
                "findTmxCompaniesByKeywords",
                [
                    {
                        "symbol": "TD",
                        "name": "The Toronto-Dominion Bank",
                        "exchange": "XTSE",
                        "instrumentType": "Common Shares",
                    }
                ],
            )
        ],
    )

    response = asyncio.run(
        query_provider_model(
            registry=registry,
            source="tmx",
            model="EquitySearch",
            query={"keywords": "Toronto-Dominion"},
            principal=SimpleNamespace(allows_domain=lambda domain: domain == "tmx_equity_search"),
            ctx=_authorized_context("EquitySearch"),
        )
    )

    assert captured_query["source"] == "tmx"
    assert captured_query["market"] == "CA"
    assert response["source"] == "tmx"
    assert response["model"] == "EquitySearch"
    assert response["domain"] == "tmx_equity_search"
    assert response["verified"] is False
    assert response["pagination"] is None
    assert response["results"] == [{"symbol": "TD", "name": "The Toronto-Dominion Bank"}]
    assert transport.requests[0]["operationName"] == "findTmxCompaniesByKeywords"
