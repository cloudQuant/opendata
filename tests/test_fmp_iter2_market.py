"""Offline official-shape tests for FMP Iteration 2 market models."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.fmp_iter2 import (
    EconomicCalendarEvent,
    IpoCalendarEvent,
    MarketRiskPremium,
    SplitsCalendarEvent,
    TreasuryRateObservation,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp import provider as fmp_provider
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models import iter2_financials
from opendata.data.providers.fmp.models.iter2 import (
    CalendarIpoFetcher,
    CalendarSplitsFetcher,
    EconomicCalendarFetcher,
    MarketRiskPremiumFetcher,
    TreasuryRatesFetcher,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


class _Response:
    def __init__(self, *, status: int, body: object) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(body, allow_nan=True)


class _HTTPClient:
    def __init__(self) -> None:
        self.responses: list[_Response] = []
        self.calls: list[tuple[str, dict[str, str], str]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
        source: str,
    ) -> _Response:
        del timeout
        assert source == "fmp"
        self.calls.append((url, dict(params), source))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


@pytest.fixture
def http_client(monkeypatch: pytest.MonkeyPatch) -> _HTTPClient:
    client = _HTTPClient()
    monkeypatch.setenv("FMP_API_KEY", "test-secret-key")
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)
    return client


def _context(model: str, *, attempts: int = 3) -> FetchContext:
    grant = RequestGrant(
        source="fmp",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline FMP fixture test grant",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=1.0,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=attempts,
            grants=(grant,),
        ),
    )


def _ok(body: object) -> _Response:
    return _Response(status=200, body=body)


def _official_ipo() -> dict[str, object]:
    # The response fields come from the official FMP example; dealSize was added
    # to the route on 2026-10-05 per the FMP changelog.
    return {
        "symbol": "IMC",
        "date": "2026-07-29",
        "daa": "2026-07-29T04:00:00.000Z",
        "company": "IMC Rare Earths Ltd",
        "exchange": "NYSE",
        "actions": "Priced",
        "shares": None,
        "priceRange": None,
        "marketCap": None,
        "dealSize": None,
    }


def _official_split() -> dict[str, object]:
    return {
        "symbol": "WHLR",
        "date": "2026-07-28",
        "numerator": 1,
        "denominator": 5,
        "splitType": "stock-split",
    }


def _official_economic_event() -> dict[str, object]:
    return {
        "date": "2026-07-29 03:30:00",
        "country": "SG",
        "event": "Import Prices YoY (Jun)",
        "currency": "SGD",
        "previous": 18.5,
        "estimate": 21,
        "actual": 13.6,
        "change": -4.9,
        "impact": "Low",
        "changePercentage": -26.486,
        "unit": "%",
    }


def _official_treasury_rate() -> dict[str, object]:
    return {
        "date": "2026-07-29",
        "month1": 3.73,
        "month2": 3.83,
        "month3": 3.83,
        "month6": 3.97,
        "year1": 4.04,
        "year2": 4.22,
        "year3": 4.29,
        "year5": 4.37,
        "year7": 4.51,
        "year10": 4.67,
        "year20": 5.21,
        "year30": 5.2,
    }


def _official_risk_premium() -> dict[str, object]:
    return {
        "country": "Colombia",
        "continent": "South America",
        "totalEquityRiskPremium": 7.89,
        "countryRiskPremium": 2.89,
    }


@pytest.mark.parametrize(
    ("fetcher_type", "model", "kwargs", "rows", "route", "request_params"),
    [
        (
            CalendarIpoFetcher,
            "CalendarIpo",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_ipo()],
            "ipos-calendar",
            {"from": "2026-07-01", "to": "2026-09-29", "apikey": "test-secret-key"},
        ),
        (
            CalendarSplitsFetcher,
            "CalendarSplits",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_split()],
            "splits-calendar",
            {"from": "2026-07-01", "to": "2026-09-29", "page": "0", "apikey": "test-secret-key"},
        ),
        (
            EconomicCalendarFetcher,
            "EconomicCalendar",
            {
                "start_date": "2026-07-01",
                "end_date": "2026-09-29",
                "country": "sg",
            },
            [_official_economic_event()],
            "economic-calendar",
            {
                "from": "2026-07-01",
                "to": "2026-09-29",
                "country": "SG",
                "apikey": "test-secret-key",
            },
        ),
        (
            TreasuryRatesFetcher,
            "TreasuryRates",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_treasury_rate()],
            "treasury-rates",
            {"from": "2026-07-01", "to": "2026-09-29", "apikey": "test-secret-key"},
        ),
        (
            MarketRiskPremiumFetcher,
            "RiskPremium",
            {},
            [_official_risk_premium()],
            "market-risk-premium",
            {"apikey": "test-secret-key"},
        ),
    ],
    ids=["ipo-calendar", "splits-calendar", "economic-calendar", "treasury-rates", "risk-premium"],
)
def test_official_shapes_use_documented_routes_and_parameters(
    http_client: _HTTPClient,
    fetcher_type: type,
    model: str,
    kwargs: dict[str, object],
    rows: list[dict[str, object]],
    route: str,
    request_params: dict[str, str],
) -> None:
    http_client.responses = [
        _ok(rows),
        *([_ok([])] if fetcher_type is CalendarSplitsFetcher else []),
    ]

    result = fetcher_type().fetch(ctx=_context(model), **kwargs)

    assert len(result) == 1
    assert [call[0] for call in http_client.calls] == [
        f"https://financialmodelingprep.com/stable/{route}"
    ] + (
        [f"https://financialmodelingprep.com/stable/{route}"]
        if fetcher_type is CalendarSplitsFetcher
        else []
    )
    assert http_client.calls[0][1] == request_params
    if fetcher_type is CalendarSplitsFetcher:
        assert http_client.calls[1][1] == {
            "from": "2026-07-01",
            "to": "2026-09-29",
            "page": "1",
            "apikey": "test-secret-key",
        }


@pytest.mark.parametrize(
    ("fetcher_type", "kwargs"),
    [
        (CalendarIpoFetcher, {"start_date": "2026-01-01", "end_date": "2026-04-02"}),
        (CalendarSplitsFetcher, {"start_date": "2026-07-01T00:00:00", "end_date": "2026-07-02"}),
        (
            EconomicCalendarFetcher,
            {"start_date": "2026-07-01", "end_date": "2026-07-02", "country": "U.S."},
        ),
        (TreasuryRatesFetcher, {"start_date": "2026-07-02", "end_date": "2026-07-01"}),
        (MarketRiskPremiumFetcher, {"start_date": "2026-07-01"}),
    ],
    ids=[
        "ipo-window-over-90",
        "split-time-bound",
        "bad-country-code",
        "treasury-reversed-window",
        "risk-premium-rejects-date",
    ],
)
def test_invalid_queries_fail_before_http(
    http_client: _HTTPClient,
    fetcher_type: type,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        fetcher_type().fetch(**kwargs)
    assert http_client.calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "model", "row", "bad_row"),
    [
        (CalendarIpoFetcher, "CalendarIpo", IpoCalendarEvent, {**_official_ipo(), "unknown": 1}),
        (
            CalendarSplitsFetcher,
            "CalendarSplits",
            SplitsCalendarEvent,
            {**_official_split(), "extra": 1},
        ),
        (
            EconomicCalendarFetcher,
            "EconomicCalendar",
            EconomicCalendarEvent,
            {**_official_economic_event(), "extra": 1},
        ),
        (
            TreasuryRatesFetcher,
            "TreasuryRates",
            TreasuryRateObservation,
            {**_official_treasury_rate(), "extra": 1},
        ),
        (
            MarketRiskPremiumFetcher,
            "RiskPremium",
            MarketRiskPremium,
            {**_official_risk_premium(), "extra": 1},
        ),
    ],
    ids=["ipo", "split", "economic", "treasury", "risk-premium"],
)
def test_contracts_reject_missing_and_unknown_fields(
    fetcher_type: type,
    model: str,
    row: type,
    bad_row: dict[str, object],
) -> None:
    del fetcher_type, model
    with pytest.raises(ValidationError):
        row.model_validate({})
    with pytest.raises(ValidationError):
        row.model_validate(bad_row)


@pytest.mark.parametrize(
    ("fetcher_type", "model", "query", "rows"),
    [
        (
            CalendarIpoFetcher,
            "CalendarIpo",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_ipo(), "date": "2026-10-01"}],
        ),
        (
            CalendarSplitsFetcher,
            "CalendarSplits",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_split(), "numerator": True}],
        ),
        (
            EconomicCalendarFetcher,
            "EconomicCalendar",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_economic_event(), "date": "2026-07-29"}],
        ),
        (
            TreasuryRatesFetcher,
            "TreasuryRates",
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [{**_official_treasury_rate(), "year10": float("nan")}],
        ),
        (
            MarketRiskPremiumFetcher,
            "RiskPremium",
            {},
            [{**_official_risk_premium(), "countryRiskPremium": "2.89"}],
        ),
    ],
    ids=[
        "ipo-out-of-range",
        "split-boolean-ratio",
        "economic-naive-date",
        "treasury-nan",
        "risk-numeric-string",
    ],
)
def test_fetchers_reject_out_of_range_or_wrong_source_values(
    http_client: _HTTPClient,
    fetcher_type: type,
    model: str,
    query: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    http_client.responses = [
        _ok(rows),
        *([_ok([])] if fetcher_type is CalendarSplitsFetcher else []),
    ]

    with pytest.raises(fmp_client.FMPProviderError) as error:
        fetcher_type().fetch(ctx=_context(model), **query)

    assert error.value.code in {
        "FMP_BAD_SHAPE",
        "FMP_OUT_OF_WINDOW_ROW",
    }
    assert "test-secret-key" not in str(error.value)


def test_calendar_splits_rejects_unfinished_page_budget(http_client: _HTTPClient) -> None:
    http_client.responses = [_ok([_official_split()])]
    with pytest.raises(fmp_client.FMPQueryLimitError) as error:
        CalendarSplitsFetcher().fetch(
            ctx=_context("CalendarSplits"),
            start_date="2026-07-01",
            end_date="2026-09-29",
            max_requests=1,
        )
    assert error.value.code == "FMP_INCOMPLETE_PAGE_BUDGET"


@pytest.mark.parametrize(
    ("model", "fetcher_type", "contract", "query", "rows"),
    [
        (
            "CalendarIpo",
            CalendarIpoFetcher,
            IpoCalendarEvent,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_ipo()],
        ),
        (
            "CalendarSplits",
            CalendarSplitsFetcher,
            SplitsCalendarEvent,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_split()],
        ),
        (
            "EconomicCalendar",
            EconomicCalendarFetcher,
            EconomicCalendarEvent,
            {"start_date": "2026-07-01", "end_date": "2026-09-29", "country": "SG"},
            [_official_economic_event()],
        ),
        (
            "TreasuryRates",
            TreasuryRatesFetcher,
            TreasuryRateObservation,
            {"start_date": "2026-07-01", "end_date": "2026-09-29"},
            [_official_treasury_rate()],
        ),
        (
            "RiskPremium",
            MarketRiskPremiumFetcher,
            MarketRiskPremium,
            {},
            [_official_risk_premium()],
        ),
    ],
    ids=["ipo", "splits", "economic", "treasury", "risk-premium"],
)
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    http_client: _HTTPClient,
    model: str,
    fetcher_type: type,
    contract: type,
    query: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    instance = fetcher_type()
    http_client.responses = [
        _ok(rows),
        *([_ok([])] if fetcher_type is CalendarSplitsFetcher else []),
    ]
    capability = instance.capability
    observed_query: dict[str, object] = {}
    original_transform_query = instance.transform_query

    def capture_query(**kwargs: object) -> object:
        observed_query.update(kwargs)
        return original_transform_query(**kwargs)

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
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, instance),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract),
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
    assert observed_query["source"] == "fmp"
    assert observed_query["market"] == capability.market
    assert response["results"] == [
        contract.model_validate(rows[0]).model_dump(mode="json", by_alias=False)
    ]


def test_fmp_provider_binds_market_slice_with_unique_identity_domains() -> None:
    bindings = fmp_provider.PROVIDER.fetcher_dict
    fetchers = fmp_provider.PROVIDER.fetchers
    expected = {
        "CalendarIpo": ("CalendarIpoFetcher", "fmp_ipo_calendar"),
        "CalendarSplits": ("CalendarSplitsFetcher", "fmp_splits_calendar"),
        "EconomicCalendar": ("EconomicCalendarFetcher", "fmp_economic_calendar"),
        "TreasuryRates": ("TreasuryRatesFetcher", "fmp_treasury_rates"),
        "RiskPremium": ("MarketRiskPremiumFetcher", "fmp_market_risk_premium"),
    }

    for model, (fetcher_name, _domain) in expected.items():
        assert bindings[model].class_name == fetcher_name
    assert {
        fetcher.canonical_model: fetcher.capability.domain
        for fetcher in fetchers
        if fetcher.canonical_model in expected
    } == {model: domain for model, (_fetcher, domain) in expected.items()}


@pytest.mark.parametrize(
    "fetcher_name,contract_name,fixture_name",
    [
        ("BalanceSheetFetcher", "BalanceSheet", "BalanceSheet"),
        ("BalanceSheetGrowthFetcher", "BalanceSheetGrowth", "BalanceSheetGrowth"),
        ("CashFlowStatementFetcher", "CashFlowStatement", "CashFlowStatement"),
        (
            "CashFlowStatementGrowthFetcher",
            "CashFlowStatementGrowth",
            "CashFlowStatementGrowth",
        ),
        ("IncomeStatementFetcher", "IncomeStatement", "IncomeStatement"),
        ("IncomeStatementGrowthFetcher", "IncomeStatementGrowth", "IncomeStatementGrowth"),
        ("FinancialRatiosFetcher", "FinancialRatios", "FinancialRatios"),
        ("KeyMetricsFetcher", "KeyMetrics", "KeyMetrics"),
        ("RevenueBusinessLineFetcher", "RevenueBusinessLine", "RevenueBusinessLine"),
        ("RevenueGeographicFetcher", "RevenueGeographic", "RevenueGeographic"),
    ],
    ids=[
        "balance-sheet",
        "balance-sheet-growth",
        "cash-flow",
        "cash-flow-growth",
        "income-statement",
        "income-statement-growth",
        "ratios",
        "key-metrics",
        "revenue-business-line",
        "revenue-geographic",
    ],
)
def test_provider_model_query_pins_financial_identity_and_serializes_contract_rows(
    monkeypatch: pytest.MonkeyPatch,
    fetcher_name: str,
    contract_name: str,
    fixture_name: str,
) -> None:
    """The normal query service injects FMP/global and serializes each financial contract."""
    fetcher_type = getattr(iter2_financials, fetcher_name)
    contract_type = fetcher_type.record_model
    fixture_path = (
        Path(__file__).parents[1]
        / "docs/evidence/C100/fmp-financials/fixtures"
        / f"{fixture_name}.json"
    )
    raw_rows = json.loads(fixture_path.read_text())
    instance = fetcher_type()
    capability = instance.capability
    observed_query: dict[str, object] = {}
    calls: list[tuple[str, dict[str, str]]] = []

    def capture_query(**kwargs: object) -> object:
        observed_query.update(kwargs)
        return original_transform_query(**kwargs)

    original_transform_query = instance.transform_query
    monkeypatch.setattr(instance, "transform_query", capture_query)

    def fake_fetch_array(
        endpoint: str,
        params: dict[str, str],
        timeout: float | None,
    ) -> list[dict[str, Any]]:
        del timeout
        calls.append((endpoint, params))
        return raw_rows

    monkeypatch.setattr(iter2_financials, "fetch_array", fake_fetch_array)
    descriptor = SimpleNamespace(
        source="fmp",
        model=contract_name,
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
        lambda *_args: (descriptor, instance),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, contract_type),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model=contract_name,
            query={"symbol": "aapl", "period": "annual"},
            principal=Principal(),
            ctx=_context(contract_name),
        )
    )

    assert observed_query["source"] == "fmp"
    assert observed_query["market"] == "global"
    assert response["source"] == "fmp"
    assert response["model"] == contract_name
    assert response["domain"] == capability.domain
    if contract_name in {"RevenueBusinessLine", "RevenueGeographic"}:
        query_model = original_transform_query(
            source="fmp",
            market="global",
            symbol="AAPL",
            period="annual",
        )
        expected_rows = instance.transform_data(raw_rows, query_model)
        expected = [row.model_dump(mode="json", by_alias=False) for row in expected_rows]
    else:
        expected = [
            contract_type.model_validate(row).model_dump(mode="json", by_alias=False)
            for row in raw_rows
        ]
    assert response["results"] == expected
    assert calls and calls[0][1]["symbol"] == "AAPL"
