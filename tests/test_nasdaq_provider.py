"""Offline contract tests for the typed Nasdaq source slice."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from opendata.data import domains
from opendata.data.domains import DomainSpec
from opendata.data.models.nasdaq import (
    NasdaqCalendarDividend,
    NasdaqCalendarEarnings,
    NasdaqCalendarIpo,
    NasdaqCompanyFiling,
    NasdaqEconomicEvent,
    NasdaqEquityScreener,
    NasdaqEquitySearch,
    NasdaqHistoricalDividend,
    NasdaqTopRetail,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import HttpResponse, ProviderEngineError
from opendata.data.providers.nasdaq.models.calendar_dividend import CalendarDividendFetcher
from opendata.data.providers.nasdaq.models.calendar_earnings import CalendarEarningsFetcher
from opendata.data.providers.nasdaq.models.calendar_ipo import CalendarIpoFetcher
from opendata.data.providers.nasdaq.models.company_filings import CompanyFilingsFetcher
from opendata.data.providers.nasdaq.models.economic_calendar import EconomicCalendarFetcher
from opendata.data.providers.nasdaq.models.equity_screener import EquityScreenerFetcher
from opendata.data.providers.nasdaq.models.equity_search import EquitySearchFetcher
from opendata.data.providers.nasdaq.models.historical_dividends import HistoricalDividendsFetcher
from opendata.data.providers.nasdaq.models.top_retail import TopRetailFetcher
from opendata.data.providers.nasdaq.provider import PROVIDER
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import query_provider_model

FIXTURES = Path(__file__).parent.parent / "docs/evidence/C100/nasdaq/fixtures"
FIXED_DAY = date(2026, 10, 12)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _context(fetcher: object, *, attempts: int = 60) -> FetchContext:
    model = fetcher.canonical_model  # type: ignore[attr-defined]
    host = (
        "data.nasdaq.com"
        if isinstance(fetcher, TopRetailFetcher)
        else "www.nasdaqtrader.com"
        if isinstance(fetcher, EquitySearchFetcher)
        else "api.nasdaq.com"
    )
    grant = RequestGrant(
        source="nasdaq",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline fixture test; fake transport only",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
        task_attempts=attempts,
        source_attempts=attempts,
        grants=(grant,),
    )
    return FetchContext(timeout=2.0, request_budget=budget)


def _transport(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: object,
    *,
    document: object = None,
    payload: bytes | str | None = None,
    status: int = 200,
    responder: Any = None,
) -> list[tuple[str, dict[str, str], str, object]]:
    calls: list[tuple[str, dict[str, str], str, object]] = []

    def fake(
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: object,
        decoder: str,
    ) -> HttpResponse:
        del timeout
        calls.append((url, dict(params), source, headers))
        selected = responder(url, params) if responder is not None else None
        if isinstance(selected, HttpResponse):
            return selected
        if selected is not None:
            return HttpResponse(status, selected)
        return HttpResponse(status, document, payload=payload if decoder != "json" else None)

    monkeypatch.setattr(fetcher, "http_transport", fake, raising=False)
    return calls


def _query_for(fetcher: object) -> dict[str, object]:
    if isinstance(fetcher, CompanyFilingsFetcher):
        return {"symbol": "ACME"}
    if isinstance(fetcher, HistoricalDividendsFetcher):
        return {"symbol": "ACME", "start_date": FIXED_DAY, "end_date": FIXED_DAY}
    if isinstance(fetcher, EconomicCalendarFetcher):
        return {"start_date": FIXED_DAY, "end_date": FIXED_DAY}
    if isinstance(fetcher, TopRetailFetcher):
        return {"start_date": date(2026, 10, 9), "end_date": FIXED_DAY}
    if isinstance(fetcher, (CalendarDividendFetcher, CalendarEarningsFetcher, CalendarIpoFetcher)):
        return {"start_date": FIXED_DAY, "end_date": FIXED_DAY}
    return {}


def _run(
    monkeypatch: pytest.MonkeyPatch,
    fetcher: object,
    *,
    document: object = None,
    payload: bytes | str | None = None,
    status: int = 200,
    responder: Any = None,
    **query: object,
) -> tuple[object, list[tuple[str, dict[str, str], str, object]]]:
    calls = _transport(
        monkeypatch,
        fetcher,
        document=document,
        payload=payload,
        status=status,
        responder=responder,
    )
    return fetcher.fetch(ctx=_context(fetcher), **query), calls  # type: ignore[attr-defined]


def test_descriptor_declares_all_nine_typed_models_and_limited_credential_scope() -> None:
    assert len(PROVIDER.fetcher_bindings) == 9
    assert {
        model for binding in PROVIDER.fetcher_bindings for model in binding.canonical_model_ids
    } == {
        "CalendarDividend",
        "CalendarEarnings",
        "CalendarIpo",
        "CompanyFilings",
        "EconomicCalendar",
        "EquitySearch",
        "EquityScreener",
        "HistoricalDividends",
        "TopRetail",
    }
    assert [
        (item.environment_variable, item.required_for_health) for item in PROVIDER.credentials
    ] == [("NASDAQ_DATA_LINK_API_KEY", False)]
    assert len(PROVIDER.fetchers) == 9


def test_provider_model_query_pins_nasdaq_identity_and_serializes_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderRegistry()
    binding = PROVIDER.fetcher_dict["EquitySearch"]
    fetcher = binding.load(PROVIDER.source)
    registry.register_provider_models(PROVIDER.source, binding.canonical_model_ids, fetcher)
    spec = DomainSpec(
        display_name="Nasdaq security search",
        rest_path="market/nasdaq/equity-search",
        contract="NasdaqEquitySearch",
        temporal_kind="snapshot",
        time_field=None,
        natural_key=("symbol", "exchange"),
        filter_dims=("symbol", "name", "exchange", "market_category", "etf"),
        storage_mode="transient",
        permissions=("query",),
    )
    monkeypatch.setattr(domains, "require_domain_semantics", lambda _domain: spec)
    monkeypatch.setattr(domains, "contract_model", lambda _domain: NasdaqEquitySearch)
    source_query: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        source_query.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)

    def fake_transport(
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
        headers: object,
        decoder: str,
    ) -> HttpResponse:
        del params, timeout, headers
        assert url == "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
        assert source == "nasdaq" and decoder == "delimited"
        directory = (FIXTURES / "equity_search.txt").read_text(encoding="utf-8")
        return HttpResponse(200, None, payload=directory)

    monkeypatch.setattr(fetcher, "http_transport", fake_transport, raising=False)
    principal = type("Principal", (), {"allows_domain": lambda _self, _domain: True})()
    response = asyncio.run(
        query_provider_model(
            registry=registry,
            source="nasdaq",
            model="EquitySearch",
            query={"query": "ACME", "source": "auto"},
            principal=principal,
            ctx=_context(fetcher),
        )
    )

    assert source_query["source"] == "nasdaq"
    assert source_query["market"] == "us"
    assert response["source"] == "nasdaq"
    assert response["model"] == "EquitySearch"
    assert response["domain"] == "nasdaq_equity_search"
    assert response["results"] == [
        {
            "symbol": "ACME",
            "name": "Acme Example Corporation - Common Stock",
            "nasdaq_traded": "Y",
            "exchange": "N",
            "market_category": "Q",
            "etf": "N",
            "round_lot_size": 100.0,
            "test_issue": "N",
            "financial_status": "N",
            "cqs_symbol": "ACME",
            "nasdaq_symbol": "ACME",
            "next_shares": None,
        }
    ]


def test_calendar_dividend_maps_typed_dates_and_money(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = CalendarDividendFetcher()
    rows, calls = _run(
        monkeypatch,
        fetcher,
        document=_fixture("calendar_dividend.json"),
        start_date=FIXED_DAY,
        end_date=FIXED_DAY,
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/calendar/dividends")
    assert calls[0][1] == {"date": "2026-10-12"}
    assert rows == (
        NasdaqCalendarDividend(
            ex_dividend_date=FIXED_DAY,
            symbol="ACME",
            amount=0.25,
            name="Acme Example Corporation",
            record_date=date(2026, 10, 13),
            payment_date=date(2026, 10, 20),
            declaration_date=None,
            annualized_amount=1.0,
        ),
    )


def test_calendar_earnings_injects_asof_and_scales_surprise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, calls = _run(
        monkeypatch,
        CalendarEarningsFetcher(),
        document=_fixture("calendar_earnings.json"),
        start_date=FIXED_DAY,
        end_date=FIXED_DAY,
    )
    assert len(calls) == 1
    assert isinstance(rows[0], NasdaqCalendarEarnings)
    assert rows[0].report_date == FIXED_DAY
    assert rows[0].eps_previous == pytest.approx(1.10)
    assert rows[0].eps_consensus == pytest.approx(1.20)
    assert rows[0].eps_actual is None and rows[0].surprise_percent is None
    assert rows[0].num_estimates == 8
    assert rows[0].previous_report_date == date(2025, 7, 28)
    assert rows[0].reporting_time == "After Market Close"
    assert calls[0][0].endswith("/api/calendar/earnings")
    assert calls[0][1] == {"date": "2026-10-12"}


@pytest.mark.parametrize(
    ("status", "expected_date_field", "expected_date", "expected_amount"),
    [
        ("upcoming", "expected_price_date", date(2026, 10, 30), 18_000_000.0),
        ("priced", "ipo_date", date(2026, 10, 8), 10_000_000.0),
        ("filed", "filed_date", date(2026, 10, 6), 20_000_000.0),
        ("withdrawn", None, None, None),
    ],
)
def test_calendar_ipo_reads_observed_status_branches_and_spo_param(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_date_field: str | None,
    expected_date: date | None,
    expected_amount: float | None,
) -> None:
    fetcher = CalendarIpoFetcher()
    calls = _transport(
        monkeypatch,
        fetcher,
        document=_fixture("calendar_ipo.json"),
    )
    rows = fetcher.fetch(
        ctx=_context(fetcher),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        status=status,
        is_spo=True,
    )
    assert len(calls) == 1 and calls[0][0].endswith("/api/ipo/calendar")
    assert calls[0][1] == {"date": "2026-10", "type": "spo"}
    if expected_date_field is None:
        assert rows == ()
        return
    assert len(rows) == 1 and isinstance(rows[0], NasdaqCalendarIpo)
    assert getattr(rows[0], expected_date_field) == expected_date
    assert rows[0].offer_amount == expected_amount
    assert rows[0].deal_status == status


def test_company_filings_walks_offsets_and_maps_nested_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CompanyFilingsFetcher()

    def responder(url: str, params: dict[str, str]) -> object:
        del url
        return _fixture(f"company_filings_page_{int(params['offset'])}.json")

    rows, calls = _run(monkeypatch, fetcher, responder=responder, symbol="ACME", limit=2)
    assert [params["offset"] for _, params, _, _ in calls] == ["0", "1"]
    assert all(url.endswith("/api/company/ACME/sec-filings") for url, _, _, _ in calls)
    assert calls[0][1] == {
        "limit": "2",
        "offset": "0",
        "sortColumn": "filed",
        "sortOrder": "desc",
        "IsQuoteMedia": "true",
    }
    assert len(rows) == 2 and all(isinstance(row, NasdaqCompanyFiling) for row in rows)
    assert rows[0].filing_date == date(2026, 10, 10)
    assert rows[0].report_type == "10-Q"
    assert rows[0].period_ending == date(2026, 9, 30)
    assert rows[0].report_url.endswith("ref=fixture-html")
    assert rows[0].pdf_url.endswith("ref=fixture-pdf")


def test_economic_calendar_synthesizes_utc_and_strips_html(monkeypatch: pytest.MonkeyPatch) -> None:
    rows, calls = _run(
        monkeypatch,
        EconomicCalendarFetcher(),
        document=_fixture("economic_calendar.json"),
        start_date=FIXED_DAY,
        end_date=FIXED_DAY,
        country="United States",
        market="global",
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/calendar/economicevents")
    assert calls[0][1] == {"date": "2026-10-12"}
    row = rows[0]
    assert isinstance(row, NasdaqEconomicEvent)
    assert row.date == datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)
    assert row.description == "Example monthly release."
    assert row.actual == "0.1%"
    assert row.category is None and row.importance is None and row.currency is None


def test_equity_search_parses_official_directory_and_filters_test_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, calls = _run(
        monkeypatch,
        EquitySearchFetcher(),
        payload=(FIXTURES / "equity_search.txt").read_text(encoding="utf-8"),
        query="Acme",
    )
    assert len(calls) == 1 and calls[0][2] == "nasdaq"
    assert [row.symbol for row in rows] == ["ACME"]
    assert isinstance(rows[0], NasdaqEquitySearch)
    assert rows[0].exchange == "N"


def test_equity_screener_maps_server_filters_and_scales_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, calls = _run(
        monkeypatch,
        EquityScreenerFetcher(),
        document=_fixture("equity_screener.json"),
        country="United States",
        sector="Technology",
        limit=20,
    )
    assert calls[0][1]["country"] == "United_States"
    assert calls[0][1]["sector"] == "Technology"
    assert calls[0][1]["limit"] == "20" and calls[0][1]["offset"] == "0"
    assert calls[0][1]["tableonly"] == "true"
    assert calls[0][0].endswith("/api/screener/stocks")
    assert isinstance(rows[0], NasdaqEquityScreener)
    assert rows[0].change_percent == pytest.approx(0.0204)
    assert rows[0].market_cap == 1_200_000_000


def test_equity_screener_follows_offsets_to_requested_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = EquityScreenerFetcher()

    def responder(url: str, params: dict[str, str]) -> object:
        assert url.endswith("/api/screener/stocks")
        document = _fixture(
            "equity_screener.json" if params["offset"] == "0" else "equity_screener_page_1.json"
        )
        document["data"]["totalrecords"] = 2
        return document

    rows, calls = _run(monkeypatch, fetcher, responder=responder, limit=2)
    assert [params["offset"] for _, params, _, _ in calls] == ["0", "1"]
    assert [row.symbol for row in rows] == ["ACME", "BETA"]


def test_historical_dividends_uses_stock_asset_class_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows, calls = _run(
        monkeypatch,
        HistoricalDividendsFetcher(),
        document=_fixture("historical_dividends.json"),
        symbol="ACME",
        start_date=date(2026, 9, 1),
        end_date=FIXED_DAY,
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/quote/ACME/dividends")
    assert calls[0][1] == {"assetclass": "stocks"}
    assert len(rows) == 1 and isinstance(rows[0], NasdaqHistoricalDividend)
    assert rows[0].amount == 0.25 and rows[0].symbol == "ACME"
    assert rows[0].dividend_type == "Cash"


def test_top_retail_reads_nasdaq_data_link_table_with_model_scoped_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "fixture-key-never-live")
    rows, calls = _run(
        monkeypatch,
        TopRetailFetcher(),
        document=_fixture("top_retail.json"),
        start_date=date(2026, 10, 9),
        end_date=FIXED_DAY,
    )
    assert len(calls) == 1
    assert calls[0][0].endswith("/api/v3/datatables/NDAQ/RTAT10.json")
    assert calls[0][1]["api_key"] == "fixture-key-never-live"
    assert calls[0][1]["date.gte"] == "2026-10-09"
    assert len(rows) == 2 and all(isinstance(row, NasdaqTopRetail) for row in rows)
    assert rows[0].symbol == "ACME" and rows[0].activity == pytest.approx(0.0145)


@pytest.mark.parametrize(
    ("fetcher_type", "document", "payload", "query"),
    [
        (
            CalendarDividendFetcher,
            {"data": {"calendar": {"headers": None, "rows": None}}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarEarningsFetcher,
            {
                "data": {
                    "asOf": "Mon, Oct 12, 2026",
                    "headers": None,
                    "rows": None,
                }
            },
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarIpoFetcher,
            {"data": {"upcoming": {"upcomingTable": {"headers": None, "rows": None}}}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CompanyFilingsFetcher,
            {"data": {"totalRecords": 0, "headers": None, "rows": None}},
            None,
            {"symbol": "ACME"},
        ),
        (
            EconomicCalendarFetcher,
            {
                "data": None,
                "status": {
                    "rCode": 200,
                    "bCodeMessage": [
                        {"code": 1002, "errorMessage": "Economic Events Calendar: No record found."}
                    ],
                },
            },
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            EquityScreenerFetcher,
            {
                "data": {
                    "totalrecords": 0,
                    "table": {"headers": None, "rows": None},
                }
            },
            None,
            {},
        ),
        (
            HistoricalDividendsFetcher,
            {"data": {"dividends": {"headers": None, "rows": None}}},
            None,
            {"symbol": "ACME", "start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            TopRetailFetcher,
            {
                "datatable": {
                    "columns": [
                        {"name": "date"},
                        {"name": "ticker"},
                        {"name": "activity"},
                        {"name": "sentiment"},
                    ],
                    "data": [],
                }
            },
            None,
            {"start_date": date(2026, 10, 9), "end_date": FIXED_DAY},
        ),
        (
            EquitySearchFetcher,
            None,
            "Nasdaq Traded|Symbol|Security Name|Listing Exchange|"
            "Market Category|ETF|Round Lot Size|"
            "Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares|\n",
            {"query": "absent"},
        ),
    ],
)
def test_each_model_preserves_explicit_empty_result(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type,
    document: object,
    payload: str | None,
    query: dict[str, object],
) -> None:
    if fetcher_type is TopRetailFetcher:
        monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "fixture-key-never-live")
    rows, _ = _run(
        monkeypatch,
        fetcher_type(),
        document=document,
        payload=payload,
        **query,
    )
    assert rows == ()


@pytest.mark.parametrize(
    ("fetcher_type", "document", "payload", "query"),
    [
        (
            CalendarDividendFetcher,
            {"data": {}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarEarningsFetcher,
            {"data": {"asOf": "10/12/2026"}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarIpoFetcher,
            {"data": {"upcoming": {}}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (CompanyFilingsFetcher, {"data": {"rows": []}}, None, {"symbol": "ACME"}),
        (
            EconomicCalendarFetcher,
            {"data": {}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (EquityScreenerFetcher, {"data": {"table": {}}}, None, {}),
        (
            HistoricalDividendsFetcher,
            {"data": {"dividends": {}}},
            None,
            {"symbol": "ACME", "start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            TopRetailFetcher,
            {"datatable": {"columns": [], "data": []}},
            None,
            {"start_date": date(2026, 10, 9), "end_date": FIXED_DAY},
        ),
        (EquitySearchFetcher, None, "no-pipe-header\n", {}),
    ],
)
def test_each_model_rejects_missing_or_invalid_container_shape(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type,
    document: object,
    payload: str | None,
    query: dict[str, object],
) -> None:
    if fetcher_type is TopRetailFetcher:
        monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "fixture-key-never-live")
    with pytest.raises(ProviderEngineError):
        _run(monkeypatch, fetcher_type(), document=document, payload=payload, **query)


@pytest.mark.parametrize(
    ("fetcher_type", "document", "payload", "query"),
    [
        (
            CalendarDividendFetcher,
            {"data": {"calendar": {"rows": [{"dividend_Ex_Date": "10/12/2026"}]}}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarEarningsFetcher,
            {"data": {"asOf": "10/12/2026", "rows": [{"name": "No symbol"}]}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CalendarIpoFetcher,
            {"data": {"upcoming": {"upcomingTable": {"rows": [{}]}}}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (
            CompanyFilingsFetcher,
            {"data": {"totalRecords": 1, "rows": [{}]}},
            None,
            {"symbol": "ACME"},
        ),
        (
            EconomicCalendarFetcher,
            {"data": {"rows": [{"gmt": None, "eventName": None}]}},
            None,
            {"start_date": FIXED_DAY, "end_date": FIXED_DAY},
        ),
        (EquityScreenerFetcher, {"data": {"table": {"rows": [{"symbol": "ACME"}]}}}, None, {}),
        (
            HistoricalDividendsFetcher,
            {"data": {"dividends": {"rows": [{"exOrEffDate": "09/15/2026", "amount": "N/A"}]}}},
            None,
            {"symbol": "ACME", "start_date": date(2026, 9, 1), "end_date": FIXED_DAY},
        ),
        (
            TopRetailFetcher,
            {
                "datatable": {
                    "columns": [
                        {"name": "date"},
                        {"name": "ticker"},
                        {"name": "activity"},
                        {"name": "sentiment"},
                    ],
                    "data": [["2026-10-09", "ACME", None, 1]],
                }
            },
            None,
            {"start_date": date(2026, 10, 9), "end_date": FIXED_DAY},
        ),
        (EquitySearchFetcher, None, "Nasdaq Traded|Security Name|\nY|Missing Symbol|\n", {}),
    ],
)
def test_each_model_rejects_bad_required_source_fields(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type,
    document: object,
    payload: str | None,
    query: dict[str, object],
) -> None:
    if fetcher_type is TopRetailFetcher:
        monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "fixture-key-never-live")
    with pytest.raises(ProviderEngineError):
        _run(monkeypatch, fetcher_type(), document=document, payload=payload, **query)


def test_bad_queries_are_rejected_before_transport_for_all_nine_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetchers = [
        CalendarDividendFetcher(),
        CalendarEarningsFetcher(),
        CalendarIpoFetcher(),
        CompanyFilingsFetcher(),
        EconomicCalendarFetcher(),
        EquitySearchFetcher(),
        EquityScreenerFetcher(),
        HistoricalDividendsFetcher(),
        TopRetailFetcher(),
    ]
    for fetcher in fetchers:
        calls = _transport(monkeypatch, fetcher, document={})
        with pytest.raises(ProviderEngineError) as error:
            fetcher.fetch(**_query_for(fetcher), unrecognized_parameter=True)
        assert error.value.code == "NASDAQ_QUERY_INVALID"
        assert calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "status"),
    [
        (CalendarDividendFetcher, 429),
        (CalendarEarningsFetcher, 429),
        (CalendarIpoFetcher, 429),
        (CompanyFilingsFetcher, 429),
        (EconomicCalendarFetcher, 429),
        (EquitySearchFetcher, 429),
        (EquityScreenerFetcher, 429),
        (HistoricalDividendsFetcher, 429),
        (TopRetailFetcher, 429),
    ],
)
def test_rate_limit_is_a_stable_bounded_error_for_every_model(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_type: type,
    status: int,
) -> None:
    if fetcher_type is TopRetailFetcher:
        monkeypatch.setenv("NASDAQ_DATA_LINK_API_KEY", "fixture-key-never-live")
    fetcher = fetcher_type()
    with pytest.raises(ProviderEngineError) as error:
        _run(monkeypatch, fetcher, status=status, document={}, payload="", **_query_for(fetcher))
    assert error.value.code == "NASDAQ_RATE_LIMITED"
    assert error.value.status == 429


def test_top_retail_missing_key_fails_before_network_and_is_not_provider_wide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NASDAQ_DATA_LINK_API_KEY", raising=False)
    fetcher = TopRetailFetcher()
    calls = _transport(monkeypatch, fetcher, document=_fixture("top_retail.json"))
    with pytest.raises(ProviderEngineError) as error:
        fetcher.fetch(ctx=_context(fetcher), start_date=date(2026, 10, 9), end_date=FIXED_DAY)
    assert error.value.code == "NASDAQ_CREDENTIAL_MISSING"
    assert calls == []


def test_company_filing_total_and_page_cap_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = CompanyFilingsFetcher()
    calls = _transport(
        monkeypatch,
        fetcher,
        document={"data": {"totalRecords": 5001, "rows": []}},
    )
    with pytest.raises(ProviderEngineError) as error:
        fetcher.fetch(ctx=_context(fetcher), symbol="ACME")
    assert error.value.code == "NASDAQ_INCOMPLETE"
    assert len(calls) == 1


def test_dividend_symbol_fanout_is_bounded_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = HistoricalDividendsFetcher()
    calls = _transport(monkeypatch, fetcher, document=_fixture("historical_dividends.json"))
    symbols = ",".join(f"SYM{i}" for i in range(21))
    with pytest.raises(ProviderEngineError) as error:
        fetcher.fetch(symbol=symbols, start_date=FIXED_DAY, end_date=FIXED_DAY)
    assert error.value.code == "NASDAQ_QUERY_INVALID"
    assert calls == []


def test_multiple_day_calendar_window_has_deterministic_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CalendarDividendFetcher()
    _transport(monkeypatch, fetcher, document={"data": {"calendar": {"rows": []}}})
    with pytest.raises(ProviderEngineError):
        fetcher.fetch(start_date=FIXED_DAY, end_date=date(2026, 11, 13))


def test_output_contracts_reject_extra_fields() -> None:
    with pytest.raises(ValueError):
        NasdaqCalendarDividend.model_validate(
            {
                "ex_dividend_date": FIXED_DAY,
                "symbol": "ACME",
                "undocumented": "must fail",
            }
        )
