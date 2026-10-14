"""Offline contract and transport checks for the Intrinio financial shard."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from loguru import logger

from opendata.data import http_client
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models.intrinio_financials import (
    IntrinioBalanceSheet,
    IntrinioCashFlowStatement,
    IntrinioFinancialRatios,
    IntrinioForwardEbitdaEstimate,
    IntrinioForwardEpsEstimate,
    IntrinioForwardPeEstimate,
    IntrinioForwardSalesEstimate,
    IntrinioIncomeStatement,
    IntrinioKeyMetrics,
    IntrinioReportedFinancials,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.intrinio.models import iter2_financials as financials
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)


class _JsonSession(requests.Session):
    """Small offline requests transport that still traverses GovernedHttpClient."""

    def __init__(
        self,
        responses: list[tuple[int, object]],
        *,
        expected_api_key: str = "offline-test-key",
    ) -> None:
        super().__init__()
        self.responses = list(responses)
        self.expected_api_key = expected_api_key
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        headers = kwargs.get("headers")
        params = kwargs.get("params")
        assert isinstance(headers, dict)
        assert headers.get("Authorization") == f"Bearer {self.expected_api_key}"
        assert self.expected_api_key not in url
        assert self.expected_api_key not in str(params)
        assert "api_key" not in (params or {})
        if not self.responses:
            raise AssertionError("unexpected extra transport request")
        status, document = self.responses.pop(0)
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.encoding = "utf-8"
        response._content = json.dumps(document, allow_nan=False).encode("utf-8")
        return response


def _grant_context(model: str, *, attempts: int = 10) -> FetchContext:
    source_attempts = min(attempts, 3)
    grant = RequestGrant(
        source="intrinio",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:governed-fake-transport",
        task_attempts=attempts,
        source_attempts=source_attempts,
        allowed_hosts=frozenset({financials.INTRINIO_HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    return FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=attempts,
            source_attempts=source_attempts,
            grants=(grant,),
        ),
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, object]],
    *,
    expected_api_key: str = "offline-test-key",
) -> _JsonSession:
    session = _JsonSession(responses, expected_api_key=expected_api_key)
    monkeypatch.setattr(
        http_client, "get_shared_http_client", lambda: GovernedHttpClient(session=session)
    )
    return session


def _company(ticker: str = "AAPL") -> dict[str, object]:
    return {
        "id": f"com_{ticker.lower()}",
        "ticker": ticker,
        "name": "Apple Inc." if ticker == "AAPL" else "Microsoft Corporation",
        "cik": "0000320193" if ticker == "AAPL" else "0000789019",
    }


def _fundamental_list(
    statement_code: str, *, fiscal_period: str = "FY", ticker: str = "AAPL"
) -> dict[str, object]:
    return {
        "fundamentals": [
            {
                "id": "fund_AAPL_2024_FY",
                "statement_code": statement_code,
                "fiscal_year": 2024,
                "fiscal_period": fiscal_period,
                "type": "calculated" if statement_code == "calculations" else "reported",
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "filing_date": "2025-02-03T13:00:00Z",
                "is_latest": True,
            }
        ],
        "company": _company(ticker),
    }


def _standardized_detail(
    statement_code: str,
    tag: str,
    value: int | float,
    unit: str,
    *,
    fiscal_period: str = "FY",
    ticker: str = "AAPL",
) -> dict[str, object]:
    return {
        "standardized_financials": [
            {
                "data_tag": {
                    "id": f"tag_{tag}",
                    "name": tag.replace("_", " ").title(),
                    "tag": tag,
                    "sequence": 1,
                    "factor": "+",
                    "balance": "credit",
                    "unit": unit,
                },
                "value": value,
            }
        ],
        "fundamental": {
            "id": "fund_AAPL_2024_FY",
            "statement_code": statement_code,
            "fiscal_year": 2024,
            "fiscal_period": fiscal_period,
            "type": "calculated" if statement_code == "calculations" else "reported",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "filing_date": "2025-02-03T13:00:00Z",
            "is_latest": True,
            "company": _company(ticker),
        },
    }


@pytest.mark.parametrize(
    ("model", "fetcher_type", "statement_code", "tag", "value", "unit", "output_type", "field"),
    [
        (
            "BalanceSheet",
            financials.IntrinioBalanceSheetFetcher,
            "balance_sheet_statement",
            "cashandequivalents",
            51_000,
            "USD",
            IntrinioBalanceSheet,
            "cash_and_cash_equivalents",
        ),
        (
            "CashFlowStatement",
            financials.IntrinioCashFlowStatementFetcher,
            "cash_flow_statement",
            "netcashfromoperatingactivities",
            92_000,
            "USD",
            IntrinioCashFlowStatement,
            "net_cash_from_operating_activities",
        ),
        (
            "IncomeStatement",
            financials.IntrinioIncomeStatementFetcher,
            "income_statement",
            "basiceps",
            6.18,
            "USD per Share",
            IntrinioIncomeStatement,
            "basic_earnings_per_share",
        ),
        (
            "FinancialRatios",
            financials.IntrinioFinancialRatiosFetcher,
            "calculations",
            "currentratio",
            1.42,
            "Float",
            IntrinioFinancialRatios,
            "current_ratio",
        ),
        (
            "KeyMetrics",
            financials.IntrinioKeyMetricsFetcher,
            "calculations",
            "marketcap",
            3_100_000_000_000,
            "USD",
            IntrinioKeyMetrics,
            "market_cap",
        ),
    ],
)
def test_standardized_models_map_documented_tags_and_period_metadata(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type[object],
    statement_code: str,
    tag: str,
    value: int | float,
    unit: str,
    output_type: type[object],
    field: str,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(
        monkeypatch,
        [
            (200, _fundamental_list(statement_code)),
            (200, _standardized_detail(statement_code, tag, value, unit)),
        ],
    )

    query = {} if model == "ForwardPeEstimates" else {"limit": 1}
    rows = fetcher_type().fetch(ctx=_grant_context(model), symbol="AAPL", **query)

    assert len(rows) == 1
    assert isinstance(rows[0], output_type)
    assert getattr(rows[0], field) == value
    assert rows[0].symbol == "AAPL"
    assert rows[0].fundamental_id == "fund_AAPL_2024_FY"
    assert rows[0].period_ending.isoformat() == "2024-12-31"
    assert rows[0].fiscal_period == "FY"
    assert rows[0].fundamental_type == (
        "calculated" if statement_code == "calculations" else "reported"
    )
    assert rows[0].line_items[0].tag == tag
    json.dumps([row.model_dump(mode="json") for row in rows], allow_nan=False)
    assert len(session.calls) == 2


def test_balance_sheet_quarter_is_real_frequency_not_model_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    fundamental = _fundamental_list("balance_sheet_statement", fiscal_period="Q2")
    fundamental["fundamentals"][0]["id"] = "fund_AAPL_2025_Q2"
    fundamental["fundamentals"][0]["fiscal_year"] = 2025
    fundamental["fundamentals"][0]["start_date"] = "2025-04-01"
    fundamental["fundamentals"][0]["end_date"] = "2025-06-30"
    detail = _standardized_detail(
        "balance_sheet_statement", "cashandequivalents", 40_000, "usd", fiscal_period="Q2"
    )
    detail["fundamental"]["id"] = "fund_AAPL_2025_Q2"
    detail["fundamental"]["fiscal_year"] = 2025
    detail["fundamental"]["start_date"] = "2025-04-01"
    detail["fundamental"]["end_date"] = "2025-06-30"
    session = _install_transport(monkeypatch, [(200, fundamental), (200, detail)])

    rows = financials.IntrinioBalanceSheetFetcher().fetch(
        ctx=_grant_context("BalanceSheet"), symbol="AAPL", period="quarter", limit=1
    )

    assert rows[0].fiscal_period == "Q2"
    assert session.calls[0]["params"]["type"] == "QTR"


def test_reported_financials_keeps_source_fact_dimensions_and_filing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    list_page = _fundamental_list("income_statement")
    reported_page = {
        "reported_financials": [
            {
                "xbrl_tag": {
                    "tag": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                    "name": "Revenue from contracts",
                    "balance": "credit",
                    "unit": "USD",
                    "abstract": False,
                    "sequence": 2,
                    "depth": 1,
                },
                "value": 1000,
                "dimensions": [{"axis": "ProductAxis", "member": "DeviceMember"}],
            }
        ],
        "fundamental": {
            "id": "fund_AAPL_2024_FY",
            "statement_code": "income_statement",
            "fiscal_year": 2024,
            "fiscal_period": "FY",
            "type": "reported",
            "start_date": "2024-01-01",
            "end_date": "2024-12-31",
            "filing_date": "2025-02-03T13:00:00Z",
            "is_latest": True,
            "company": _company(),
        },
    }
    session = _install_transport(monkeypatch, [(200, list_page), (200, reported_page)])

    rows = financials.IntrinioReportedFinancialsFetcher().fetch(
        ctx=_grant_context("ReportedFinancials"), symbol="AAPL", statement_type="income", limit=1
    )

    assert len(rows) == 1
    assert isinstance(rows[0], IntrinioReportedFinancials)
    assert rows[0].fundamental_type == "reported"
    assert rows[0].line_items[0].tag.startswith("us-gaap_")
    assert rows[0].line_items[0].dimensions[0].member == "DeviceMember"
    assert rows[0].filed_at.isoformat() == "2025-02-03T13:00:00+00:00"
    assert len(session.calls) == 2


def test_reported_cash_flow_quarter_preserves_ytd_source_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    list_page = _fundamental_list("cash_flow_statement", fiscal_period="Q2YTD")
    list_page["fundamentals"][0]["id"] = "fund_AAPL_2025_Q2YTD"
    list_page["fundamentals"][0]["fiscal_year"] = 2025
    list_page["fundamentals"][0]["start_date"] = "2025-01-01"
    list_page["fundamentals"][0]["end_date"] = "2025-06-30"
    page = {
        "reported_financials": [
            {
                "xbrl_tag": {
                    "tag": "us-gaap_NetCashProvidedByUsedInOperatingActivities",
                    "unit": "USD",
                },
                "value": 500,
                "dimensions": [],
            }
        ],
        "fundamental": {
            "id": "fund_AAPL_2025_Q2YTD",
            "statement_code": "cash_flow_statement",
            "fiscal_year": 2025,
            "fiscal_period": "Q2YTD",
            "type": "reported",
            "start_date": "2025-01-01",
            "end_date": "2025-06-30",
            "filing_date": "2025-08-01T12:00:00Z",
            "is_latest": True,
            "company": _company(),
        },
    }
    session = _install_transport(monkeypatch, [(200, list_page), (200, page)])

    rows = financials.IntrinioReportedFinancialsFetcher().fetch(
        ctx=_grant_context("ReportedFinancials"),
        symbol="AAPL",
        statement_type="cash",
        period="quarter",
        limit=1,
    )

    assert rows[0].fiscal_period == "Q2YTD"
    assert session.calls[0]["params"]["type"] == "YTD"


@pytest.mark.parametrize(
    ("model", "fetcher_type", "response", "output_type", "expected"),
    [
        (
            "ForwardEpsEstimates",
            financials.IntrinioForwardEpsEstimatesFetcher,
            {
                "estimates": [
                    {
                        "messages": ["fiscal_quarter is deprecated, use fiscal_period instead"],
                        "company": _company(),
                        "date": "2025-12-31",
                        "fiscal_year": 2025,
                        "fiscal_period": "FY",
                        "fiscal_quarter": "FY",
                        "calendar_year": 2025,
                        "calendar_period": "Q4",
                        "calendar_quarter": "Q4",
                        "count": 5,
                        "mean": 2.1,
                        "median": 2.0,
                        "high": 3.0,
                        "low": 1.0,
                        "standard_deviation": 0.5,
                        "percent_change": 0.1,
                        "mean_7_days_ago": 2.0,
                        "mean_30_days_ago": 1.9,
                        "mean_60_days_ago": 1.8,
                        "mean_90_days_ago": 1.7,
                    }
                ]
            },
            IntrinioForwardEpsEstimate,
            {"mean_1w": 2.0, "mean_1m": 1.9, "mean_2m": 1.8, "mean_3m": 1.7},
        ),
        (
            "ForwardSalesEstimates",
            financials.IntrinioForwardSalesEstimatesFetcher,
            {
                "estimates": [
                    {
                        "messages": [],
                        "company": _company(),
                        "date": "2025-12-31",
                        "fiscal_year": 2025,
                        "fiscal_period": "FY",
                        "fiscal_quarter": "FY",
                        "calendar_year": 2025,
                        "calendar_period": "Q4",
                        "calendar_quarter": "Q4",
                        "count": 7,
                        "mean": 400000,
                        "median": 395000,
                        "high": 450000,
                        "low": 350000,
                        "standard_deviation": 30000,
                        "analyst_revisions_percent_change_1w": 0.02,
                        "analyst_revisions_up_1w": 2,
                        "analyst_revisions_down_1w": 1,
                        "analyst_revisions_percent_change_1m": 0.05,
                        "analyst_revisions_up_1m": 4,
                        "analyst_revisions_down_1m": 2,
                        "analyst_revisions_percent_change_3m": 0.08,
                        "analyst_revisions_up_3m": 6,
                        "analyst_revisions_down_3m": 3,
                    }
                ]
            },
            IntrinioForwardSalesEstimate,
            {"revisions_1w_up": 2, "revisions_1m_down": 2, "revisions_3m_change_percent": 0.08},
        ),
        (
            "ForwardEbitdaEstimates",
            financials.IntrinioForwardEbitdaEstimatesFetcher,
            {
                "ebitda_consensus": [
                    {
                        "ticker": "AAPL",
                        "company_name": "Apple Inc.",
                        "estimate_year": 2025,
                        "estimate_month": 12,
                        "period": "fy",
                        "consensus_type": "ebitda",
                        "updated_date": "2025-01-31",
                        "estimate_count": 6,
                        "high": 140000000,
                        "low": 125000000,
                        "mean": 132000000,
                        "std_dev": 5000000,
                    }
                ]
            },
            IntrinioForwardEbitdaEstimate,
            {"fiscal_period": None, "estimate_month": 12, "source_period": "fy"},
        ),
        (
            "ForwardPeEstimates",
            financials.IntrinioForwardPeEstimatesFetcher,
            {
                "ticker": "AAPL",
                "company_name": "Apple Inc.",
                "updated_date": "2025-01-31",
                "forward_pe_year1": 28.4,
                "forward_pe_year2": 26.2,
                "forward_pe_year3": 23.1,
                "forward_pe_year4": 21.0,
                "forward_pe_year5": 19.4,
                "forward_peg_ratio_year1": 2.1,
                "last_ttm_eps": 6.1,
            },
            IntrinioForwardPeEstimate,
            {"year1": 28.4, "eps_ttm": 6.1},
        ),
    ],
)
def test_estimate_models_map_official_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type[object],
    response: object,
    output_type: type[object],
    expected: dict[str, object],
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [(200, response)])

    query = {} if model == "ForwardPeEstimates" else {"limit": 1}
    rows = fetcher_type().fetch(ctx=_grant_context(model), symbol="AAPL", **query)

    assert len(rows) == 1
    assert isinstance(rows[0], output_type)
    for field, value in expected.items():
        assert getattr(rows[0], field) == value
    assert rows[0].symbol == "AAPL"
    json.dumps([row.model_dump(mode="json") for row in rows], allow_nan=False)
    assert len(session.calls) == 1


def test_bad_query_is_rejected_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTRINIO_API_KEY", raising=False)
    session = _install_transport(monkeypatch, [])

    with pytest.raises(financials.IntrinioFinancialError, match="BAD_QUERY"):
        financials.IntrinioBalanceSheetFetcher().fetch(
            ctx=_grant_context("BalanceSheet"), symbol=" AAPL ", period="ttm", limit=0
        )

    assert session.calls == []


@pytest.mark.parametrize("api_key", [None, "", "   "])
def test_missing_or_blank_api_key_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
) -> None:
    if api_key is None:
        monkeypatch.delenv("INTRINIO_API_KEY", raising=False)
    else:
        monkeypatch.setenv("INTRINIO_API_KEY", api_key)
    session = _install_transport(monkeypatch, [])

    with pytest.raises(financials.IntrinioFinancialError) as error:
        financials.IntrinioBalanceSheetFetcher().fetch(
            ctx=_grant_context("BalanceSheet"), symbol="AAPL", limit=1
        )

    assert error.value.code == "INTRINIO_FINANCIALS_CREDENTIAL_MISSING"
    assert session.calls == []


def test_credential_is_redacted_from_auth_failure_errors_urls_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "offline-fake-key-do-not-log"
    monkeypatch.setenv("INTRINIO_API_KEY", api_key)
    session = _install_transport(
        monkeypatch,
        [(401, {"error": "invalid key"})],
        expected_api_key=api_key,
    )
    log_output = StringIO()
    sink_id = logger.add(log_output, format="{message}")
    try:
        with pytest.raises(financials.IntrinioFinancialError) as error:
            financials.IntrinioBalanceSheetFetcher().fetch(
                ctx=_grant_context("BalanceSheet"), symbol="AAPL", limit=1
            )
    finally:
        logger.remove(sink_id)

    assert error.value.code == "INTRINIO_FINANCIALS_CREDENTIAL_REJECTED"
    assert error.value.url is None
    assert api_key not in str(error.value)
    assert api_key not in log_output.getvalue()
    assert len(session.calls) == 1
    call = session.calls[0]
    assert api_key not in str(call["url"])
    assert api_key not in str(call["params"])
    assert call["headers"]["Authorization"] == f"Bearer {api_key}"  # type: ignore[index]


@pytest.mark.parametrize(
    ("model", "fetcher_type", "statement_code", "tag", "value"),
    [
        (
            "BalanceSheet",
            financials.IntrinioBalanceSheetFetcher,
            "balance_sheet_statement",
            "cashandequivalents",
            100,
        ),
        (
            "CashFlowStatement",
            financials.IntrinioCashFlowStatementFetcher,
            "cash_flow_statement",
            "netcashfromoperatingactivities",
            100,
        ),
        (
            "IncomeStatement",
            financials.IntrinioIncomeStatementFetcher,
            "income_statement",
            "basiceps",
            1.2,
        ),
        (
            "FinancialRatios",
            financials.IntrinioFinancialRatiosFetcher,
            "calculations",
            "currentratio",
            1.4,
        ),
        (
            "KeyMetrics",
            financials.IntrinioKeyMetricsFetcher,
            "calculations",
            "marketcap",
            1000,
        ),
    ],
)
def test_each_standardized_identity_rejects_mapped_tag_with_wrong_unit(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type[object],
    statement_code: str,
    tag: str,
    value: int | float,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(
        monkeypatch,
        [
            (200, _fundamental_list(statement_code)),
            (
                200,
                _standardized_detail(statement_code, tag, value, "shares"),
            ),
        ],
    )

    with pytest.raises(financials.IntrinioFinancialError, match="TAG_UNIT_MISMATCH"):
        fetcher_type().fetch(ctx=_grant_context(model), symbol="AAPL", limit=1)

    assert len(session.calls) == 2


@pytest.mark.parametrize(
    ("model", "fetcher_type", "response", "query"),
    [
        (
            "BalanceSheet",
            financials.IntrinioBalanceSheetFetcher,
            _fundamental_list("balance_sheet_statement", ticker="MSFT"),
            {"limit": 1},
        ),
        (
            "CashFlowStatement",
            financials.IntrinioCashFlowStatementFetcher,
            _fundamental_list("cash_flow_statement", ticker="MSFT"),
            {"limit": 1},
        ),
        (
            "IncomeStatement",
            financials.IntrinioIncomeStatementFetcher,
            _fundamental_list("income_statement", ticker="MSFT"),
            {"limit": 1},
        ),
        (
            "FinancialRatios",
            financials.IntrinioFinancialRatiosFetcher,
            _fundamental_list("calculations", ticker="MSFT"),
            {"limit": 1},
        ),
        (
            "KeyMetrics",
            financials.IntrinioKeyMetricsFetcher,
            _fundamental_list("calculations", ticker="MSFT"),
            {"limit": 1},
        ),
        (
            "ReportedFinancials",
            financials.IntrinioReportedFinancialsFetcher,
            _fundamental_list("income_statement", ticker="MSFT"),
            {"limit": 1, "statement_type": "income"},
        ),
        (
            "ForwardEpsEstimates",
            financials.IntrinioForwardEpsEstimatesFetcher,
            {
                "estimates": [
                    {
                        "company": _company("MSFT"),
                        "date": "2025-12-31",
                        "fiscal_year": 2025,
                        "fiscal_period": "FY",
                        "mean": 2.0,
                        "median": 2.0,
                        "high": 2.4,
                        "low": 1.6,
                        "standard_deviation": 0.2,
                    }
                ]
            },
            {"limit": 1},
        ),
        (
            "ForwardSalesEstimates",
            financials.IntrinioForwardSalesEstimatesFetcher,
            {
                "estimates": [
                    {
                        "company": _company("MSFT"),
                        "date": "2025-12-31",
                        "fiscal_year": 2025,
                        "fiscal_period": "FY",
                        "mean": 2000,
                        "median": 2000,
                        "high": 2400,
                        "low": 1600,
                        "standard_deviation": 200,
                    }
                ]
            },
            {"limit": 1},
        ),
        (
            "ForwardEbitdaEstimates",
            financials.IntrinioForwardEbitdaEstimatesFetcher,
            {
                "ebitda_consensus": [
                    {
                        "ticker": "MSFT",
                        "estimate_year": 2025,
                        "estimate_month": 12,
                        "period": "fy",
                        "consensus_type": "ebitda",
                        "updated_date": "2025-01-31",
                        "estimate_count": 4,
                        "high": 1400,
                        "low": 1200,
                        "mean": 1300,
                        "std_dev": 50,
                    }
                ]
            },
            {"limit": 1},
        ),
        (
            "ForwardPeEstimates",
            financials.IntrinioForwardPeEstimatesFetcher,
            {
                "ticker": "MSFT",
                "updated_date": "2025-01-31",
                "forward_pe_year1": 28.4,
                "forward_pe_year2": 26.2,
                "forward_pe_year3": 23.1,
                "forward_pe_year4": 21.0,
                "forward_pe_year5": 19.4,
                "forward_peg_ratio_year1": 2.1,
                "last_ttm_eps": 6.1,
            },
            {},
        ),
    ],
)
def test_each_financial_identity_rejects_a_different_source_symbol(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher_type: type[object],
    response: object,
    query: dict[str, object],
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [(200, response)])

    with pytest.raises(financials.IntrinioFinancialError, match="SYMBOL_MISMATCH"):
        fetcher_type().fetch(ctx=_grant_context(model), symbol="AAPL", **query)

    assert len(session.calls) == 1


def test_missing_fundamental_period_metadata_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    bad_list = _fundamental_list("income_statement")
    del bad_list["fundamentals"][0]["fiscal_period"]
    session = _install_transport(monkeypatch, [(200, bad_list)])

    with pytest.raises(financials.IntrinioFinancialError, match="BAD_SHAPE"):
        financials.IntrinioIncomeStatementFetcher().fetch(
            ctx=_grant_context("IncomeStatement"), symbol="AAPL", limit=1
        )

    assert len(session.calls) == 1


def test_request_budget_preflight_and_rate_limit_are_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    session = _install_transport(monkeypatch, [(200, _fundamental_list("income_statement"))])

    with pytest.raises(RequestAttemptLimitError):
        financials.IntrinioIncomeStatementFetcher().fetch(
            ctx=_grant_context("IncomeStatement", attempts=1), symbol="AAPL", limit=1
        )
    assert session.calls == []

    monkeypatch.setattr(
        financials,
        "_http_get_json",
        lambda *args, **kwargs: financials.HttpResponse(429, {"message": "rate limited"}),
    )
    with pytest.raises(financials.IntrinioFinancialError, match="RATE_LIMITED"):
        financials.IntrinioIncomeStatementFetcher().fetch(
            ctx=_grant_context("IncomeStatement"), symbol="AAPL", limit=1
        )


def test_estimate_repeated_cursor_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    row = {
        "company": _company(),
        "date": "2025-12-31",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "calendar_year": 2025,
        "calendar_period": "FY",
        "count": 1,
        "mean": 1.0,
        "median": 1.0,
        "high": 1.0,
        "low": 1.0,
        "standard_deviation": None,
    }
    session = _install_transport(
        monkeypatch,
        [
            (200, {"estimates": [row], "next_page": "cursor-1"}),
            (200, {"estimates": [row], "next_page": "cursor-1"}),
        ],
    )

    with pytest.raises(financials.IntrinioFinancialError, match="PAGING_CURSOR_REPEATED"):
        financials.IntrinioForwardEpsEstimatesFetcher().fetch(
            ctx=_grant_context("ForwardEpsEstimates"), symbol="AAPL", limit=3
        )

    assert len(session.calls) == 2


def test_provider_model_query_injects_identity_and_serializes_typed_financial_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opendata.services import provider_model_query as service

    monkeypatch.setenv("INTRINIO_API_KEY", "offline-test-key")
    _install_transport(
        monkeypatch,
        [
            (200, _fundamental_list("income_statement")),
            (200, _standardized_detail("income_statement", "totalrevenue", 1000, "usd")),
        ],
    )
    fetcher = financials.IntrinioIncomeStatementFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="intrinio",
        model="IncomeStatement",
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
        service, "_resolve_fetcher", lambda registry, source, model: (descriptor, fetcher)
    )
    monkeypatch.setattr(
        service, "_reviewed_query_contract", lambda domain: (object(), IntrinioIncomeStatement)
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
            model="IncomeStatement",
            query={"symbol": "AAPL", "limit": 1, "source": "auto"},
            principal=SimpleNamespace(allows_domain=lambda domain: domain == capability.domain),
            ctx=_grant_context("IncomeStatement"),
        )
    )

    assert seen["source"] == "intrinio"
    assert seen["market"] == "us"
    assert result["results"][0]["revenue"] == 1000
    assert result["results"][0]["fundamental_type"] == "reported"
    json.dumps(result, allow_nan=False)
