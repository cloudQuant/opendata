"""Offline contract tests for the ten FMP financial fetcher identities."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.models.fmp_financials import (
    BalanceSheet,
    BalanceSheetGrowth,
    CashFlowStatement,
    CashFlowStatementGrowth,
    FinancialRatios,
    FMPFinancialPeriod,
    IncomeStatement,
    IncomeStatementGrowth,
    KeyMetrics,
    RevenueBusinessLine,
    RevenueGeographic,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp.models import iter2_financials as financials
from opendata.data.providers.fmp.models._client import FMPProviderError
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

EVIDENCE = Path(__file__).parents[1] / "docs/evidence/C100/fmp-financials"

CASES = (
    (
        financials.BalanceSheetFetcher,
        BalanceSheet,
        "BalanceSheet",
        "balance-sheet-statement",
        "cashAndCashEquivalents",
    ),
    (
        financials.BalanceSheetGrowthFetcher,
        BalanceSheetGrowth,
        "BalanceSheetGrowth",
        "balance-sheet-statement-growth",
        "growthCashAndCashEquivalents",
    ),
    (
        financials.CashFlowStatementFetcher,
        CashFlowStatement,
        "CashFlowStatement",
        "cash-flow-statement",
        "netCashProvidedByOperatingActivities",
    ),
    (
        financials.CashFlowStatementGrowthFetcher,
        CashFlowStatementGrowth,
        "CashFlowStatementGrowth",
        "cash-flow-statement-growth",
        "growthNetIncome",
    ),
    (
        financials.IncomeStatementFetcher,
        IncomeStatement,
        "IncomeStatement",
        "income-statement",
        "revenue",
    ),
    (
        financials.IncomeStatementGrowthFetcher,
        IncomeStatementGrowth,
        "IncomeStatementGrowth",
        "income-statement-growth",
        "growthRevenue",
    ),
    (
        financials.FinancialRatiosFetcher,
        FinancialRatios,
        "FinancialRatios",
        "ratios",
        "grossProfitMargin",
    ),
    (financials.KeyMetricsFetcher, KeyMetrics, "KeyMetrics", "key-metrics", "marketCap"),
    (
        financials.RevenueBusinessLineFetcher,
        RevenueBusinessLine,
        "RevenueBusinessLine",
        "revenue-product-segmentation",
        "Mac",
    ),
    (
        financials.RevenueGeographicFetcher,
        RevenueGeographic,
        "RevenueGeographic",
        "revenue-geographic-segmentation",
        "Americas Segment",
    ),
)


def _fixture(name: str) -> list[dict[str, Any]]:
    """Load the independently hand-authored response fixture for one identity."""
    return json.loads((EVIDENCE / "fixtures" / f"{name}.json").read_text())


def _context(canonical_model: str | None) -> FetchContext:
    """Grant a single offline FMP transport attempt for the exact fetcher model."""
    assert canonical_model is not None
    grant = RequestGrant(
        source="fmp",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline financial fixture test grant",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    budget = RequestBudget(task_attempts=2, source_attempts=2, grants=(grant,))
    return FetchContext(timeout=1.0, request_budget=budget)


def _fetch(fetcher_type: type[Any], **kwargs: object) -> Any:
    """Run through Fetcher.fetch with an explicit offline-only test grant."""
    return fetcher_type().fetch(ctx=_context(fetcher_type.canonical_model), **kwargs)


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_each_fixed_identity_has_a_typed_offline_response_fixture(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each independent FMP sample maps through its exact route to its own model."""
    raw = _fixture(name)
    calls: list[tuple[str, dict[str, str], float | None]] = []

    def fake_fetch_array(
        called_endpoint: str,
        params: dict[str, str],
        timeout: float | None,
    ) -> list[dict[str, Any]]:
        calls.append((called_endpoint, params, timeout))
        return deepcopy(raw)

    monkeypatch.setattr(financials, "fetch_array", fake_fetch_array)
    rows = _fetch(fetcher_type, symbol="aapl", period="annual")

    assert len(rows) >= 1
    assert all(type(row) is model_type for row in rows)
    assert all(row.symbol == "AAPL" and row.fiscal_period == "FY" for row in rows)
    assert calls == [
        (
            endpoint,
            {"symbol": "AAPL", "period": "annual", "limit": "5"}
            if endpoint not in ("revenue-product-segmentation", "revenue-geographic-segmentation")
            else {"symbol": "AAPL", "period": "annual", "structure": "flat"},
            1.0,
        )
    ]
    row = rows[0]
    public = row.model_dump(mode="json", by_alias=False)
    if name == "BalanceSheet":
        assert public["prepaid_expenses"] == 1842000000
        assert public["plant_property_equipment_net"] == 116733000000
        assert public["non_current_assets"] == 211284000000
        assert public["accounts_payable"] == 64150000000
        assert public["current_deferred_revenue"] == 7806000000
        assert public["other_total_shareholders_equity"] == 1800000000
        assert public["total_liabilities_and_total_equity"] == 359241000000
        assert public["other_shareholders_equity"] is None
        assert "prepaids" not in public
    elif name == "BalanceSheetGrowth":
        assert public["growth_prepaid_expenses"] == 0.13
        assert public["growth_total_shareholders_equity"] == 0.29
        assert public["growth_total_equity"] == 0.30
        assert "growth_total_common_equity" not in public
        assert "growth_total_equity_non_controlling_interests" not in public
    elif name == "CashFlowStatement":
        assert public["net_cash_from_operating_activities"] == 111482000000
        assert public["purchase_of_property_plant_and_equipment"] == -12715000000
        assert public["net_cash_from_investing_activities"] == 15195000000
        assert public["net_cash_from_financing_activities"] == -120686000000
        assert public["repayment_of_debt"] is None
        assert "netCashProvidedByOperatingActivities" not in public
    elif name == "CashFlowStatementGrowth":
        assert public["growth_net_cash_from_operating_activities"] == pytest.approx(
            -0.05726656180763441
        )
        assert public["growth_net_equity_issuance"] == pytest.approx(0.04463448798828845)
        assert "growth_net_stock_issuance" not in public
    elif name == "IncomeStatement":
        assert public["basic_earnings_per_share"] == 7.49
        assert public["diluted_earnings_per_share"] == 7.46
        assert public["consolidated_net_income"] == 112010000000
        assert "eps" not in public
    elif name == "IncomeStatementGrowth":
        assert public["growth_gross_profit_margin"] == 0.015
    elif name == "FinancialRatios":
        assert public["currency"] == "USD"
        assert public["price_to_earnings"] == 34.1068090787717
        assert public["dividend_yield"] == 0.0125
        assert public["dividend_yield_percentage"] == 1.25
        assert "price_to_earnings_ratio" not in public
        assert "reported_currency" not in public
    elif name == "KeyMetrics":
        assert public["currency"] == "USD"
        assert "reported_currency" not in public
        assert "reported_currency" not in public
    elif name in ("RevenueBusinessLine", "RevenueGeographic"):
        assert public["filing_date"] is None
        assert "reported_currency" not in public
    assert numeric_key


def test_balance_sheet_accepts_legacy_documented_equity_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy FMP documentation uses a distinct lower-case spelling."""
    raw = _fixture("BalanceSheet")
    raw[0]["othertotalStockholdersEquity"] = raw[0].pop("otherTotalStockholdersEquity")
    raw[0]["totalLiabilitiesAndStockholdersEquity"] = raw[0].pop("totalLiabilitiesAndTotalEquity")
    monkeypatch.setattr(financials, "fetch_array", lambda *args, **kwargs: raw)

    row = _fetch(financials.BalanceSheetFetcher, symbol="AAPL", period="annual")[0]

    assert row.other_total_shareholders_equity == 1800000000
    assert row.total_liabilities_and_shareholders_equity == 359241000000


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_bad_query_is_rejected_before_provider_io(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(financials, "fetch_array", lambda *args: calls.append("called"))

    with pytest.raises(FMPProviderError, match="FMP_BAD_QUERY"):
        fetcher_type().fetch(symbol="AAPL", period="monthly")

    assert calls == []


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_missing_required_period_key_is_rejected(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture(name)
    del raw[0]["date"]
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(fetcher_type, symbol="AAPL", period="annual")


def test_nullable_public_period_metadata_is_still_required_to_validate_a_fetched_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_metadata = FMPFinancialPeriod.model_validate(
        {"symbol": None, "date": None, "fiscalYear": None, "period": None}
    )
    assert public_metadata.symbol is None
    assert public_metadata.period_ending is None
    assert public_metadata.fiscal_year is None
    assert public_metadata.fiscal_period is None

    raw = _fixture("IncomeStatement")
    raw[0]["fiscalYear"] = None
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)
    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(financials.IncomeStatementFetcher, symbol="AAPL")


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_non_numeric_value_and_unexpected_unit_encoding_fail_closed(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture(name)
    if name == "RevenueBusinessLine":
        raw[0]["data"][numeric_key] = "33708000000"
    elif name == "RevenueGeographic":
        raw[0]["data"][numeric_key] = "178353000000"
    else:
        raw[0][numeric_key] = "1000"
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(fetcher_type, symbol="AAPL", period="annual")


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_unmodeled_source_field_is_rejected(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture(name)
    if name in ("RevenueBusinessLine", "RevenueGeographic"):
        raw[0]["providerFutureField"] = "not part of the documented envelope"
    else:
        raw[0]["providerFutureField"] = 1
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(fetcher_type, symbol="AAPL", period="annual")


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_period_end_requires_an_exact_source_date(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture(name)
    raw[0]["date"] = "2025-09-27T00:00:00Z"
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(fetcher_type, symbol="AAPL", period="annual")


@pytest.mark.parametrize("fetcher_type,model_type,name,endpoint,numeric_key", CASES)
def test_requested_frequency_must_match_source_period(
    fetcher_type: type[Any],
    model_type: type[Any],
    name: str,
    endpoint: str,
    numeric_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture(name)
    raw[0]["period"] = "Q1"
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_FREQUENCY_MISMATCH"):
        _fetch(fetcher_type, symbol="AAPL", period="annual")


def test_as_of_and_unsupported_segmentation_limit_are_rejected_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(financials, "fetch_array", lambda *args: calls.append("called"))

    with pytest.raises(FMPProviderError, match="FMP_BAD_QUERY"):
        financials.RevenueBusinessLineFetcher().fetch(symbol="AAPL", as_of="2025-09-27")
    with pytest.raises(FMPProviderError, match="FMP_BAD_QUERY"):
        financials.RevenueGeographicFetcher().fetch(symbol="AAPL", limit=20)
    with pytest.raises(FMPProviderError, match="FMP_BAD_QUERY"):
        financials.BalanceSheetFetcher().fetch(symbol="AAPL", limit=1001)
    with pytest.raises(FMPProviderError, match="FMP_BAD_QUERY"):
        financials.FinancialRatiosFetcher().fetch(
            symbol="AAPL",
            start_date="2025-01-01",
            end_date="2025-12-31",
        )

    assert calls == []


def test_upstream_returning_more_rows_than_the_requested_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture("BalanceSheet")
    extra = deepcopy(raw[0])
    extra["date"] = "2024-09-28"
    extra["fiscalYear"] = "2024"
    raw.append(extra)
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_PROVIDER_LIMIT_VIOLATION"):
        _fetch(financials.BalanceSheetFetcher, symbol="AAPL", limit=1)


def test_rate_limit_error_remains_sanitized_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    def rate_limited(*args: object) -> list[dict[str, Any]]:
        raise FMPProviderError("FMP_RATE_LIMITED", status=429)

    monkeypatch.setattr(financials, "fetch_array", rate_limited)

    with pytest.raises(FMPProviderError, match="FMP_RATE_LIMITED status=429") as captured:
        _fetch(financials.IncomeStatementFetcher, symbol="AAPL")
    assert "AAPL" not in str(captured.value)


def test_growth_values_and_financial_amounts_keep_source_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    growth_fixture = _fixture("IncomeStatementGrowth")
    monkeypatch.setattr(financials, "fetch_array", lambda *args: growth_fixture)
    growth_rows = _fetch(financials.IncomeStatementGrowthFetcher, symbol="AAPL")
    assert growth_rows[0].growth_revenue == pytest.approx(0.0642551178283274)
    assert type(growth_rows[0].growth_revenue) is float
    assert growth_rows[0].growth_basic_earings_per_share == pytest.approx(0.22585924713584285)

    statement_fixture = _fixture("IncomeStatement")
    monkeypatch.setattr(financials, "fetch_array", lambda *args: statement_fixture)
    statement_rows = _fetch(financials.IncomeStatementFetcher, symbol="AAPL")
    assert statement_rows[0].revenue == 416161000000
    assert type(statement_rows[0].revenue) is int

    monkeypatch.setattr(financials, "fetch_array", lambda *args: _fixture("FinancialRatios"))
    ratios = _fetch(financials.FinancialRatiosFetcher, symbol="AAPL")
    assert type(ratios[0].gross_profit_margin) is float


def test_statement_amounts_match_public_integer_type_and_reject_fractional_amounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture("IncomeStatement")
    raw[0]["revenue"] = 1.5
    monkeypatch.setattr(financials, "fetch_array", lambda *args: raw)

    with pytest.raises(FMPProviderError, match="FMP_BAD_SHAPE"):
        _fetch(financials.IncomeStatementFetcher, symbol="AAPL")


def test_provider_model_query_injects_identity_and_serializes_public_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _fixture("IncomeStatement")
    monkeypatch.setattr(financials, "fetch_array", lambda *args: deepcopy(raw))
    fetcher = financials.IncomeStatementFetcher()
    capability = fetcher.capability
    observed: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        observed.update(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    descriptor = SimpleNamespace(
        source="fmp",
        model="IncomeStatement",
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
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, IncomeStatement),
    )

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="fmp",
            model="IncomeStatement",
            query={"symbol": "aapl", "period": "annual"},
            principal=Principal(),
            ctx=_context("IncomeStatement"),
        )
    )

    assert observed["source"] == "fmp"
    assert observed["market"] == "global"
    assert observed["symbol"] == "aapl"
    assert response["source"] == "fmp"
    assert response["model"] == "IncomeStatement"
    assert response["domain"] == capability.domain
    result = response["results"][0]
    assert result["basic_earnings_per_share"] == 7.49
    assert result["consolidated_net_income"] == 112010000000
    assert "eps" not in result


def test_revenue_maps_are_expanded_to_typed_segment_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(financials, "fetch_array", lambda *args: _fixture("RevenueBusinessLine"))
    business_rows = _fetch(financials.RevenueBusinessLineFetcher, symbol="AAPL")
    assert [row.business_line for row in business_rows] == [
        "Mac",
        "Service",
        "Wearables, Home and Accessories",
    ]
    assert business_rows[0].revenue == 33708000000

    monkeypatch.setattr(financials, "fetch_array", lambda *args: _fixture("RevenueGeographic"))
    geographic_rows = _fetch(financials.RevenueGeographicFetcher, symbol="AAPL")
    assert [row.region for row in geographic_rows] == [
        "Americas Segment",
        "Europe Segment",
        "Greater China Segment",
    ]
    assert geographic_rows[0].revenue == 178353000000


def test_capability_period_is_frequency_semantics_and_domains_keep_model_identity() -> None:
    capabilities = [fetcher_type.capability for fetcher_type, *_ in CASES]

    assert {capability.period for capability in capabilities} == {"periodic"}
    assert len({capability.domain for capability in capabilities}) == len(CASES)
    assert all(not capability.period.endswith("statement") for capability in capabilities)
