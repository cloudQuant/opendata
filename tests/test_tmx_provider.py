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
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.tmx import TmxEquityHistorical, TmxEquitySearch
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.tmx.models import client as tmx_client
from opendata.data.providers.tmx.models import fetchers_w33_etf_loaders as tmx_directory
from opendata.data.providers.tmx.models import fetchers_w33_graphql as tmx_graphql
from opendata.data.providers.tmx.models import fetchers_w34_envelope as tmx_envelope
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
    RequestBudgetError,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import query_provider_model
from scripts.quality.iter2_witnesses._parts.tmx import rows_w33_etf_loaders as directory_rows
from scripts.quality.iter2_witnesses._parts.tmx import rows_w33_graphql as graphql_rows
from scripts.quality.iter2_witnesses._parts.tmx import rows_w34_options_snapshots as envelope_rows


def _authorized_context(
    model: str,
    *,
    hosts: set[str] | None = None,
    timeout: float | None = 5.0,
) -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="tmx",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-synthetic-fixture-only",
        task_attempts=8,
        source_attempts=8,
        allowed_hosts={"app-money.tmx.com"} if hosts is None else hosts,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    return FetchContext(
        timeout=timeout,
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


# These builders provide synthetic source-shaped bodies, never official/live captures.
_WAVE_HOSTS = {
    "app-money.tmx.com",
    "www.m-x.ca",
    "tmxinfoservices.com",
    "dgr53wu9i7rmp.cloudfront.net",
    "bondtradedata.iiroc.ca",
}


class _WaveTransport:
    """Replace only session I/O; admission, HTTP classification and fetch stages remain real."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        seam: Any,
        document: dict[str, Any],
    ) -> None:
        self.responses = list(document["responses"])
        self.requests: list[dict[str, Any]] = []
        session = requests.Session()

        def request(method: str, url: str, **kwargs: Any) -> requests.Response:
            self.requests.append({"method": method, "url": url, **kwargs})
            assert self.responses, "unexpected extra transport attempt"
            spec = self.responses.pop(0)
            assert method == spec["method"]
            assert spec["url_contains"] in url
            if "exception" in spec:
                raise spec["exception"]
            body = spec["body"]
            if isinstance(body, bytes):
                content = body
            elif spec.get("content_type", "").startswith("text/"):
                content = ("\n".join(body) if isinstance(body, list) else body).encode("utf-8")
            else:
                content = json.dumps(body).encode("utf-8")
            response = requests.Response()
            response.status_code = spec.get("status", 200)
            response.url = url
            response.encoding = "utf-8"
            response.headers["Content-Type"] = spec.get("content_type", "application/json")
            response._content = content
            return response

        monkeypatch.setattr(session, "request", request)
        governed = GovernedHttpClient(
            config=HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
            session=session,
            sleep=lambda _seconds: None,
        )
        monkeypatch.setattr(seam, "get_shared_http_client", lambda: governed)


def _wave_fetch(
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    *,
    timeout: float | None = 5.0,
) -> FetchResult:
    assert fetcher.canonical_model is not None
    return fetcher.fetch(
        ctx=_authorized_context(fetcher.canonical_model, hosts=_WAVE_HOSTS, timeout=timeout),
        **query,
    )


def _assert_wave_requests(transport: _WaveTransport, count: int) -> None:
    assert not transport.responses
    assert len(transport.requests) == count
    for request in transport.requests:
        assert request["timeout"] == 5.0
        assert request["allow_redirects"] is False
        assert request["headers"]["Accept"]


def test_tmx_options_expands_weekly_sides_and_live_cells_override_eod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._options_chains_document()
    cells = document["responses"][1]["body"][1].split(",")
    # Deliberately disagree with every live cell shared with EOD.
    for index in (7, 8, 10, 17):
        cells[index] = "999"
    cells[-2] = "SOURCE-CALL-ID"
    document["responses"][1]["body"][1] = ",".join(cells)
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    rows = _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "shop.TO"})
    assert [(row.expiration, row.strike, row.option_type) for row in rows] == [
        (date(2026, 10, 16), 120.0, "call"),
        (date(2026, 10, 16), 120.0, "put"),
        (date(2026, 11, 20), 125.0, "call"),
        (date(2026, 11, 20), 125.0, "put"),
    ]
    assert rows[0].model_dump(exclude_none=True) == {
        "contract_symbol": "SOURCE-CALL-ID",
        "expiration": date(2026, 10, 16),
        "strike": 120.0,
        "option_type": "call",
        "underlying_symbol": "SHOP:CA",
        "eod_date": date(2026, 10, 9),
        "dte": 7,
        "bid": 10.5,
        "ask": 11.0,
        "last_trade_price": 10.8,
        "change": 0.9,
        "open_interest": 3210,
        "volume": 45,
        "close_bid": 10.4,
        "close_ask": 11.1,
        "close_bid_size": 12,
        "close_ask_size": 20,
        "prev_close": 9.9,
        "open": 10.1,
        "high": 11.2,
        "low": 9.8,
        "total_value": 48600.0,
        "transactions": 12,
        "settlement_price": 10.75,
        "implied_volatility": pytest.approx(0.342),
    }
    assert rows[1].contract_symbol == "SHOP  261016P00120000"
    assert rows[1].change == -0.4 and rows[1].volume == 12
    assert rows[3].last_trade_price is None and rows[3].change is None
    assert rows[3].volume == 0 and rows[3].dte == 42
    assert all(row.strike != 0 and row.underlying_price is None for row in rows)
    _assert_wave_requests(transport, 2)
    assert [(request["method"], request["url"]) for request in transport.requests] == [
        ("GET", "https://www.m-x.ca/en/trading/data/quotes?symbol=SHOP"),
        ("GET", "https://www.m-x.ca/en/trading/data/historical?symbol=SHOP&dnld=1#quotes"),
    ]
    assert [request["headers"]["Accept"] for request in transport.requests] == [
        "text/html,application/xhtml+xml",
        "text/csv,application/octet-stream",
    ]


def test_tmx_options_live_only_contracts_and_blank_eod_identifier_use_derived_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._options_chains_document()
    eod = document["responses"][1]["body"]
    cells = eod[1].split(",")
    cells[-2] = " "
    cells[-1] = "call"
    eod[1] = ",".join(cells)
    cells = eod[2].split(",")
    cells[-1] = "put"
    eod[2] = ",".join(cells)
    del eod[4]  # Far put is on the live page but not in the last session.
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    rows = _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "SHOP.TSX"})
    assert rows[0].contract_symbol == "SHOP  261016C00120000"
    assert rows[0].eod_date == date(2026, 10, 9)
    assert rows[3].model_dump(exclude_none=True) == {
        "contract_symbol": "SHOP  261120P00125000",
        "expiration": date(2026, 11, 20),
        "strike": 125.0,
        "option_type": "put",
        "underlying_symbol": "SHOP:CA",
        "bid": 13.1,
        "ask": 13.8,
        "open_interest": 410,
        "volume": 0,
    }
    # Neither the expired EOD-only contract nor its class/root record becomes a listed row.
    assert len(rows) == 4
    _assert_wave_requests(transport, 2)


@pytest.mark.parametrize("expiration", ["2026-10-16", "2026-11-20"])
def test_tmx_options_expiration_screen_precedes_caller_limit(
    monkeypatch: pytest.MonkeyPatch,
    expiration: str,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_envelope, envelope_rows._options_chains_document())
    rows = _wave_fetch(
        tmx_envelope.OptionsChainsFetcher(),
        {"symbol": "SHOP", "expiration": expiration, "limit": 2},
    )
    assert [(row.expiration.isoformat(), row.option_type) for row in rows] == [
        (expiration, "call"),
        (expiration, "put"),
    ]
    _assert_wave_requests(transport, 2)


@pytest.mark.parametrize(
    "query,code",
    [
        ({"limit": 3}, "TMX_BAD_ROWS"),
        ({"expiration": "2026-12-18"}, "TMX_BAD_ROWS"),
        ({"expiration": "N/A"}, "TMX_BAD_ROW"),
        ({"expiration": "2026/10/16"}, "TMX_BAD_ROW"),
        ({"expiration": "2026-02-30"}, "TMX_BAD_ROW"),
    ],
)
def test_tmx_options_refuses_empty_expirations_and_overdelivery(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
    code: str,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_envelope, envelope_rows._options_chains_document())
    with pytest.raises(TmxProviderError, match=code) as exc:
        _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "SHOP", **query})
    assert exc.value.code == code
    _assert_wave_requests(transport, 2)


@pytest.mark.parametrize(
    "replacement,code",
    [
        ("<html><body>No table</body></html>", "TMX_BAD_ROWS"),
        ('colspan="5"', "TMX_BAD_ROWS"),
        ("Expiry missing", "TMX_BAD_ROWS"),
        ("2026/10/16", "TMX_BAD_ROW"),
        ("N/A-expiry", "TMX_BAD_ROW"),
        ("fractional-count", "TMX_BAD_ROW"),
        ("bad-price", "TMX_BAD_ROW"),
        ("missing-strike", "TMX_BAD_ROW"),
        ("short-row", "TMX_BAD_ROWS"),
    ],
)
def test_tmx_options_refuses_malformed_html_and_typed_cells(
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
    code: str,
) -> None:
    document = envelope_rows._options_chains_document()
    lines = document["responses"][0]["body"]
    if replacement.startswith("<html>"):
        lines[:] = [replacement]
    elif replacement.startswith("colspan"):
        lines[0] = lines[0].replace('colspan="6"', replacement, 1)
    elif replacement == "Expiry missing":
        lines[0] = lines[0].replace("Expiry date", replacement)
    elif replacement == "fractional-count":
        lines[1] = lines[1].replace("<td>3210</td>", "<td>3210.5</td>")
    elif replacement == "bad-price":
        lines[1] = lines[1].replace("<td>10.50</td>", "<td>$10.50</td>")
    elif replacement == "missing-strike":
        lines[1] = lines[1].replace("<td>120.00</td>", "<td>--</td>")
    elif replacement == "short-row":
        lines[1] = lines[1].replace("<td>120.00</td>", "")
    else:
        lines[1] = lines[1].replace(
            "2026-10-16", "N/A" if replacement == "N/A-expiry" else replacement
        )
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "SHOP"})
    _assert_wave_requests(transport, 2)


@pytest.mark.parametrize(
    "column,value,code",
    [
        (None, "", "TMX_BAD_ROWS"),
        (None, "wrong-header", "TMX_BAD_ROWS"),
        (None, "short-row", "TMX_BAD_ROWS"),
        (0, "2026-10-09T16:00:00Z", "TMX_BAD_ROW"),
        (0, "N/A", "TMX_BAD_ROW"),
        (1, "--", "TMX_BAD_ROW"),
        (2, "2026-02-30", "TMX_BAD_ROW"),
        (2, "N/A", "TMX_BAD_ROW"),
        (5, "12.5", "TMX_BAD_ROW"),
        (8, "1.5", "TMX_BAD_ROW"),
        (15, "0.5", "TMX_BAD_ROW"),
        (18, "not-a-number", "TMX_BAD_ROW"),
        (20, "2", "TMX_BAD_ROW"),
    ],
)
def test_tmx_options_refuses_malformed_csv_dates_counts_and_flags(
    monkeypatch: pytest.MonkeyPatch,
    column: int | None,
    value: str,
    code: str,
) -> None:
    document = envelope_rows._options_chains_document()
    lines = document["responses"][1]["body"]
    if column is not None:
        cells = lines[1].split(",")
        cells[column] = value
        lines[1] = ",".join(cells)
    elif value == "wrong-header":
        lines[0] = lines[0].replace("Symbol", "Ticker")
    elif value == "short-row":
        lines[1] = ",".join(lines[1].split(",")[:-1])
    else:
        lines[:] = [value]
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "SHOP"})
    _assert_wave_requests(transport, 2)


def test_tmx_options_missing_statistics_stay_null_and_ticker_normalization_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._options_chains_document()
    cells = document["responses"][1]["body"][1].split(",")
    for index in (5, 6, 18):
        cells[index] = "N/A"
    document["responses"][1]["body"][1] = ",".join(cells)
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    rows = _wave_fetch(tmx_envelope.OptionsChainsFetcher(), {"symbol": "bip-un.TO"})
    assert rows[0].underlying_symbol == "BIP.UN:CA"
    assert rows[0].close_bid_size is None and rows[0].close_ask_size is None
    assert rows[0].implied_volatility is None
    assert transport.requests[0]["url"].endswith("symbol=BIP.UN")
    _assert_wave_requests(transport, 2)


def test_tmx_index_snapshots_join_symbols_preserve_directory_percent_and_round_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._index_snapshots_document()
    panel = document["responses"][1]["body"]["data"]["getQuoteForSymbols"]
    panel[0].update(
        price=24017.416, prevClose=23840.024, weeks52high=25000.126, weeks52low=18999.164
    )
    panel.reverse()  # Join by symbol, not response order.
    panel.extend([{"symbol": "CUX", "price": 999}, {"symbol": "", "price": 888}, "not-a-row"])
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    rows = _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {"region": "ca"})
    assert rows[0].model_dump(exclude_none=True) == {
        "symbol": "CUX",
        "name": "S&P/TSX Composite Index",
        "currency": "CAD",
        "price": 24017.42,
        "prev_close": 23840.02,
        "change": 177.39,
        "change_percent": 0.0082,
        "year_high": 25000.13,
        "year_low": 18999.16,
        "return_mtd": 0.019,
        "return_qtd": 0.034,
        "return_ytd": 0.086,
        "total_market_value": 2_400_000_000_000.0,
        "constituent_average_market_value": 8_100_000_000.0,
        "constituent_median_market_value": 3_200_000_000.0,
        "constituent_top10_market_value": 1_512_000_000_000.0,
        "constituent_largest_market_value": 310_000_000_000.0,
        "constituent_largest_weight": 0.129,
        "constituent_smallest_market_value": 11_000_000.0,
        "constituent_smallest_weight": 0.0001,
    }
    assert [(row.symbol, row.currency, row.price, row.change_percent) for row in rows] == [
        ("CUX", "CAD", 24017.42, 0.0082),
        ("SPT", "USD", 1874.23, -0.0044),
    ]
    assert all(row.volume is None and row.number_of_constituents is None for row in rows)
    _assert_wave_requests(transport, 2)
    assert (
        transport.requests[0]["url"]
        == "https://tmxinfoservices.com/files/indices/sptsx-indices.json"
    )
    body = transport.requests[1]["json"]
    assert body["operationName"] == "getQuoteForSymbols"
    assert body["variables"] == {"symbols": ["CUX", "SPT", "HGL"]}
    assert transport.requests[1]["url"] == tmx_client.GRAPHQL_URL


@pytest.mark.parametrize("region", ["us", "eu"])
def test_tmx_index_snapshots_refuse_unevidenced_regions_without_sending(
    monkeypatch: pytest.MonkeyPatch,
    region: str,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_envelope, {"responses": []})
    with pytest.raises(TmxProviderError, match="TMX_BAD_FILTER"):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {"region": region})
    assert transport.requests == []


@pytest.mark.parametrize(
    "body,code",
    [
        (b"{broken", "TMX_BAD_JSON"),
        ([], "TMX_BAD_ROWS"),
        ({}, "TMX_BAD_ROWS"),
        ({"indices": []}, "TMX_BAD_ROWS"),
        ({"indices": {"CUX": []}}, "TMX_BAD_ROWS"),
        ({"indices": {"CUX": {"name_en": ""}}}, "TMX_BAD_ROW"),
        ({"indices": {"CUX": {"name_en": "Index", "performance": []}}}, "TMX_BAD_ROWS"),
        ({"indices": {"CUX": {"name_en": "Index", "quotedmarketvalue": 1}}}, "TMX_BAD_ROWS"),
    ],
)
def test_tmx_index_snapshots_refuse_invalid_directory_before_quote_request(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
    code: str,
) -> None:
    document = envelope_rows._index_snapshots_document()
    document["responses"][0]["body"] = body
    document["responses"].pop()
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not-json", "TMX_BAD_JSON"),
        ([], "TMX_GRAPHQL_ERROR"),
        ({"errors": [], "data": {}}, "TMX_GRAPHQL_ERROR"),
        ({"data": []}, "TMX_BAD_ENVELOPE"),
        ({"data": {}}, "TMX_BAD_ENVELOPE"),
        (_data("getQuoteForSymbols", {}), "TMX_BAD_ROWS"),
        (_data("getQuoteForSymbols", None), "TMX_BAD_ROWS"),
        (_data("getQuoteForSymbols", []), "TMX_BAD_ROWS"),
    ],
)
def test_tmx_index_snapshots_refuse_malformed_quote_envelopes_and_unquoted_panels(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
    code: str,
) -> None:
    document = envelope_rows._index_snapshots_document()
    document["responses"][1]["body"] = body
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {})
    _assert_wave_requests(transport, 2)


@pytest.mark.parametrize(
    "key,value",
    [("price", "24017.41"), ("price", True), ("volume", 1.5), ("number_of_constituents", 1.5)],
)
def test_tmx_index_snapshots_refuse_numeric_text_booleans_and_fractional_counts(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: object,
) -> None:
    document = envelope_rows._index_snapshots_document()
    document["responses"][1]["body"]["data"]["getQuoteForSymbols"][0][key] = value
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {})
    _assert_wave_requests(transport, 2)


def test_tmx_index_snapshots_nullable_unknown_and_count_cells_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._index_snapshots_document()
    directory = document["responses"][0]["body"]["indices"]
    directory["CUX"]["performance"].update(
        previousday=0, monthtodate=None, unknownMetric=123, yeartodate=""
    )
    directory["SPT"].pop("quotedmarketvalue")
    panel = document["responses"][1]["body"]["data"]["getQuoteForSymbols"]
    panel[0].update(volume=12, number_of_constituents=3, weeks52low=None)
    panel[1]["price"] = " "
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    rows = _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {"limit": 1})
    assert len(rows) == 1 and rows[0].symbol == "CUX"
    assert rows[0].change_percent is None and rows[0].return_mtd is None
    assert rows[0].return_ytd is None and rows[0].year_low is None
    assert rows[0].volume == 12 and rows[0].number_of_constituents == 3
    _assert_wave_requests(transport, 2)
    transport = _WaveTransport(monkeypatch, tmx_envelope, envelope_rows._index_snapshots_document())
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {"limit": 1})
    _assert_wave_requests(transport, 2)


def _expected_etf_profile() -> dict[str, object]:
    return {
        "symbol": "XYZ",
        "name": "Growth Portfolio ETF",
        "issuer": "Family House Funds",
        "domicile": None,
        "website": "https://www.tmx.example/factsheet/XYZ",
        "description": "Growth Portfolio ETF seeks long-term growth.",
        "inception_date": date(2012, 3, 15),
        "investment_style": "Growth",
        "esg": True,
        "currency": "CAD",
        "unit_price": 21.44,
        "close": 21.4,
        "prev_close": 21.32,
        "return_1m": 1.2,
        "return_3m": 3.4,
        "return_6m": 6.1,
        "return_ytd": 9.8,
        "return_1y": 12.5,
        "return_3y": 30.2,
        "return_5y": 55.1,
        "return_10y": 120.4,
        "return_from_inception": 210.9,
        "avg_volume": 184000,
        "avg_volume_30d": 176000,
        "aum": 812500000.0,
        "pe_ratio": 18.9,
        "pb_ratio": 2.4,
        "management_fee": 0.2,
        "mer": 0.58,
        "distribution_yield": 1.7,
        "dividend_frequency": "Monthly",
    }


@pytest.mark.parametrize(
    "name,fetcher,query,builder,count,url",
    [
        (
            "info",
            tmx_directory.EtfInfoFetcher(),
            {"symbol": "xyz.TO"},
            directory_rows._etf_directory_document,
            1,
            "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json",
        ),
        (
            "search",
            tmx_directory.EtfSearchFetcher(),
            {"query": " GrOwTh "},
            directory_rows._etf_directory_document,
            3,
            "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json",
        ),
        (
            "sectors",
            tmx_directory.EtfSectorsFetcher(),
            {"symbol": "xyz.tsx"},
            directory_rows._etf_directory_document,
            2,
            "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json",
        ),
        (
            "countries",
            tmx_directory.EtfCountriesFetcher(),
            {"symbol": "xyz"},
            directory_rows._etf_directory_document,
            2,
            "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json",
        ),
        (
            "holdings",
            tmx_directory.EtfHoldingsFetcher(),
            {"symbol": "xyz"},
            directory_rows._etf_directory_document,
            3,
            "https://dgr53wu9i7rmp.cloudfront.net/etfs/etfs.json",
        ),
        (
            "bonds",
            tmx_directory.BondPricesFetcher(),
            {"isin": "CA90000001X0", "lei": None, "issuer_name": "cORPORATION"},
            directory_rows._bond_listing_document,
            1,
            "https://bondtradedata.iiroc.ca/debtip/designatedbonds/list",
        ),
        (
            "treasuries",
            tmx_directory.TreasuryPricesFetcher(),
            {"govt_type": "federal"},
            directory_rows._bond_listing_document,
            20,
            "https://bondtradedata.iiroc.ca/debtip/designatedbonds/list",
        ),
        (
            "indices",
            tmx_directory.AvailableIndicesFetcher(),
            {},
            directory_rows._index_directory_document,
            3,
            "https://tmxinfoservices.com/files/indices/sptsx-indices.json",
        ),
        (
            "index-sectors",
            tmx_directory.IndexSectorsFetcher(),
            {"symbol": "CUX"},
            directory_rows._index_directory_document,
            3,
            "https://tmxinfoservices.com/files/indices/sptsx-indices.json",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and not value.startswith("https") else None,
)
def test_tmx_all_nine_directory_fetchers_map_exact_source_rows(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    count: int,
    url: str,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_directory, builder())
    rows = _wave_fetch(fetcher, query)
    assert len(rows) == count
    if name == "info":
        assert rows[0].model_dump() == _expected_etf_profile()
    elif name == "search":
        expected = _expected_etf_profile()
        for key in ("domicile", "website", "description"):
            del expected[key]
        expected.update(
            inception_date="2012-03-15",
            short_name="XYZ",
            beta_1y=0.91,
            beta_3y=0.95,
            beta_5y=1.02,
            beta_10y=1.05,
            beta_15y=1.08,
        )
        assert rows[0].model_dump() == expected
        # ABC also matches through its objective's "long-term growth" text.
        assert [row.symbol for row in rows] == ["XYZ", "ABC", "QDF"]
    elif name == "sectors":
        assert [row.model_dump() for row in rows] == [
            {"sector": "Financials", "weight": 60.5, "symbol": "XYZ.TSX"},
            {"sector": "Technology", "weight": 39.5, "symbol": "XYZ.TSX"},
        ]
    elif name == "countries":
        assert [row.model_dump() for row in rows] == [
            {"country": "Canada", "weight": 88.1, "symbol": "XYZ"},
            {"country": "United States", "weight": 11.9, "symbol": "XYZ"},
        ]
    elif name == "holdings":
        assert rows[0].model_dump() == {
            "symbol": "SHOP",
            "name": "Holding 1",
            "weight": 2.4,
            "shares": 10000,
            "market_value": None,
            "currency": "CAD",
            "share_percentage": 0.24,
            "share_change": -1.5,
            "country": "Canada",
            "exchange": "TOR",
            "type_id": "EQ",
            "fund_id": "FUND-1",
        }
        assert rows[1].share_change is None and rows[1].market_value == 1250001.5
    elif name == "bonds":
        assert rows[0].model_dump() == {
            "isin": "CA90000001X0",
            "lei": None,
            "figi": "BB00000001",
            "cusip": "123450110",
            "coupon_rate": 1.75,
            "ytm": 3.5,
            "price": 98.55,
            "highest_price": 101.2,
            "lowest_price": 95.4,
            "total_trades": 5,
            "last_traded_date": date(2026, 10, 15),
            "maturity_date": date(2028, 2, 28),
            "issue_date": date(2011, 2, 15),
            "issuer_name": "Corporation Bond 1",
        }
    elif name == "treasuries":
        assert rows[0].model_dump() == {
            "issuer_name": "Federal Government of Canada Debt",
            "cusip": "123450010",
            "isin": "CA90000000X0",
            "security_type": "Bond",
            "issue_date": date(2010, 1, 15),
            "maturity_date": date(2027, 1, 28),
            "call_date": None,
            "bid": None,
            "offer": None,
            "eod_price": 99.3,
            "last_traded_date": date(2026, 10, 15),
            "total_trades": 4,
            "last_price": 98.5,
            "highest_price": 101.2,
            "lowest_price": None,
            "rate": 1.5,
            "ytm": 3.4,
        }
    elif name == "indices":
        assert [row.model_dump() for row in rows] == [
            {
                "symbol": "CUX",
                "name": "S&P/TSX Composite Index",
                "exchange": None,
                "currency": "CAD",
            },
            {
                "symbol": "SPT",
                "name": "S&P/TSX Capped Energy Index (USD)",
                "exchange": None,
                "currency": "USD",
            },
            {
                "symbol": "QDF",
                "name": "S&P/TSX Growth Income Index",
                "exchange": None,
                "currency": "CAD",
            },
        ]
    else:
        assert [row.model_dump() for row in rows] == [
            {"sector": "Financials", "weight": 32.4},
            {"sector": "Industrials", "weight": 17.1},
            {"sector": "Technology", "weight": 14.8},
        ]
    _assert_wave_requests(transport, 1)
    assert transport.requests[0]["method"] == "GET" and transport.requests[0]["url"] == url
    assert transport.requests[0].get("json") is None
    assert transport.requests[0]["headers"]["Accept"] == "application/json"


@pytest.mark.parametrize(
    "fetcher,builder",
    [
        (tmx_directory.EtfInfoFetcher(), directory_rows._etf_directory_document),
        (tmx_directory.EtfSectorsFetcher(), directory_rows._etf_directory_document),
        (tmx_directory.EtfCountriesFetcher(), directory_rows._etf_directory_document),
        (tmx_directory.EtfHoldingsFetcher(), directory_rows._etf_directory_document),
        (tmx_directory.IndexSectorsFetcher(), directory_rows._index_directory_document),
    ],
)
def test_tmx_directory_missing_symbol_is_empty_not_invented(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    builder: Any,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_directory, builder())
    assert _wave_fetch(fetcher, {"symbol": "ABSENT"}) == ()
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "fetcher,key",
    [
        (tmx_directory.EtfSectorsFetcher(), "sectors"),
        (tmx_directory.EtfCountriesFetcher(), "regions"),
        (tmx_directory.EtfHoldingsFetcher(), "top10holdings"),
    ],
)
def test_tmx_etf_missing_nested_lists_publish_no_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    key: str,
) -> None:
    document = directory_rows._etf_directory_document()
    document["responses"][0]["body"][0].pop(key)
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    assert _wave_fetch(fetcher, {"symbol": "XYZ"}) == ()
    _assert_wave_requests(transport, 1)


def test_tmx_etf_holdings_sharechange_precedence_fallback_and_na_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = directory_rows._etf_directory_document()
    holdings = document["responses"][0]["body"][0]["top10holdings"]
    holdings[0].update(shareChange=999)
    holdings[1].pop("sharechange")
    holdings[1].update(shareChange=2.5, marketvalue=" NA ")
    holdings[2].update(sharechange=None, shareChange="-", marketvalue="-")
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(tmx_directory.EtfHoldingsFetcher(), {"symbol": "xyz"})
    assert [row.share_change for row in rows] == [-1.5, 2.5, None]
    assert [row.market_value for row in rows] == [None, None, None]
    assert [row.shares for row in rows] == [10000, 10001, 10002]
    _assert_wave_requests(transport, 1)


def test_tmx_etf_info_absent_altdata_and_hyphen_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = directory_rows._etf_directory_document()
    record = document["responses"][0]["body"][0]
    record.update(symbol="XYZ.UN")
    record.pop("altData")
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(tmx_directory.EtfInfoFetcher(), {"symbol": "xyz-un.TO", "use_cache": True})
    assert len(rows) == 1 and rows[0].symbol == "XYZ.UN"
    assert rows[0].issuer is None and rows[0].mer is None and rows[0].website is None
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "key", ["longname", "shortname", "investmentstyle", "prospectobjective", "symbol"]
)
def test_tmx_etf_search_screens_each_source_keyword_column_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    document = directory_rows._etf_directory_document()
    record = document["responses"][0]["body"][0]
    record[key] = "Unique-Needle"
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(tmx_directory.EtfSearchFetcher(), {"query": " unique-NEEDLE "})
    assert len(rows) == 1
    assert rows[0].symbol == ("Unique-Needle" if key == "symbol" else "XYZ")
    assert rows[0].unit_price == 21.44
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "column,values,expected",
    [
        ("aum", [2.0, 100.0, 11.0], ["ABC", "QDF", "XYZ"]),
        ("symbol", ["XYZ", "ABC", "QDF"], ["XYZ", "QDF", "ABC"]),
        ("esg", [False, True, False], ["ABC", "XYZ", "QDF"]),
    ],
)
def test_tmx_etf_search_orders_numeric_text_and_boolean_columns(
    monkeypatch: pytest.MonkeyPatch,
    column: str,
    values: list[object],
    expected: list[str],
) -> None:
    document = directory_rows._etf_directory_document()
    for record, value in zip(document["responses"][0]["body"], values, strict=True):
        record[column] = value
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(tmx_directory.EtfSearchFetcher(), {"query": "ETF", "sort_by": column})
    assert [row.symbol for row in rows] == expected
    assert [getattr(row, column) for row in rows] == sorted(values, reverse=True)
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("query", [{"query": " "}, {"query": "ETF", "sort_by": "missing-column"}])
def test_tmx_etf_search_refuses_unsupported_keyword_and_sort(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_directory, directory_rows._etf_directory_document())
    with pytest.raises(TmxProviderError, match="TMX_BAD_FILTER"):
        _wave_fetch(tmx_directory.EtfSearchFetcher(), query)
    _assert_wave_requests(transport, 1)


def _replace_fixture_cell(body: Any, path: tuple[Any, ...], value: object) -> None:
    for key in path[:-1]:
        body = body[key]
    body[path[-1]] = value


@pytest.mark.parametrize(
    "fetcher,builder,query,path,value,code",
    [
        (
            tmx_directory.EtfInfoFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "altData"),
            [],
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.EtfInfoFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "avgdailyvolume"),
            1.5,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfInfoFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "inceptiondate"),
            "2012-02-30",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfSearchFetcher(),
            directory_rows._etf_directory_document,
            {"query": "growth"},
            (0, "totalreturn1month"),
            "1.2",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfSectorsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "sectors"),
            {},
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.EtfSectorsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "sectors", 0),
            1,
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.EtfSectorsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "sectors", 0, "percent"),
            "60.5",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfCountriesFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "regions", 0, "name"),
            5,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfCountriesFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "regions", 0),
            [],
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.EtfHoldingsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "top10holdings", 0, "weighting"),
            "2.4",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfHoldingsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "top10holdings", 0, "numberofshares"),
            1.5,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.EtfHoldingsFetcher(),
            directory_rows._etf_directory_document,
            {"symbol": "XYZ"},
            (0, "top10holdings"),
            {},
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.BondPricesFetcher(),
            directory_rows._bond_listing_document,
            {"isin": "CA90000000X0"},
            (0, "couponRate"),
            "1.5",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.BondPricesFetcher(),
            directory_rows._bond_listing_document,
            {"isin": "CA90000000X0"},
            (0, "totalTrades"),
            1.5,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.BondPricesFetcher(),
            directory_rows._bond_listing_document,
            {"isin": "CA90000000X0"},
            (0, "maturityDate"),
            2027,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.TreasuryPricesFetcher(),
            directory_rows._bond_listing_document,
            {"govt_type": "federal"},
            (0, "maturityDate"),
            "28/01/2027",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.AvailableIndicesFetcher(),
            directory_rows._index_directory_document,
            {},
            ("indices", "CUX", "name_en"),
            5,
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.AvailableIndicesFetcher(),
            directory_rows._index_directory_document,
            {},
            ("indices", "CUX"),
            [],
            "TMX_BAD_ROWS",
        ),
        (
            tmx_directory.IndexSectorsFetcher(),
            directory_rows._index_directory_document,
            {"symbol": "CUX"},
            ("indices", "CUX", "sectors", 0, "weight"),
            "32.4",
            "TMX_BAD_ROW",
        ),
        (
            tmx_directory.IndexSectorsFetcher(),
            directory_rows._index_directory_document,
            {"symbol": "CUX"},
            ("indices", "CUX", "sectors", 0),
            None,
            "TMX_BAD_ROWS",
        ),
    ],
)
def test_tmx_directory_refuses_invalid_nested_records_and_strict_source_cells(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    builder: Any,
    query: dict[str, object],
    path: tuple[Any, ...],
    value: object,
    code: str,
) -> None:
    document = builder()
    _replace_fixture_cell(document["responses"][0]["body"], path, value)
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    with pytest.raises(TmxProviderError, match=code) as exc:
        _wave_fetch(fetcher, query)
    assert exc.value.code == code
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("body", [{}, [1], [None]])
def test_tmx_etf_directory_refuses_non_object_array_records(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
) -> None:
    document = directory_rows._etf_directory_document()
    document["responses"][0]["body"] = body
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_directory.EtfInfoFetcher(), {"symbol": "XYZ"})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("body", [[], {}, {"indices": []}])
def test_tmx_index_directory_refuses_missing_object_section(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
) -> None:
    document = directory_rows._index_directory_document()
    document["responses"][0]["body"] = body
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_directory.AvailableIndicesFetcher(), {})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "query",
    [{"country": "CA"}, {"currency": "CAD"}, {"issued_amount_min": 0}, {"issued_amount_max": 100}],
)
def test_tmx_bond_refuses_unevidenced_filters_instead_of_ignoring_them(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_directory, directory_rows._bond_listing_document())
    with pytest.raises(TmxProviderError, match="TMX_BAD_FILTER"):
        _wave_fetch(tmx_directory.BondPricesFetcher(), query)
    _assert_wave_requests(transport, 1)


def test_tmx_bond_identifier_issuer_and_coupon_bounds_filter_raw_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = directory_rows._bond_listing_document()
    good = directory_rows._bond_record(0)
    good.update(issuer="Corporate Fixture", couponRate=2.0)
    below = {**good, "couponRate": 1.99}
    above = {**good, "couponRate": 2.01}
    invalid = {**good, "couponRate": True}
    unreadable = {**good, "couponRate": "N/A"}
    unrelated = {**good, "lei": "OTHER", "maturityDate": "invalid-but-filtered"}
    document["responses"][0]["body"] = [below, above, invalid, unreadable, unrelated, good]
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(
        tmx_directory.BondPricesFetcher(),
        {
            "isin": "ca900",
            "lei": "lei000000",
            "issuer_name": "CORPORATE",
            "coupon_min": 2.0,
            "coupon_max": 2.0,
        },
    )
    assert len(rows) == 1 and rows[0].coupon_rate == 2.0
    assert rows[0].isin == "CA90000000X0" and rows[0].lei == "LEI000000"
    assert rows[0].figi is None and rows[0].lowest_price is None
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "fetcher", [tmx_directory.BondPricesFetcher(), tmx_directory.TreasuryPricesFetcher()]
)
@pytest.mark.parametrize(
    "query,expected",
    [
        ({}, ["EARLY", "BOUNDARY", "LATE", "UNKNOWN"]),
        ({"maturity_date_min": "2030-01-01"}, ["BOUNDARY", "LATE"]),
        ({"maturity_date_max": "2030-01-01"}, ["EARLY", "BOUNDARY"]),
        ({"issue_date_min": "2020-01-01"}, ["BOUNDARY", "LATE"]),
        ({"issue_date_max": "2020-01-01"}, ["EARLY", "BOUNDARY"]),
        ({"last_traded_min": "2026-10-09"}, ["BOUNDARY", "LATE"]),
        (
            {
                "issue_date_min": "2020-01-01",
                "issue_date_max": "2020-01-01",
                "maturity_date_min": "2030-01-01",
                "maturity_date_max": "2030-01-01",
                "last_traded_min": "2026-10-09",
            },
            ["BOUNDARY"],
        ),
    ],
)
def test_tmx_bond_and_treasury_date_windows_are_inclusive_and_drop_missing_dates(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    expected: list[str],
) -> None:
    document = directory_rows._bond_listing_document()
    base = directory_rows._bond_record(0)
    document["responses"][0]["body"] = [
        {
            **base,
            "isin": "EARLY",
            "maturityDate": "2029-12-31",
            "originalIssueDate": "2019-12-31",
            "lastTradedDate": "2026-10-08",
        },
        {
            **base,
            "isin": "BOUNDARY",
            "maturityDate": "2030-01-01",
            "originalIssueDate": "2020-01-01",
            "lastTradedDate": "2026-10-09",
        },
        {
            **base,
            "isin": "LATE",
            "maturityDate": "2030-01-02",
            "originalIssueDate": "2020-01-02",
            "lastTradedDate": "2026-10-10",
        },
        {
            **base,
            "isin": "UNKNOWN",
            "maturityDate": "N/A",
            "originalIssueDate": "NA",
            "lastTradedDate": "-",
        },
    ]
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(fetcher, query)
    assert [row.isin for row in rows] == expected
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "govt_type,expected",
    [
        (None, ["FED", "PROV", "MUNI"]),
        ("federal", ["FED"]),
        ("provincial", ["PROV"]),
        ("municipal", ["MUNI"]),
    ],
)
def test_tmx_treasury_selects_only_requested_government_issuer_tokens(
    monkeypatch: pytest.MonkeyPatch,
    govt_type: str | None,
    expected: list[str],
) -> None:
    document = directory_rows._bond_listing_document()
    base = directory_rows._bond_record(0)
    document["responses"][0]["body"] = [
        {**base, "isin": "FED", "issuer": "fEDERAL Government"},
        {**base, "isin": "PROV", "issuer": "Provincial Debt"},
        {**base, "isin": "MUNI", "issuer": "Municipal Note"},
        {"isin": "CORP", "issuer": "Corporate", "maturityDate": "bad-but-filtered"},
        {"isin": "NO-ISSUER", "issuer": None},
    ]
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(tmx_directory.TreasuryPricesFetcher(), {"govt_type": govt_type})
    assert [row.isin for row in rows] == expected
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("family", ["etf", "bond", "treasury"])
def test_tmx_directory_filters_before_real_ten_thousand_record_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    if family == "etf":
        document = directory_rows._etf_directory_document()
        selected = document["responses"][0]["body"][0]
        noise = {"symbol": "NOISE", "longname": "Ignore", "avgdailyvolume": "corrupt-but-filtered"}
        fetcher = tmx_directory.EtfSearchFetcher()
        query = {"query": "growth"}
        overlimit_query = {"query": "Ignore"}
    else:
        document = directory_rows._bond_listing_document()
        selected = document["responses"][0]["body"][0]
        noise = {"issuer": "Corporate Noise", "isin": "NOISE", "maturityDate": "bad-but-filtered"}
        fetcher = (
            tmx_directory.BondPricesFetcher()
            if family == "bond"
            else tmx_directory.TreasuryPricesFetcher()
        )
        query = {"isin": "CA90000000X0"} if family == "bond" else {"govt_type": "federal"}
        overlimit_query = {}
    document["responses"][0]["body"] = [noise.copy() for _ in range(10001)] + [selected]
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    rows = _wave_fetch(fetcher, query)
    assert len(rows) == 1
    assert (rows[0].symbol if family == "etf" else rows[0].isin) == (
        "XYZ" if family == "etf" else "CA90000000X0"
    )
    _assert_wave_requests(transport, 1)
    if family == "treasury":
        # Give the oversized raw set the selection's own government token.
        for record in document["responses"][0]["body"][:-1]:
            record["issuer"] = "Federal Noise"
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(fetcher, overlimit_query)
    _assert_wave_requests(transport, 1)


def test_tmx_available_indices_refuses_directory_over_real_row_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = directory_rows._index_directory_document()
    document["responses"][0]["body"]["indices"] = {
        str(index): {"name_en": "Index"} for index in range(10001)
    }
    transport = _WaveTransport(monkeypatch, tmx_directory, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_directory.AvailableIndicesFetcher(), {})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "name,fetcher,query,builder,operation,variables",
    [
        (
            "history",
            tmx_graphql.EtfHistoricalFetcher(),
            {"symbol": "ZWB", "start_date": "2026-10-09", "end_date": "2026-10-09"},
            graphql_rows._etf_historical_document,
            "getCompanyPriceHistory",
            {
                "symbol": "ZWB",
                "start": "2026-10-09",
                "end": "2026-10-09",
                "adjusted": False,
                "adjustmentType": None,
                "unadjusted": None,
                "limit": 1,
            },
        ),
        (
            "earnings",
            tmx_graphql.CalendarEarningsFetcher(),
            {"start_date": "2026-10-09", "end_date": "2026-10-09"},
            graphql_rows._calendar_earnings_document,
            "getEnhancedEarningsForDate",
            {"date": "2026-10-09"},
        ),
        (
            "news",
            tmx_graphql.CompanyNewsFetcher(),
            {"symbol": "shop", "page": 3, "limit": 2},
            graphql_rows._company_news_document,
            "getNewsAndEvents",
            {"symbol": "shop", "page": 3, "limit": 2, "locale": "en"},
        ),
        (
            "gainers",
            tmx_graphql.EquityGainersFetcher(),
            {},
            graphql_rows._equity_gainers_document,
            "getStockListSymbolsWithQuote",
            {"stockListId": "TOP_PRICE_PERFORMER", "locale": "en"},
        ),
        (
            "insiders",
            tmx_graphql.InsiderTradingFetcher(),
            {"symbol": "CNQ", "limit": 4},
            graphql_rows._insider_trading_document,
            "getCompanyInsidersActivities",
            {"symbol": "CNQ"},
        ),
        (
            "summary",
            tmx_graphql.InsiderTradingFetcher(),
            {"symbol": "CNQ", "summary": True, "limit": 1},
            graphql_rows._insider_trading_document,
            "getCompanyInsidersActivities",
            {"symbol": "CNQ"},
        ),
    ],
)
def test_tmx_graphql_five_fetchers_send_exact_variables_and_map_contract_rows(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    operation: str,
    variables: dict[str, object],
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_client, builder())
    rows = _wave_fetch(fetcher, query)
    if name == "history":
        assert [row.model_dump() for row in rows] == [
            {
                "symbol": "ZWB",
                "date": date(2026, 10, 9),
                "open": 24.05,
                "high": 24.41,
                "low": 23.98,
                "close": 24.28,
                "volume": 184300,
                "vwap": 24.2105,
                "change": 0.23,
                "change_percent": None,
                "transactions": 612,
                "transactions_value": 4468404.0,
            }
        ]
    elif name == "earnings":
        assert [row.model_dump() for row in rows] == [
            {
                "report_date": date(2026, 10, 9),
                "symbol": "SHOP",
                "name": "Shopify Inc.",
                "eps_previous": None,
                "eps_consensus": 1.42,
                "eps_actual": 1.51,
                "eps_surprise": 0.09,
                "surprise_percent": 0.0634,
                "reporting_time": "BMO",
            },
            {
                "report_date": date(2026, 10, 9),
                "symbol": "CNQ",
                "name": "Canadian Natural Resources Limited",
                "eps_previous": None,
                "eps_consensus": 1.42,
                "eps_actual": 1.51,
                "eps_surprise": 0.09,
                "surprise_percent": 0.0634,
                "reporting_time": "BMO",
            },
        ]
    elif name == "news":
        assert len(rows) == 2
        assert rows[0].model_dump() == {
            "date": datetime(2026, 10, 9, 16, 30, tzinfo=timezone.utc),
            "title": "Shopify beats quarterly estimates",
            "url": "https://money.tmx.com/quote/SHOP/news/2026-10488123",
            "author": None,
            "excerpt": "Shopify beats quarterly estimates (continued)",
            "body": None,
            "images": None,
            "symbols": "shop",
            "source": "TMX Newsroom",
        }
        assert rows[1].url == "https://money.tmx.com/quote/SHOP/news/2026-10488098"
    elif name == "gainers":
        assert len(rows) == 2
        assert rows[0].model_dump() == {
            "symbol": "SHOP",
            "name": "Shopify Inc.",
            "rank": 1,
            "price": 41.07,
            "change": 1.36,
            "percent_change": 0.0342,
            "volume": 2310400,
            "thirty_day_price_change": None,
            "ninety_day_price_change": None,
            "dividend_yield": None,
            "avg_volume_10d": None,
        }
        assert rows[1].symbol == "CNQ" and rows[1].rank == 2
    elif name == "insiders":
        assert [row.model_dump(exclude_none=True) for row in rows] == [
            {
                "period": period,
                "symbol": "CNQ",
                "owner_name": owner,
                "transaction_type": direction,
                "acquisition_or_disposition": direction,
                "number_of_trades": 3,
                "securities_transacted": 30000,
                "securities_owned": 400000,
                "trade_value": 1200000.0,
            }
            for period in ("3M", "6M")
            for direction, owner in (("buy", "A. Buyer"), ("sell", "A. Seller"))
        ]
        assert all(row.filing_date is None and row.transaction_date is None for row in rows)
    else:
        assert [row.model_dump(exclude_none=True) for row in rows] == [
            {
                "period": "3M",
                "symbol": "CNQ",
                "securities_bought": 30000,
                "securities_sold": 30000,
                "net_activity": 0,
                "securities_transacted": 60000,
            }
        ]
    _assert_wave_requests(transport, 1)
    request = transport.requests[0]
    assert request["method"] == "POST" and request["url"] == tmx_client.GRAPHQL_URL
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["json"]["operationName"] == operation
    assert request["json"]["variables"] == variables


@pytest.mark.parametrize(
    "category,list_id",
    [
        ("volume", "TOP_VOLUME"),
        ("tech", "TOP_TECH"),
        ("52w_high", "TOP_WEEK_52_HIGH"),
        ("rising_stars", "RISING_STARS"),
    ],
)
def test_tmx_gainers_nondefault_categories_request_and_verify_their_list_id(
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    list_id: str,
) -> None:
    document = graphql_rows._equity_gainers_document()
    document["responses"][0]["body"]["data"]["stockList"]["stockListId"] = list_id
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    rows = _wave_fetch(tmx_graphql.EquityGainersFetcher(), {"category": category})
    assert [(row.symbol, row.rank) for row in rows] == [("SHOP", 1), ("CNQ", 2)]
    assert transport.requests[0]["json"]["variables"] == {"stockListId": list_id, "locale": "en"}
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "fetcher,query,code",
    [
        (
            tmx_graphql.CalendarEarningsFetcher(),
            {"start_date": "2026-10-09", "symbol": "SHOP"},
            "TMX_BAD_FILTER",
        ),
        (tmx_graphql.EquityGainersFetcher(), {"sort": "volume"}, "TMX_BAD_SORT"),
        (tmx_graphql.EquityGainersFetcher(), {"symbol": "SHOP"}, "TMX_BAD_FILTER"),
        (tmx_graphql.EquityGainersFetcher(), {"category": "unknown"}, "TMX_BAD_FILTER"),
    ],
)
def test_tmx_graphql_refuses_unsupported_filters_before_sending(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    code: str,
) -> None:
    transport = _WaveTransport(monkeypatch, tmx_client, {"responses": []})
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(fetcher, query)
    assert transport.requests == []


@pytest.mark.parametrize(
    "fetcher,query",
    [
        (
            tmx_graphql.CalendarEarningsFetcher(),
            {"start_date": "2026-10-09", "end_date": "2026-10-10"},
        ),
        (tmx_graphql.CompanyNewsFetcher(), {"symbol": " SHOP "}),
        (tmx_graphql.CompanyNewsFetcher(), {"symbol": "SHOP", "page": 0}),
        (tmx_graphql.InsiderTradingFetcher(), {"symbol": "CNQ", "summary": "true"}),
        (
            tmx_graphql.EtfHistoricalFetcher(),
            {"symbol": "ZWB", "start_date": "2026-10-10", "end_date": "2026-10-09"},
        ),
        (tmx_envelope.OptionsChainsFetcher(), {"symbol": "SHOP", "limit": 1.5}),
        (tmx_envelope.IndexSnapshotsFetcher(), {"region": "asia"}),
    ],
)
def test_tmx_wave_query_validation_fails_without_any_http_attempt(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
) -> None:
    # Both possible transport modules are offline even if validation accidentally starts I/O.
    transport = _WaveTransport(monkeypatch, tmx_client, {"responses": []})
    envelope_transport = _WaveTransport(monkeypatch, tmx_envelope, {"responses": []})
    with pytest.raises(ValidationError):
        _wave_fetch(fetcher, query)
    assert transport.requests == [] and envelope_transport.requests == []


@pytest.mark.parametrize("newsid", [None, "", " id", "id ", 17])
def test_tmx_company_news_refuses_unusable_source_id_not_fabricated_url(
    monkeypatch: pytest.MonkeyPatch,
    newsid: object,
) -> None:
    document = graphql_rows._company_news_document()
    document["responses"][0]["body"]["data"]["news"][0]["newsid"] = newsid
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _wave_fetch(tmx_graphql.CompanyNewsFetcher(), {"symbol": "SHOP"})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("asserted_date", ["2026-10-08", "2026-10-09", None])
def test_tmx_earnings_refuses_source_asserted_report_date_even_when_equal(
    monkeypatch: pytest.MonkeyPatch,
    asserted_date: object,
) -> None:
    document = graphql_rows._calendar_earnings_document()
    document["responses"][0]["body"]["data"]["getEnhancedEarningsForDate"][0]["report_date"] = (
        asserted_date
    )
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _wave_fetch(tmx_graphql.CalendarEarningsFetcher(), {"start_date": "2026-10-09"})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"stockListId": "TOP_VOLUME", "listItems": []},
        {"stockListId": "TOP_PRICE_PERFORMER", "listItems": [1]},
    ],
)
def test_tmx_gainers_refuses_wrong_list_echo_or_list_shape(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    document = graphql_rows._equity_gainers_document()
    document["responses"][0]["body"]["data"]["stockList"] = payload
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_graphql.EquityGainersFetcher(), {})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "summary,section,value",
    [
        (False, "insiderActivities", None),
        (False, "insiderActivities", {}),
        (True, "activitySummary", None),
        (True, "activitySummary", {}),
    ],
)
def test_tmx_insiders_require_only_the_requested_detail_or_summary_section(
    monkeypatch: pytest.MonkeyPatch,
    summary: bool,
    section: str,
    value: object,
) -> None:
    document = graphql_rows._insider_trading_document()
    document["responses"][0]["body"]["data"]["getCompanyInsidersActivities"][section] = value
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_graphql.InsiderTradingFetcher(), {"symbol": "CNQ", "summary": summary})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "direction,value", [("buy", None), ("buy", {}), ("sell", None), ("sell", "missing-array")]
)
def test_tmx_insiders_refuse_missing_or_wrong_typed_direction_arrays(
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
    value: object,
) -> None:
    document = graphql_rows._insider_trading_document()
    activity = document["responses"][0]["body"]["data"]["getCompanyInsidersActivities"][
        "insiderActivities"
    ][0]
    if value is None:
        activity.pop(direction)
    else:
        activity[direction] = value
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _wave_fetch(tmx_graphql.InsiderTradingFetcher(), {"symbol": "CNQ"})
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("summary", [False, True])
def test_tmx_insiders_do_not_require_unrequested_section_and_allow_empty_direction(
    monkeypatch: pytest.MonkeyPatch,
    summary: bool,
) -> None:
    document = graphql_rows._insider_trading_document()
    payload = document["responses"][0]["body"]["data"]["getCompanyInsidersActivities"]
    payload.pop("insiderActivities" if summary else "activitySummary")
    if not summary:
        payload["insiderActivities"][0]["buy"] = []
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    rows = _wave_fetch(tmx_graphql.InsiderTradingFetcher(), {"symbol": "CNQ", "summary": summary})
    assert len(rows) == (1 if summary else 3)
    assert rows[0].period == "3M"
    assert rows[0].securities_transacted == (60000 if summary else 30000)
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("family", ["history", "news", "insiders", "summary"])
def test_tmx_graphql_refuses_overdelivery_instead_of_silent_truncation(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    if family == "history":
        document = graphql_rows._etf_historical_document()
        document["responses"][0]["body"]["data"]["getCompanyPriceHistory"].append(
            graphql_rows._history_row("2026-10-08")
        )
        fetcher = tmx_graphql.EtfHistoricalFetcher()
        query = {"symbol": "ZWB", "start_date": "2026-10-09", "end_date": "2026-10-09"}
    elif family == "news":
        document = graphql_rows._company_news_document()
        fetcher = tmx_graphql.CompanyNewsFetcher()
        query = {"symbol": "SHOP", "limit": 1}
    else:
        document = graphql_rows._insider_trading_document()
        fetcher = tmx_graphql.InsiderTradingFetcher()
        query = {"symbol": "CNQ", "limit": 1, "summary": family == "summary"}
        if family == "summary":
            summaries = document["responses"][0]["body"]["data"]["getCompanyInsidersActivities"][
                "activitySummary"
            ]
            summaries.append({**summaries[0], "periodkey": "6M"})
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(fetcher, query)
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize(
    "fetcher,builder,query,path,value",
    [
        (
            tmx_graphql.EtfHistoricalFetcher(),
            graphql_rows._etf_historical_document,
            {"symbol": "ZWB", "start_date": "2026-10-09", "end_date": "2026-10-09"},
            ("getCompanyPriceHistory", 0, "datetime"),
            "2026-02-30",
        ),
        (
            tmx_graphql.EtfHistoricalFetcher(),
            graphql_rows._etf_historical_document,
            {"symbol": "ZWB", "start_date": "2026-10-09", "end_date": "2026-10-09"},
            ("getCompanyPriceHistory", 0, "openPrice"),
            "24.05",
        ),
        (
            tmx_graphql.CalendarEarningsFetcher(),
            graphql_rows._calendar_earnings_document,
            {"start_date": "2026-10-09"},
            ("getEnhancedEarningsForDate", 0, "estimatedEps"),
            True,
        ),
        (
            tmx_graphql.CompanyNewsFetcher(),
            graphql_rows._company_news_document,
            {"symbol": "SHOP"},
            ("news", 0, "datetime"),
            "2026-10-09T25:00:00Z",
        ),
        (
            tmx_graphql.EquityGainersFetcher(),
            graphql_rows._equity_gainers_document,
            {},
            ("stockList", "listItems", 0, "rank"),
            1.5,
        ),
        (
            tmx_graphql.InsiderTradingFetcher(),
            graphql_rows._insider_trading_document,
            {"symbol": "CNQ"},
            ("getCompanyInsidersActivities", "insiderActivities", 0, "buy", 0, "trades"),
            1.5,
        ),
        (
            tmx_graphql.InsiderTradingFetcher(),
            graphql_rows._insider_trading_document,
            {"symbol": "CNQ", "summary": True},
            ("getCompanyInsidersActivities", "activitySummary", 0, "buyShares"),
            "30000",
        ),
    ],
)
def test_tmx_graphql_refuses_corrupt_mapped_dates_numbers_and_counts(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: Fetcher[Any, Any],
    builder: Any,
    query: dict[str, object],
    path: tuple[Any, ...],
    value: object,
) -> None:
    document = builder()
    _replace_fixture_cell(document["responses"][0]["body"]["data"], path, value)
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROW"):
        _wave_fetch(fetcher, query)
    _assert_wave_requests(transport, 1)


@pytest.mark.parametrize("family", ["history", "earnings", "news", "gainers", "insiders"])
def test_tmx_graphql_preserves_legitimate_empty_source_lists(
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    if family == "history":
        document = graphql_rows._etf_historical_document()
        document["responses"][0]["body"]["data"]["getCompanyPriceHistory"] = []
        fetcher = tmx_graphql.EtfHistoricalFetcher()
        query = {"symbol": "ZWB", "start_date": "2026-10-09", "end_date": "2026-10-09"}
    elif family == "earnings":
        document = graphql_rows._calendar_earnings_document()
        document["responses"][0]["body"]["data"]["getEnhancedEarningsForDate"] = []
        fetcher = tmx_graphql.CalendarEarningsFetcher()
        query = {"start_date": "2026-10-09"}
    elif family == "news":
        document = graphql_rows._company_news_document()
        document["responses"][0]["body"]["data"]["news"] = []
        fetcher = tmx_graphql.CompanyNewsFetcher()
        query = {"symbol": "SHOP"}
    elif family == "gainers":
        document = graphql_rows._equity_gainers_document()
        document["responses"][0]["body"]["data"]["stockList"]["listItems"] = []
        fetcher = tmx_graphql.EquityGainersFetcher()
        query = {}
    else:
        document = graphql_rows._insider_trading_document()
        document["responses"][0]["body"]["data"]["getCompanyInsidersActivities"][
            "insiderActivities"
        ] = []
        fetcher = tmx_graphql.InsiderTradingFetcher()
        query = {"symbol": "CNQ"}
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    assert _wave_fetch(fetcher, query) == ()
    _assert_wave_requests(transport, 1)


_WAVE_TRANSPORT_CASES = (
    (
        tmx_envelope,
        tmx_envelope.OptionsChainsFetcher(),
        {"symbol": "SHOP"},
        envelope_rows._options_chains_document,
        0,
    ),
    (
        tmx_envelope,
        tmx_envelope.OptionsChainsFetcher(),
        {"symbol": "SHOP"},
        envelope_rows._options_chains_document,
        1,
    ),
    (
        tmx_envelope,
        tmx_envelope.IndexSnapshotsFetcher(),
        {},
        envelope_rows._index_snapshots_document,
        1,
    ),
    (
        tmx_directory,
        tmx_directory.EtfInfoFetcher(),
        {"symbol": "XYZ"},
        directory_rows._etf_directory_document,
        0,
    ),
    (
        tmx_client,
        tmx_graphql.CompanyNewsFetcher(),
        {"symbol": "SHOP"},
        graphql_rows._company_news_document,
        0,
    ),
)


@pytest.mark.parametrize("seam,fetcher,query,builder,leg", _WAVE_TRANSPORT_CASES)
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "TMX_ACCESS_ERROR"),
        (403, "TMX_ACCESS_ERROR"),
        (429, "TMX_RATE_LIMITED"),
        (404, "TMX_HTTP_ERROR"),
        (503, "TMX_UPSTREAM_ERROR"),
    ],
)
def test_tmx_wave_http_errors_are_sanitized_and_keep_status(
    monkeypatch: pytest.MonkeyPatch,
    seam: Any,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    leg: int,
    status: int,
    code: str,
) -> None:
    document = builder()
    document["responses"][leg].update(status=status, body=b"private upstream diagnostic")
    document["responses"] = document["responses"][: leg + 1]
    transport = _WaveTransport(monkeypatch, seam, document)
    with pytest.raises(TmxProviderError, match=code) as exc:
        _wave_fetch(fetcher, query)
    assert exc.value.code == code and exc.value.status == status
    assert "private" not in str(exc.value)
    _assert_wave_requests(transport, leg + 1)


@pytest.mark.parametrize("seam,fetcher,query,builder,leg", _WAVE_TRANSPORT_CASES)
@pytest.mark.parametrize(
    "failure,code",
    [
        (requests.Timeout("private timeout"), "TMX_HTTP_ERROR"),
        (RuntimeError("private transport error"), "TMX_TRANSPORT_ERROR"),
    ],
)
def test_tmx_wave_transport_exceptions_are_coded_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
    seam: Any,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    leg: int,
    failure: Exception,
    code: str,
) -> None:
    document = builder()
    document["responses"][leg]["exception"] = failure
    document["responses"] = document["responses"][: leg + 1]
    transport = _WaveTransport(monkeypatch, seam, document)
    with pytest.raises(TmxProviderError, match=code) as exc:
        _wave_fetch(fetcher, query)
    assert exc.value.status is None and "private" not in str(exc.value)
    _assert_wave_requests(transport, leg + 1)


@pytest.mark.parametrize("seam,fetcher,query,builder,leg", _WAVE_TRANSPORT_CASES)
def test_tmx_wave_oversized_actual_response_bytes_refuse_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
    seam: Any,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    leg: int,
) -> None:
    document = builder()
    document["responses"][leg]["body"] = b"x" * (16 * 1024 * 1024 + 1)
    document["responses"] = document["responses"][: leg + 1]
    transport = _WaveTransport(monkeypatch, seam, document)
    with pytest.raises(TmxProviderError, match="TMX_RESPONSE_TOO_LARGE") as exc:
        _wave_fetch(fetcher, query)
    assert exc.value.status == 200
    _assert_wave_requests(transport, leg + 1)


@pytest.mark.parametrize("seam,fetcher,query,builder,leg", _WAVE_TRANSPORT_CASES)
def test_tmx_wave_undecodable_text_and_json_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    seam: Any,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    leg: int,
) -> None:
    document = builder()
    document["responses"][leg]["body"] = b"\xff"
    # Options loads both text envelopes before parsing; invalid UTF-8 refuses in extract.
    document["responses"] = document["responses"][: leg + 1]
    transport = _WaveTransport(monkeypatch, seam, document)
    code = "TMX_BAD_ROWS" if fetcher.canonical_model == "OptionsChains" else "TMX_BAD_JSON"
    with pytest.raises(TmxProviderError, match=code) as exc:
        _wave_fetch(fetcher, query)
    assert exc.value.status == 200
    _assert_wave_requests(transport, leg + 1)


@pytest.mark.parametrize(
    "seam,fetcher,query,builder,leg", _WAVE_TRANSPORT_CASES[:1] + _WAVE_TRANSPORT_CASES[2:]
)
def test_tmx_wave_request_grants_deny_wrong_host_before_session_io(
    monkeypatch: pytest.MonkeyPatch,
    seam: Any,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    builder: Any,
    leg: int,
) -> None:
    del builder, leg
    # The second-leg index case proves its first admitted GET is not a free POST grant.
    if fetcher.canonical_model == "IndexSnapshots":
        document = envelope_rows._index_snapshots_document()
        document["responses"].pop()
        hosts = {"tmxinfoservices.com"}
    else:
        document = {"responses": []}
        hosts = {"unrelated.invalid"}
    transport = _WaveTransport(monkeypatch, seam, document)
    with pytest.raises(RequestBudgetError):
        fetcher.fetch(ctx=_authorized_context(fetcher.canonical_model, hosts=hosts), **query)
    assert len(transport.requests) == (1 if fetcher.canonical_model == "IndexSnapshots" else 0)
    assert not transport.responses


@pytest.mark.parametrize("timeout,expected", [(None, 25.0), (100.0, 25.0), (2.0, 2.0)])
@pytest.mark.parametrize("family", ["envelope", "directory", "graphql"])
def test_tmx_wave_timeouts_use_default_and_clamp_caller_value(
    monkeypatch: pytest.MonkeyPatch,
    timeout: float | None,
    expected: float,
    family: str,
) -> None:
    if family == "envelope":
        seam, fetcher, query, builder = (
            tmx_envelope,
            tmx_envelope.OptionsChainsFetcher(),
            {"symbol": "SHOP"},
            envelope_rows._options_chains_document,
        )
    elif family == "directory":
        seam, fetcher, query, builder = (
            tmx_directory,
            tmx_directory.EtfInfoFetcher(),
            {"symbol": "XYZ"},
            directory_rows._etf_directory_document,
        )
    else:
        seam, fetcher, query, builder = (
            tmx_client,
            tmx_graphql.CompanyNewsFetcher(),
            {"symbol": "SHOP"},
            graphql_rows._company_news_document,
        )
    transport = _WaveTransport(monkeypatch, seam, builder())
    rows = _wave_fetch(fetcher, query, timeout=timeout)
    assert rows
    assert not transport.responses
    assert all(request["timeout"] == expected for request in transport.requests)


@pytest.mark.parametrize(
    "body,code",
    [
        (b"{bad", "TMX_BAD_JSON"),
        ({"errors": [{"message": "private"}]}, "TMX_GRAPHQL_ERROR"),
        ({"data": {}}, "TMX_BAD_ENVELOPE"),
        (_data("news", [None]), "TMX_BAD_ROWS"),
    ],
)
def test_tmx_new_graphql_fetcher_refuses_bad_envelopes_and_row_arrays(
    monkeypatch: pytest.MonkeyPatch,
    body: object,
    code: str,
) -> None:
    document = graphql_rows._company_news_document()
    document["responses"][0]["body"] = body
    transport = _WaveTransport(monkeypatch, tmx_client, document)
    with pytest.raises(TmxProviderError, match=code):
        _wave_fetch(tmx_graphql.CompanyNewsFetcher(), {"symbol": "SHOP"})
    _assert_wave_requests(transport, 1)


def test_tmx_index_quote_panel_refuses_real_over_ceiling_row_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = envelope_rows._index_snapshots_document()
    document["responses"][1]["body"]["data"]["getQuoteForSymbols"] = [
        {"symbol": "CUX", "price": 1} for _ in range(10001)
    ]
    transport = _WaveTransport(monkeypatch, tmx_envelope, document)
    with pytest.raises(TmxProviderError, match="TMX_BAD_ROWS"):
        _wave_fetch(tmx_envelope.IndexSnapshotsFetcher(), {})
    _assert_wave_requests(transport, 2)
