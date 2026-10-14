"""The Intrinio provider's existing declarations and clean typed market bindings.

Every case drives a generated fetcher through the engine's transport seam with a hand-written
fixture document under the cited row key (``pairs``, ``companies``, ``stock_price_adjustments``,
``initial_public_offerings``), with an API key planted in the environment. No case touches a
socket or a file.

The fixtures carry the ``name`` column on one record and omit it on another, because that is the
face the contract has: ``name`` is an inherited optional standard-model field published under its
own spelling (currency_pairs.py:21 / equity_search.py:24, neither aliased by the provider), so a
record without it must come back null rather than refuse.

``HistoricalDividends`` is the model whose published ``symbol`` no record carries: upstream prepends
the queried symbol to every item (historical_dividends.py:102-104), and the declaration reproduces
that with a ``ModelSpec.column_derives`` plan over the validated parameter. Its cases below are the
witness that the stamp reaches the row -- and that a record the contract requires to carry a
``dividend`` still refuses when it does not.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import requests

from opendata.data import http_client
from opendata.data.http_client import GovernedHttpClient
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    make_http_json_fetcher,
)
from opendata.data.providers._engine.spec import PaginationSpec
from opendata.data.providers._engine.testing import (
    FixedResponseTransport,
    SequencedResponseTransport,
    fixture_context,
)
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.models.calendar_ipo import IntrinioCalendarIpoFetcher
from opendata.data.providers.intrinio.models.currency_pairs import IntrinioCurrencyPairsFetcher
from opendata.data.providers.intrinio.models.equity_search import IntrinioEquitySearchFetcher
from opendata.data.providers.intrinio.models.historical_dividends import (
    IntrinioHistoricalDividendsFetcher,
)
from opendata.data.providers.intrinio.models.insider_trading import IntrinioInsiderTradingFetcher
from opendata.data.providers.intrinio.specs import (
    CALENDAR_IPO,
    CURRENCY_PAIRS,
    EQUITY_SEARCH,
    HISTORICAL_DIVIDENDS,
    INSIDER_TRADING,
)
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import RequestAttemptLimitError
from opendata.services import provider_model_query

if TYPE_CHECKING:
    from opendata.data.providers._engine.spec import ModelSpec

_EXPECTED_INTRINIO_MODEL_IDS = frozenset(
    {
        "CurrencyPairs",
        "EquitySearch",
        "HistoricalDividends",
        "CalendarIpo",
        "InsiderTrading",
        "EquityInfo",
        "EquityHistorical",
        "EtfInfo",
        "HistoricalMarketCap",
        "IndexHistorical",
        "EquityQuote",
        "EtfHistorical",
        "EtfHoldings",
        "EtfSearch",
        "OptionsUnusual",
        "OptionsChains",
        "PriceTargetConsensus",
        "ShareStatistics",
        "CompanyNews",
        "WorldNews",
        "CompanyFilings",
        "SearchAttributes",
        "LatestAttributes",
        "HistoricalAttributes",
        "FredSeries",
        "BalanceSheet",
        "CashFlowStatement",
        "IncomeStatement",
        "FinancialRatios",
        "KeyMetrics",
        "ReportedFinancials",
        "ForwardEbitdaEstimates",
        "ForwardEpsEstimates",
        "ForwardPeEstimates",
        "ForwardSalesEstimates",
        "EtfPricePerformance",
        "MarketSnapshots",
    }
)

PAIRS_URL = "https://api-v2.intrinio.com/forex/pairs"
SEARCH_URL = "https://api-v2.intrinio.com/companies/search"
ADJUSTMENTS_URL = "https://api-v2.intrinio.com/securities/AAPL/prices/adjustments"
IPO_URL = "https://api-v2.intrinio.com/companies/ipos"

PAIRS_DOCUMENT = {
    "pairs": [
        {
            "code": "USDCAD",
            "name": "US Dollar / Canadian Dollar",
            "base_currency": "USD",
            "quote_currency": "CAD",
        },
        {"code": "EURUSD", "base_currency": "EUR", "quote_currency": "USD"},
        {
            "code": "GBPAUD",
            "name": "British Pound / Australian Dollar",
            "base_currency": "GBP",
            "quote_currency": "AUD",
        },
    ]
}

COMPANIES_DOCUMENT = {
    "companies": [
        {
            "id": "c0ffee",
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "cik": "320193",
            "lei": "LEI-AAPL",
        },
    ]
}

#: Contract-legal adjustment records: every one carries the two keys the standard model requires
#: (``date`` and ``dividend``, standard_models/historical_dividends.py:38-41), the three renamed
#: keys, and the optional ``factor``/``split_ratio`` the provider adds. No body of this endpoint has
#: been captured in this repo, so this fixture is the cited contract, not an observed document --
#: and the refusal case below is what keeps that honest: a record without ``dividend`` must refuse.
ADJUSTMENTS_DOCUMENT = {
    "stock_price_adjustments": [
        {"date": "2024-03-15", "dividend": 0.25, "dividend_currency": "USD", "factor": 0.9985},
        {"date": "2020-08-31", "dividend": 0.2, "split_ratio": 4.0, "factor": 0.25},
    ],
    "next_page": None,
}

#: The cited contract's item shape (calendar_ipo.py:45-155): flat offer/trade scalars plus nested
#: ``company`` and ``security`` objects. The second item omits both objects and most scalars,
#: because every upstream field is optional and absence must read back as null, not refusal.
IPO_DOCUMENT = {
    "initial_public_offerings": [
        {
            "ticker": "ARM",
            "date": "2023-09-14",
            "status": "priced",
            "exchange": "NASDAQ",
            "offer_amount": 126900000.0,
            "share_price": 51.0,
            "share_count": 2490000,
            "open_price": 66.99,
            "volume": 18000000,
            "day_change": 31.35,
            "id": "ipo-1",
            "company": {
                "id": "co-1",
                "ticker": "ARM",
                "name": "Arm Holdings plc",
                "lei": "LEI-ARM",
                "cik": "1975112",
            },
            "security": {
                "id": "sec-1",
                "name": "Ordinary Shares",
                "code": "00B",
                "currency": "USD",
                "ticker": "ARM",
                "composite_ticker": "ARM:NASDAQ",
                "figi": "FIGI-1",
                "primary_listing": True,
            },
        },
        {"ticker": "NEWCO", "date": "2024-01-05", "status": "upcoming"},
    ],
    "next_page": None,
}


@pytest.fixture(autouse=True)
def _intrinio_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local-only key; the synthetic transport never reaches a socket with it."""
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")


def _run(spec, document=None, **query):
    """Drive one declared model over one hand-written document."""
    body = PAIRS_DOCUMENT if document is None else document
    transport = FixedResponseTransport(HttpResponse(200, body))
    if spec is CURRENCY_PAIRS:
        cls = IntrinioCurrencyPairsFetcher
    elif spec is HISTORICAL_DIVIDENDS:
        cls = IntrinioHistoricalDividendsFetcher
    else:
        cls = make_http_json_fetcher(SOURCE, spec)
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), **query))
    return transport, rows


def _run_pages(spec, pages, *, sends=3, **query):
    """Drive one declared model over a finite sequence of cursor pages."""
    transport = SequencedResponseTransport(*pages)
    cls = (
        IntrinioHistoricalDividendsFetcher
        if spec is HISTORICAL_DIVIDENDS
        else make_http_json_fetcher(SOURCE, spec)
    )
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=sends), **query))
    return transport, rows


class _JsonSession(requests.Session):
    """A finite fake wire session which still exercises GovernedHttpClient accounting."""

    def __init__(self, documents: list[object]) -> None:
        super().__init__()
        self.documents = list(documents)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.documents:
            raise AssertionError("unexpected extra governed HTTP request")
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(self.documents.pop(0), allow_nan=False).encode("utf-8")
        return response


def _run_governed_pages(monkeypatch, fetcher_type, spec, pages, *, sends=3, **query):
    """Run pages through the governed client with an offline requests-compatible session."""
    session = _JsonSession(pages)
    client = GovernedHttpClient(session=session)
    monkeypatch.setattr(http_client, "get_shared_http_client", lambda: client)
    ctx = fixture_context(SOURCE, spec, sends=sends)
    rows = list(fetcher_type().fetch(ctx=ctx, **query))
    return session, ctx, rows


class TestRegistration:
    """intrinio is a real provider now, not a reserved catalog identity."""

    def test_catalog_no_longer_reserves_intrinio(self) -> None:
        provider = get_provider("intrinio")
        assert "reserved" not in provider.description.lower()
        binding_ids = {
            model_id
            for binding in provider.fetcher_bindings
            for model_id in (binding.canonical_model_ids or (binding.model_key,))
        }
        assert binding_ids == _EXPECTED_INTRINIO_MODEL_IDS
        assert len(provider.fetcher_bindings) == 37
        assert "OptionsSnapshots" not in binding_ids
        assert provider.credentials[0].environment_variable == "INTRINIO_API_KEY"

    def test_registry_publishes_the_currently_routable_models(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("intrinio", registry)
        domains = {c.domain for c in capabilities}
        assert domains == {
            "intrinio_currency_pairs",
            "intrinio_equity_search",
            "intrinio_historical_dividends",
            "intrinio_calendar_ipo",
            "intrinio_insider_trading",
            "intrinio_equity_info",
            "intrinio_equity_historical",
            "intrinio_etf_info",
            "intrinio_historical_market_cap",
            "intrinio_index_historical",
            "intrinio_equity_quote",
            "intrinio_etf_historical",
            "intrinio_etf_holdings",
            "intrinio_etf_search",
            "intrinio_options_unusual",
            "intrinio_options_chains",
            "intrinio_price_target_consensus",
            "intrinio_share_statistics",
            "intrinio_company_news",
            "intrinio_world_news",
            "intrinio_company_filings",
            "intrinio_search_attributes",
            "intrinio_latest_attributes",
            "intrinio_historical_attributes",
            "intrinio_fred_series",
            "intrinio_balance_sheet",
            "intrinio_cash_flow_statement",
            "intrinio_income_statement",
            "intrinio_financial_ratios",
            "intrinio_key_metrics",
            "intrinio_reported_financials",
            "intrinio_forward_ebitda_estimates",
            "intrinio_forward_eps_estimates",
            "intrinio_forward_pe_estimates",
            "intrinio_forward_sales_estimates",
            "intrinio_etf_price_performance",
            "intrinio_market_snapshots",
        }
        assert len(capabilities) == len(get_provider("intrinio").fetcher_bindings)
        assert all(c.source == "intrinio" for c in capabilities)
        assert not any(c.verified for c in capabilities)


class TestCurrencyPairs:
    """POSITIVE plus the rows.filter leg: renamed symbol out, upstream 'code' gone."""

    def test_the_directory_publishes_renamed_columns_for_every_pair(self) -> None:
        transport, rows = _run(CURRENCY_PAIRS)
        assert transport.calls[0]["url"] == PAIRS_URL
        assert transport.calls[0]["params"] == {"api_key": "test-key"}
        assert len(rows) == 3
        assert rows[0].symbol == "USDCAD"
        dumped = rows[0].model_dump()
        assert set(dumped) == {"symbol", "name", "base_currency", "quote_currency"}
        assert "code" not in dumped
        assert rows[0].name == "US Dollar / Canadian Dollar"

    def test_the_row_filter_keeps_only_the_substring_hits(self) -> None:
        transport, rows = _run(CURRENCY_PAIRS, query="usd")
        assert [row.symbol for row in rows] == ["USDCAD", "EURUSD"]
        assert "query" not in transport.calls[0]["params"]

    def test_the_local_query_is_not_sent_to_the_parameterless_endpoint(self) -> None:
        transport, _ = _run(CURRENCY_PAIRS, query="EUR")
        assert transport.calls[0]["url"] == PAIRS_URL
        assert transport.calls[0]["params"] == {"api_key": "test-key"}

    def test_a_pair_record_without_name_publishes_null_the_way_upstream_does(self) -> None:
        """POSITIVE on the optional field: a record omitting name is not a shape failure."""
        _, rows = _run(CURRENCY_PAIRS)
        assert rows[1].name is None
        assert rows[1].symbol == "EURUSD"

    def test_a_needle_that_hits_nothing_selects_everything_away(self) -> None:
        _, rows = _run(CURRENCY_PAIRS, PAIRS_DOCUMENT, query="zzz")
        assert rows == []

    def test_an_empty_published_pair_array_is_a_valid_zero_result(self) -> None:
        _, rows = _run(CURRENCY_PAIRS, {"pairs": []}, query="EUR")
        assert rows == []


class TestEquitySearch:
    """POSITIVE: rows under 'companies', with the documented one-page search contract."""

    def test_the_search_renames_the_upstream_keys(self) -> None:
        transport, rows = _run(EQUITY_SEARCH, COMPANIES_DOCUMENT, query="AAPL")
        assert transport.calls[0]["url"] == SEARCH_URL
        dumped = rows[0].model_dump()
        assert rows[0].symbol == "AAPL"
        assert rows[0].intrinio_id == "c0ffee"
        assert set(dumped) == {"symbol", "name", "intrinio_id", "cik", "lei"}
        assert rows[0].name == "Apple Inc."
        assert "ticker" not in dumped and "id" not in dumped

    def test_limit_is_aliased_to_page_size_with_its_default(self) -> None:
        transport, _ = _run(EQUITY_SEARCH, COMPANIES_DOCUMENT, query="AAPL")
        params = transport.calls[0]["params"]
        assert params["page_size"] == "10000"
        assert params["query"] == "AAPL"
        assert params["active"] == "true"
        assert "limit" not in params and "is_symbol" not in params

    def test_undocumented_cursor_does_not_trigger_a_second_request(self) -> None:
        document = {**COMPANIES_DOCUMENT, "next_page": "not-in-the-search-contract"}
        transport, rows = _run(EQUITY_SEARCH, document, query="AAPL")
        assert len(rows) == 1
        assert len(transport.calls) == 1
        assert "next_page" not in transport.calls[0]["params"]


class TestHistoricalDividends:
    """POSITIVE on columns.derive: the queried symbol reaches every row it is not a record key."""

    def test_the_feed_addresses_the_path_and_stamps_the_symbol(self) -> None:
        transport, rows = _run(HISTORICAL_DIVIDENDS, ADJUSTMENTS_DOCUMENT, symbol="AAPL")
        assert transport.calls[0]["url"] == ADJUSTMENTS_URL
        assert [row.symbol for row in rows] == ["AAPL", "AAPL"]
        assert len(rows) == 2

    def test_the_three_published_keys_are_renamed_and_the_optional_ones_stay_null(self) -> None:
        _, rows = _run(HISTORICAL_DIVIDENDS, ADJUSTMENTS_DOCUMENT, symbol="AAPL")
        dumped = rows[0].model_dump()
        assert set(dumped) == {
            "symbol",
            "ex_dividend_date",
            "amount",
            "currency",
            "factor",
            "split_ratio",
        }
        assert dumped["ex_dividend_date"].isoformat() == "2024-03-15"
        assert dumped["amount"] == 0.25
        assert dumped["currency"] == "USD"
        assert dumped["split_ratio"] is None
        for retired in ("date", "dividend", "dividend_currency"):
            assert retired not in dumped

    def test_the_symbol_is_not_also_sent_in_the_query_string(self) -> None:
        """Upstream excludes it (historical_dividends.py:92); the path consumed it here."""
        transport, _ = _run(
            HISTORICAL_DIVIDENDS, ADJUSTMENTS_DOCUMENT, symbol="AAPL", start_date="2020-01-01"
        )
        params = transport.calls[0]["params"]
        assert params["page_size"] == "100"
        assert params["start_date"] == "2020-01-01"
        assert "symbol" not in params

    def test_nonempty_next_page_cursor_is_followed(self) -> None:
        second = {
            "stock_price_adjustments": [
                {"date": "2024-03-18", "dividend": 0.3, "dividend_currency": "USD"},
            ],
            "next_page": None,
        }
        transport, rows = _run_pages(
            HISTORICAL_DIVIDENDS,
            [
                HttpResponse(
                    200,
                    {
                        "stock_price_adjustments": [
                            {"date": "2024-03-15", "dividend": 0.25},
                        ],
                        "next_page": "cursor-2",
                    },
                ),
                HttpResponse(200, second),
            ],
            sends=2,
            symbol="AAPL",
            start_date="2024-01-01",
            end_date="2024-12-31",
            limit=1,
        )
        assert [row.ex_dividend_date.isoformat() for row in rows] == ["2024-03-15", "2024-03-18"]
        assert len(transport.calls) == 2
        assert transport.calls[0]["params"]["page_size"] == "1"
        assert "next_page" not in transport.calls[0]["params"]
        assert transport.calls[1]["params"]["next_page"] == "cursor-2"

    def test_missing_next_page_is_not_treated_as_terminal(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, {"stock_price_adjustments": []}))
        fetcher = type(
            IntrinioHistoricalDividendsFetcher.__name__,
            (IntrinioHistoricalDividendsFetcher,),
            {"http_transport": transport},
        )()
        with pytest.raises(ProviderEngineError, match="INTRINIO_SHAPE_INVALID"):
            fetcher.fetch(
                ctx=fixture_context(SOURCE, HISTORICAL_DIVIDENDS, sends=1),
                symbol="AAPL",
            )
        assert len(transport.calls) == 1

    def test_only_explicit_null_marks_the_terminal_cursor(self) -> None:
        transport = FixedResponseTransport(
            HttpResponse(200, {"stock_price_adjustments": [], "next_page": ""})
        )
        fetcher = type(
            IntrinioHistoricalDividendsFetcher.__name__,
            (IntrinioHistoricalDividendsFetcher,),
            {"http_transport": transport},
        )()
        with pytest.raises(ProviderEngineError, match="INTRINIO_SHAPE_INVALID"):
            fetcher.fetch(
                ctx=fixture_context(SOURCE, HISTORICAL_DIVIDENDS, sends=1),
                symbol="AAPL",
            )
        assert len(transport.calls) == 1

    def test_an_adjustment_record_without_the_required_dividend_refuses(self) -> None:
        """REFUSAL: amount is required upstream, so a record lacking it is not a null-amount row."""
        document = {
            "stock_price_adjustments": [{"date": "2021-01-04", "factor": 0.5}],
            "next_page": None,
        }
        with pytest.raises(Exception) as caught:
            _run(HISTORICAL_DIVIDENDS, document, symbol="MSFT")
        assert "INTRINIO_SHAPE_INVALID" in str(caught.value)
        assert "amount" in str(caught.value)

    def test_no_symbol_means_no_request_at_all(self) -> None:
        """REFUSAL before I/O: a stamp of None would publish a row that names no security."""
        transport = FixedResponseTransport(HttpResponse(200, ADJUSTMENTS_DOCUMENT))
        cls = make_http_json_fetcher(SOURCE, HISTORICAL_DIVIDENDS)
        fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
        with pytest.raises(Exception) as caught:
            fetcher.fetch(ctx=fixture_context(SOURCE, HISTORICAL_DIVIDENDS, sends=0))
        error = caught.value
        assert "INTRINIO_QUERY_INVALID" in str(error)
        assert error.missing == ("symbol",)
        assert (error.rejected, error.invalid) == ((), ())
        assert transport.calls == []


class TestCalendarIpo:
    """POSITIVE on path.explode: the repoint plan projects nested company/security scalars out."""

    def test_the_calendar_addresses_the_endpoint_and_aliases_the_query(self) -> None:
        transport, rows = _run(
            CALENDAR_IPO, IPO_DOCUMENT, symbol="ARM", min_value=1000, max_value=200000
        )
        assert transport.calls[0]["url"] == IPO_URL
        params = transport.calls[0]["params"]
        assert params["ticker"] == "ARM"
        assert params["offer_amount_greater_than"] == "1000"
        assert params["offer_amount_less_than"] == "200000"
        assert params["page_size"] == "100"
        assert "symbol" not in params and "limit" not in params
        assert len(rows) == 2

    def test_each_row_carries_the_flattened_contract(self) -> None:
        _, rows = _run(CALENDAR_IPO, IPO_DOCUMENT)
        dumped = rows[0].model_dump()
        # 20 flat IPO keys + 5 company scalars + 10 security scalars, per the cited contract.
        assert len(dumped) == 35
        assert dumped["symbol"] == "ARM"
        assert dumped["ipo_date"].isoformat() == "2023-09-14"
        assert dumped["company_name"] == "Arm Holdings plc"
        assert dumped["company_cik"] == "1975112"
        assert dumped["security_composite_ticker"] == "ARM:NASDAQ"
        assert dumped["security_primary_listing"] is True
        for retired in ("company", "security"):
            assert retired not in dumped

    def test_an_item_without_the_nested_objects_publishes_nulls_not_a_refusal(self) -> None:
        """Every upstream field is optional (calendar_ipo.py:45-155), absence reads as null."""
        _, rows = _run(CALENDAR_IPO, IPO_DOCUMENT)
        second = rows[1].model_dump()
        assert second["symbol"] == "NEWCO"
        assert second["status"] == "upcoming"
        assert second["offer_amount"] is None
        assert second["company_id"] is None
        assert second["security_id"] is None

    def test_a_document_without_the_row_key_refuses(self) -> None:
        with pytest.raises(Exception) as caught:
            _run(CALENDAR_IPO, {"ipos": []})
        assert "INTRINIO_SHAPE_INVALID" in str(caught.value)

    def test_an_empty_published_list_is_a_valid_zero_result(self) -> None:
        transport, rows = _run(
            CALENDAR_IPO,
            {"initial_public_offerings": [], "next_page": None},
        )
        assert rows == []
        assert len(transport.calls) == 1

    def test_a_missing_nested_object_key_is_a_null_not_a_shape_failure(self) -> None:
        document = {
            "initial_public_offerings": [
                {"ticker": "X", "date": "2024-02-02", "company": None, "security": None},
            ],
            "next_page": None,
        }
        _, rows = _run(CALENDAR_IPO, document)
        assert rows[0].company_id is None
        assert rows[0].security_ticker is None


@pytest.mark.parametrize(
    ("fetcher_type", "spec", "query", "cursor", "terminal_page"),
    [
        (
            IntrinioCalendarIpoFetcher,
            CALENDAR_IPO,
            {
                "symbol": "SAMP",
                "status": "priced",
                "start_date": "2024-08-01",
                "end_date": "2024-08-31",
                "limit": 1,
            },
            "IPO-CURSOR-2",
            {
                "initial_public_offerings": [
                    {**IPO_DOCUMENT["initial_public_offerings"][0], "ticker": "SAMP"}
                ],
                "next_page": None,
            },
        ),
        (
            IntrinioInsiderTradingFetcher,
            INSIDER_TRADING,
            {
                "symbol": "AAPL",
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
                "ownership_type": "D",
                "sort_by": "filing_date",
                "limit": 1,
            },
            "INSIDER-CURSOR-2",
            {
                "transaction_filings": [
                    {
                        "filing_date": "2024-01-10",
                        "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/example.xml",
                        "issuer_ticker": "AAPL",
                        "issuer_cik": "0000320193",
                        "issuer_company": "Apple Inc.",
                        "owner": {
                            "owner_cik": "0001234567",
                            "owner_name": "Example Person",
                        },
                        "transactions": [{"transaction_date": "2024-01-08"}],
                    }
                ],
                "next_page": None,
            },
        ),
    ],
    ids=("CalendarIpo", "InsiderTrading"),
)
def test_empty_page_cursor_continues_via_governed_budget(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    spec: ModelSpec,
    query: dict[str, object],
    cursor: str,
    terminal_page: object,
) -> None:
    first_page = {spec.rows_pointer: [], "next_page": cursor}
    session, ctx, rows = _run_governed_pages(
        monkeypatch,
        fetcher_type,
        spec,
        [first_page, terminal_page],
        sends=2,
        **query,
    )
    assert len(rows) == 1
    assert len(session.calls) == 2
    first_params = session.calls[0]["params"]
    second_params = session.calls[1]["params"]
    assert "next_page" not in first_params
    assert second_params["next_page"] == cursor
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 2


_MISSING_CURSOR = object()


@pytest.mark.parametrize(
    ("fetcher_type", "spec", "query"),
    [
        (
            IntrinioCalendarIpoFetcher,
            CALENDAR_IPO,
            {"symbol": "SAMP", "status": "priced", "limit": 1},
        ),
        (
            IntrinioInsiderTradingFetcher,
            INSIDER_TRADING,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
    ],
    ids=("CalendarIpo", "InsiderTrading"),
)
@pytest.mark.parametrize(
    ("cursor_value", "expected_invalid", "expected_missing"),
    [
        pytest.param(_MISSING_CURSOR, (), ("next_page",), id="missing"),
        pytest.param(False, ("next_page",), (), id="false"),
        pytest.param(True, ("next_page",), (), id="true"),
        pytest.param(0, ("next_page",), (), id="integer-zero"),
        pytest.param(17, ("next_page",), (), id="integer-positive"),
        pytest.param("", ("next_page",), (), id="empty-string"),
        pytest.param([], ("next_page",), (), id="list"),
        pytest.param({}, ("next_page",), (), id="object"),
    ],
)
def test_strict_cursor_refuses_missing_or_non_nullable_string_before_next_send(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    spec: ModelSpec,
    query: dict[str, object],
    cursor_value: object,
    expected_invalid: tuple[str, ...],
    expected_missing: tuple[str, ...],
) -> None:
    document: dict[str, object] = {spec.rows_pointer: []}
    if cursor_value is not _MISSING_CURSOR:
        document["next_page"] = cursor_value
    session = _JsonSession([document, {spec.rows_pointer: [], "next_page": None}])
    monkeypatch.setattr(
        http_client,
        "get_shared_http_client",
        lambda: GovernedHttpClient(session=session),
    )
    ctx = fixture_context(SOURCE, spec, sends=2)
    with pytest.raises(ProviderEngineError) as caught:
        fetcher_type().fetch(ctx=ctx, **query)
    assert caught.value.code == "INTRINIO_SHAPE_INVALID"
    assert caught.value.invalid == expected_invalid
    assert caught.value.missing == expected_missing
    assert len(session.calls) == 1
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 1


@pytest.mark.parametrize(
    ("fetcher_type", "spec", "query"),
    [
        (
            IntrinioCalendarIpoFetcher,
            CALENDAR_IPO,
            {"symbol": "SAMP", "status": "priced", "limit": 1},
        ),
        (
            IntrinioInsiderTradingFetcher,
            INSIDER_TRADING,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
    ],
    ids=("CalendarIpo", "InsiderTrading"),
)
def test_strict_cursor_refuses_repeated_cursor_before_repeated_send(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    spec: ModelSpec,
    query: dict[str, object],
) -> None:
    bounded = replace(spec, pagination=replace(spec.pagination, max_pages=5))
    pages = [
        {spec.rows_pointer: [], "next_page": "cursor-a"},
        {spec.rows_pointer: [], "next_page": "cursor-b"},
        {spec.rows_pointer: [], "next_page": "cursor-a"},
    ]
    session = _JsonSession(pages)
    monkeypatch.setattr(
        http_client,
        "get_shared_http_client",
        lambda: GovernedHttpClient(session=session),
    )
    ctx = fixture_context(SOURCE, bounded, sends=4)
    with pytest.raises(ProviderEngineError) as caught:
        make_http_json_fetcher(SOURCE, bounded)().fetch(ctx=ctx, **query)
    assert caught.value.code == "INTRINIO_INCOMPLETE"
    assert len(session.calls) == 3
    assert [call["params"].get("next_page") for call in session.calls] == [
        None,
        "cursor-a",
        "cursor-b",
    ]
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 3


@pytest.mark.parametrize(
    ("fetcher_type", "spec", "query", "first_page", "second_page"),
    [
        (
            IntrinioCalendarIpoFetcher,
            CALENDAR_IPO,
            {"symbol": "SAMP", "status": "priced", "limit": 1},
            {"initial_public_offerings": [], "next_page": "cursor-a"},
            {"initial_public_offerings": [], "next_page": None},
        ),
        (
            IntrinioInsiderTradingFetcher,
            INSIDER_TRADING,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"},
            {"transaction_filings": [], "next_page": "cursor-a"},
            {"transaction_filings": [], "next_page": None},
        ),
    ],
    ids=("CalendarIpo", "InsiderTrading"),
)
def test_strict_cursor_does_not_send_next_page_after_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    spec: ModelSpec,
    query: dict[str, object],
    first_page: dict[str, object],
    second_page: dict[str, object],
) -> None:
    session = _JsonSession([first_page, second_page])
    monkeypatch.setattr(
        http_client,
        "get_shared_http_client",
        lambda: GovernedHttpClient(session=session),
    )
    ctx = fixture_context(SOURCE, spec, sends=1)
    with pytest.raises(RequestAttemptLimitError):
        fetcher_type().fetch(ctx=ctx, **query)
    assert len(session.calls) == 1
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 1


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, id="integer"),
        pytest.param("true", id="string"),
        pytest.param(None, id="null"),
    ],
)
def test_strict_nullable_cursor_flag_requires_an_exact_boolean(value: object) -> None:
    with pytest.raises(ValueError, match="strict_nullable_string must be a bool"):
        PaginationSpec(strict_nullable_string=value)  # type: ignore[arg-type]


def test_strict_nullable_cursor_flag_requires_cursor_pagination() -> None:
    with pytest.raises(ValueError, match="requires cursor pagination"):
        PaginationSpec(strict_nullable_string=True)


def test_only_the_two_intrinio_cursor_endpoints_opt_into_strict_metadata() -> None:
    assert CALENDAR_IPO.pagination.strict_nullable_string is True
    assert INSIDER_TRADING.pagination.strict_nullable_string is True
    assert HISTORICAL_DIVIDENDS.pagination.strict_nullable_string is False


def test_non_strict_cursor_keeps_legacy_missing_cursor_terminal_behavior() -> None:
    legacy = replace(
        CALENDAR_IPO,
        pagination=replace(CALENDAR_IPO.pagination, strict_nullable_string=False),
    )
    transport, rows = _run(legacy, {"initial_public_offerings": []}, symbol="SAMP", status="priced")
    assert rows == []
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("spec", "query"),
    [
        (
            CALENDAR_IPO,
            {"symbol": "SAMP", "status": "priced", "limit": 1},
        ),
        (
            INSIDER_TRADING,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
    ],
    ids=("CalendarIpo", "InsiderTrading"),
)
def test_repeated_cursor_refuses_at_declared_page_bound_and_uses_governed_attempts(
    monkeypatch: pytest.MonkeyPatch,
    spec: ModelSpec,
    query: dict[str, object],
) -> None:
    bounded = replace(spec, pagination=replace(spec.pagination, max_pages=2))
    repeated_page = {spec.rows_pointer: [], "next_page": "REPEATED-CURSOR"}
    session = _JsonSession([repeated_page, repeated_page])
    monkeypatch.setattr(
        http_client,
        "get_shared_http_client",
        lambda: GovernedHttpClient(session=session),
    )
    ctx = fixture_context(SOURCE, bounded, sends=2)
    bounded_fetcher = make_http_json_fetcher(SOURCE, bounded)
    with pytest.raises(ProviderEngineError) as caught:
        bounded_fetcher().fetch(ctx=ctx, **query)
    assert caught.value.code == "INTRINIO_INCOMPLETE"
    assert len(session.calls) == 2
    assert session.calls[1]["params"]["next_page"] == "REPEATED-CURSOR"
    assert ctx.request_budget is not None and ctx.request_budget.attempts_used == 2


class TestRefusals:
    """REFUSAL arms: a wrong row key, a missing required column, no credential, no is_symbol."""

    def test_a_document_without_the_declared_row_key_refuses(self) -> None:
        with pytest.raises(Exception) as caught:
            _run(EQUITY_SEARCH, {"results": COMPANIES_DOCUMENT["companies"]}, query="AAPL")
        assert "INTRINIO_SHAPE_INVALID" in str(caught.value)

    def test_a_row_missing_a_declared_required_column_refuses(self) -> None:
        document = {"companies": [{"ticker": "MSFT", "name": "Microsoft", "cik": "789019"}]}
        with pytest.raises(Exception) as caught:
            _run(EQUITY_SEARCH, document, query="MSFT")
        assert "INTRINIO_SHAPE_INVALID" in str(caught.value)
        assert "intrinio_id" in str(caught.value)

    def test_a_missing_credential_is_refused_before_any_send(self, monkeypatch) -> None:
        monkeypatch.delenv("INTRINIO_API_KEY")
        transport = FixedResponseTransport(HttpResponse(200, PAIRS_DOCUMENT))
        fetcher = type(
            IntrinioCurrencyPairsFetcher.__name__,
            (IntrinioCurrencyPairsFetcher,),
            {"http_transport": transport},
        )()
        with pytest.raises(Exception) as caught:
            fetcher.fetch(ctx=fixture_context(SOURCE, CURRENCY_PAIRS, sends=1))
        assert "INTRINIO_CREDENTIAL_MISSING" in str(caught.value)
        assert transport.calls == []

    def test_is_symbol_is_not_an_accepted_parameter(self) -> None:
        transport = FixedResponseTransport(HttpResponse(200, COMPANIES_DOCUMENT))
        cls = make_http_json_fetcher(SOURCE, EQUITY_SEARCH)
        fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
        with pytest.raises(Exception) as caught:
            fetcher.fetch(
                ctx=fixture_context(SOURCE, EQUITY_SEARCH, sends=1),
                query="AAPL",
                is_symbol=True,
            )
        assert "INTRINIO_QUERY_INVALID" in str(caught.value)
        assert caught.value.rejected == ("is_symbol",)
        assert (caught.value.missing, caught.value.invalid) == ((), ())
        assert "is_symbol" in str(caught.value)
        assert transport.calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "spec", "query", "document"),
    [
        (IntrinioCurrencyPairsFetcher, CURRENCY_PAIRS, {"query": "EUR"}, PAIRS_DOCUMENT),
        (IntrinioEquitySearchFetcher, EQUITY_SEARCH, {"query": "AAPL"}, COMPANIES_DOCUMENT),
        (
            IntrinioHistoricalDividendsFetcher,
            HISTORICAL_DIVIDENDS,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-12-31"},
            ADJUSTMENTS_DOCUMENT,
        ),
        (
            IntrinioCalendarIpoFetcher,
            CALENDAR_IPO,
            {"symbol": "ARM", "status": "priced"},
            IPO_DOCUMENT,
        ),
        (
            IntrinioInsiderTradingFetcher,
            None,
            {"symbol": "AAPL", "start_date": "2024-01-01", "end_date": "2024-03-31"},
            {
                "transaction_filings": [
                    {
                        "filing_date": "2024-03-01",
                        "filing_url": "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
                        "issuer_ticker": "AAPL",
                        "issuer_cik": "0000320193",
                        "issuer_company": "Apple Inc.",
                        "owner": {"owner_cik": "0001114892", "owner_name": "Cook Timothy D"},
                        "transactions": [{"transaction_date": "2024-02-27", "amount_of_shares": 3}],
                    },
                ],
                "next_page": None,
            },
        ),
    ],
)
def test_provider_model_query_injects_source_market_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type[Any],
    spec: ModelSpec | None,
    query: dict[str, object],
    document: object,
) -> None:
    """Exercise the real source binding through service injection and contract serialization."""
    fetcher = fetcher_type()
    declaration = fetcher.model_spec if spec is None else spec
    transport = FixedResponseTransport(HttpResponse(200, document))
    monkeypatch.setattr(fetcher, "http_transport", transport)
    capability = fetcher.capability
    received: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_query(**values: object) -> object:
        received.update(values)
        return original_transform(**values)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    descriptor = SimpleNamespace(
        source=SOURCE,
        model=fetcher.canonical_model,
        domain=capability.domain,
        verified=False,
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
        lambda _domain: (None, fetcher.row_model),
    )
    principal = SimpleNamespace(allows_domain=lambda domain: domain == capability.domain)
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source=SOURCE,
            model=fetcher.canonical_model,
            query=dict(query),
            principal=principal,
            ctx=fixture_context(SOURCE, declaration, sends=1),
        )
    )
    assert received["source"] == SOURCE
    assert received["market"] == capability.market
    assert response["source"] == SOURCE
    assert response["model"] == fetcher.canonical_model
    assert response["results"]
    assert json.dumps(response["results"], allow_nan=False)
    if fetcher.canonical_model == "CurrencyPairs":
        assert "query" not in transport.calls[0]["params"]
