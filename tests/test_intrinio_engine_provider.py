"""The four declared intrinio engine models: offline positives and reachable refusals.

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

import pytest

from opendata.data.providers._engine.http_json import HttpResponse, make_http_json_fetcher
from opendata.data.providers._engine.row_expansion import EMPTY_RESULT, RowExpansionError
from opendata.data.providers._engine.testing import FixedResponseTransport, fixture_context
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import (
    CALENDAR_IPO,
    CURRENCY_PAIRS,
    EQUITY_SEARCH,
    HISTORICAL_DIVIDENDS,
)
from opendata.data.registry import ProviderRegistry

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
    ]
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
    ]
}


@pytest.fixture(autouse=True)
def _intrinio_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local-only key; the synthetic transport never reaches a socket with it."""
    monkeypatch.setenv("INTRINIO_API_KEY", "test-key")


def _run(spec, document=None, **query):
    """Drive one declared model over one hand-written document."""
    body = PAIRS_DOCUMENT if document is None else document
    transport = FixedResponseTransport(HttpResponse(200, body))
    cls = make_http_json_fetcher(SOURCE, spec)
    fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
    rows = list(fetcher.fetch(ctx=fixture_context(SOURCE, spec, sends=1), **query))
    return transport, rows


class TestRegistration:
    """intrinio is a real provider now, not a reserved catalog identity."""

    def test_catalog_no_longer_reserves_intrinio(self) -> None:
        provider = get_provider("intrinio")
        assert "reserved" not in provider.description.lower()
        assert len(provider.fetcher_bindings) == 4
        assert provider.credentials[0].environment_variable == "INTRINIO_API_KEY"

    def test_registry_publishes_the_four_declared_models(self) -> None:
        registry = ProviderRegistry()
        capabilities = register_provider("intrinio", registry)
        domains = {c.domain for c in capabilities}
        assert domains == {
            "intrinio_currency_pairs",
            "intrinio_equity_search",
            "intrinio_historical_dividends",
            "intrinio_calendar_ipo",
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
        _, rows = _run(CURRENCY_PAIRS, query="usd")
        assert [row.symbol for row in rows] == ["USDCAD", "EURUSD"]

    def test_a_pair_record_without_name_publishes_null_the_way_upstream_does(self) -> None:
        """POSITIVE on the optional field: a record omitting name is not a shape failure."""
        _, rows = _run(CURRENCY_PAIRS)
        assert rows[1].name is None
        assert rows[1].symbol == "EURUSD"

    def test_a_needle_that_hits_nothing_selects_everything_away(self) -> None:
        _, rows = _run(CURRENCY_PAIRS, PAIRS_DOCUMENT, query="zzz")
        assert rows == []


class TestEquitySearch:
    """POSITIVE: one GET, rows under 'companies', id->intrinio_id and ticker->symbol."""

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

    def test_an_adjustment_record_without_the_required_dividend_refuses(self) -> None:
        """REFUSAL: amount is required upstream, so a record lacking it is not a null-amount row."""
        document = {"stock_price_adjustments": [{"date": "2021-01-04", "factor": 0.5}]}
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

    def test_an_empty_published_list_refuses_rather_than_publishing_zero(self) -> None:
        """Upstream raises EmptyDataError on this answer (:190-191); the repoint kernel refuses."""
        with pytest.raises(RowExpansionError) as caught:
            _run(CALENDAR_IPO, {"initial_public_offerings": []})
        assert caught.value.reason == EMPTY_RESULT

    def test_a_missing_nested_object_key_is_a_null_not_a_shape_failure(self) -> None:
        document = {
            "initial_public_offerings": [
                {"ticker": "X", "date": "2024-02-02", "company": None, "security": None},
            ]
        }
        _, rows = _run(CALENDAR_IPO, document)
        assert rows[0].company_id is None
        assert rows[0].security_ticker is None


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
        cls = make_http_json_fetcher(SOURCE, CURRENCY_PAIRS)
        fetcher = type(cls.__name__, (cls,), {"http_transport": transport})()
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
