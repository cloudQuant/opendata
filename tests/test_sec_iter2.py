"""Offline positive/negative source fixtures for SEC directory, XBRL, FTD and Form 4."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from pydantic import ValidationError

from opendata.data import http_client as http_client_module
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.sec_iter2 import (
    SecCikMapRecord,
    SecCompanyFactComparison,
    SecFtdRecord,
    SecInsiderTransaction,
    SecSymbolMapRecord,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.sec.models import _common as common
from opendata.data.providers.sec.models import iter2_directory as directory
from opendata.data.providers.sec.models import iter2_financial as financial
from opendata.data.providers.sec.models import iter2_ftd as ftd
from opendata.data.providers.sec.models import iter2_insider as insider
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

FIXTURES = Path(__file__).parent.parent / "docs/evidence/C100/sec/fixtures"
_COMPANYFACTS_URL = f"{financial.SEC_COMPANYFACTS_URL}/CIK0000320193.json"
_FIXED_MODELS = (
    "CikMap",
    "SymbolMap",
    "EquitySearch",
    "SicSearch",
    "BalanceSheet",
    "BalanceSheetGrowth",
    "CashFlowStatement",
    "CashFlowStatementGrowth",
    "IncomeStatement",
    "IncomeStatementGrowth",
    "CompareCompanyFacts",
    "EquityFTD",
    "InsiderTrading",
    "CompanyFilings",
    "Filings",
    "SecFiling",
    "SecHtmFile",
    "RssLitigation",
    "SchemaFiles",
    "LatestFinancialReports",
    "ManagementDiscussionAnalysis",
    "Form13FHR",
    "NportDisclosure",
)


def _fixture(model: str, filename: str) -> bytes:
    return (FIXTURES / model / filename).read_bytes()


def _json(model: str, filename: str) -> dict[str, Any]:
    return json.loads(_fixture(model, filename))


@pytest.mark.parametrize(
    ("fetcher", "query", "inherited_field"),
    (
        (directory.CikMapFetcher, {"symbol": "AAPL"}, "start_date"),
        (directory.SymbolMapFetcher, {"query": "Apple"}, "symbol"),
        (directory.EquitySearchFetcher, {"query": "Apple"}, "symbol"),
        (directory.SicSearchFetcher, {"query": "3571"}, "symbol"),
    ),
)
def test_directory_query_allowlists_keep_inherited_fields_closed(
    fetcher: Any,
    query: dict[str, object],
    inherited_field: str,
) -> None:
    """Accept injected service identity but preserve each model's allowlist."""
    instance = fetcher()
    validated = instance.transform_query(source="sec", market="us", **query)
    assert validated.source == "sec"
    assert validated.market == "us"

    for extra in ("not_a_query_field", inherited_field):
        with pytest.raises(ValidationError) as invalid:
            instance.transform_query(source="sec", market="us", **query, **{extra: "rejected"})
        assert [(error["type"], error["loc"]) for error in invalid.value.errors()] == [
            ("extra_forbidden", (extra,))
        ]


def _ctx() -> FetchContext:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grants = tuple(
        RequestGrant(
            source="sec",
            canonical_model=model,
            operation=RequestOperation.QUERY,
            decision=GrantDecision.ALLOWED,
            rights_evidence="test-fixture:offline-only:synthetic SEC response",
            task_attempts=32,
            source_attempts=32,
            allowed_hosts={"sec.gov", "www.sec.gov", "data.sec.gov"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            profile=profile,
        )
        for model in _FIXED_MODELS
    )
    budget = RequestBudget(
        task_attempts=32,
        source_attempts=32,
        grants=grants,
        profile=profile,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


class _SecFixtureSession(requests.Session):
    """Low-level offline session used through the real governed HTTP client."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        super().__init__()
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        del kwargs
        self.calls.append((method, url))
        status, content = self.routes.get(url, (404, b"not found"))
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.encoding = "utf-8"
        response.headers["content-type"] = "application/json"
        response._content = content
        response.request = requests.Request(method, url).prepare()
        return response


def _install_sec_fixture_client(
    monkeypatch: pytest.MonkeyPatch, content: bytes
) -> _SecFixtureSession:
    return _install_sec_fixture_routes(monkeypatch, {_COMPANYFACTS_URL: (200, content)})


def _install_sec_fixture_routes(
    monkeypatch: pytest.MonkeyPatch,
    routes: dict[str, tuple[int, bytes]],
) -> _SecFixtureSession:
    monkeypatch.setenv(common.SEC_USER_AGENT_ENV, "OpenData tests contact tests@example.com")
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    session = _SecFixtureSession(routes)
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    monkeypatch.setattr(common, "get_shared_http_client", lambda: client)
    return session


def _assets_companyfacts_document(*, cik: int = 320193) -> dict[str, Any]:
    return {
        "cik": cik,
        "facts": {
            "us-gaap": {
                "Assets": {
                    "label": "Assets",
                    "units": {
                        "USD": [
                            {
                                "end": "2025-09-27",
                                "filed": "2025-11-01",
                                "form": "10-K",
                                "accn": "0000320193-25-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "val": 359241000000,
                            }
                        ]
                    },
                }
            }
        },
    }


def _patch_json(monkeypatch: pytest.MonkeyPatch, module: Any, document: object) -> None:
    monkeypatch.setattr(module, "fetch_json", lambda _ctx, _url: document)


def test_cik_map_fixture_is_typed_and_malformed_directory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        directory,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("cik-map", "positive.json")),
    )
    rows = directory.CikMapFetcher().fetch(
        ctx=_ctx(), symbol="AAPL", offset=0, limit=1, market="us", source="sec"
    )
    assert type(rows[0]) is SecCikMapRecord
    assert rows[0].cik == "0000320193"
    assert rows[0].model_dump(mode="json")["ticker"] == "AAPL"

    monkeypatch.setattr(
        directory,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("cik-map", "negative.json")),
    )
    with pytest.raises(common.SecProviderError, match="SEC_TICKER_DIRECTORY_BAD_SHAPE"):
        directory.CikMapFetcher().fetch(ctx=_ctx(), symbol="AAPL")


def test_symbol_map_fixture_accepts_public_query_and_returns_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        directory,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("symbol-map", "positive.json")),
    )
    rows = directory.SymbolMapFetcher().fetch(ctx=_ctx(), query="Apple")
    assert type(rows[0]) is SecSymbolMapRecord
    assert rows[0].symbol == "AAPL"
    assert rows[0].ticker == "AAPL"
    assert rows[0].cik == "0000320193"
    assert directory.SymbolMapFetcher().fetch(ctx=_ctx(), query="0000000001") == ()

    monkeypatch.setattr(
        directory,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("symbol-map", "negative.json")),
    )
    with pytest.raises(common.SecProviderError, match="SEC_BAD_CIK"):
        directory.SymbolMapFetcher().fetch(ctx=_ctx(), query="Apple")


def test_equity_search_fixture_pagination_and_bad_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        directory,
        "exchange_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("equity-search", "positive.json")),
    )
    rows = directory.EquitySearchFetcher().fetch(ctx=_ctx(), query="apple", offset=0, limit=1)
    assert rows[0].exchange == "Nasdaq"
    assert directory.EquitySearchFetcher().fetch(ctx=_ctx(), query="missing") == ()

    monkeypatch.setattr(
        directory,
        "exchange_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("equity-search", "negative.json")),
    )
    with pytest.raises(common.SecProviderError, match="SEC_TICKER_DIRECTORY_BAD_SHAPE"):
        directory.EquitySearchFetcher().fetch(ctx=_ctx())


def test_equity_search_fund_mode_uses_official_sec_fund_ticker_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[2110, "S000009184", "C000024954", "LACAX"]],
    }
    url = directory.SEC_FUND_TICKERS_URL
    session = _install_sec_fixture_routes(
        monkeypatch, {url: (200, json.dumps(document).encode("utf-8"))}
    )

    rows = directory.EquitySearchFetcher().fetch(
        ctx=_ctx(), query="LACAX", is_symbol=True, is_fund=True
    )

    assert len(rows) == 1
    assert rows[0].symbol == "LACAX"
    assert rows[0].cik == "0000002110"
    assert rows[0].name is None
    assert rows[0].series_id == "S000009184"
    assert rows[0].class_id == "C000024954"
    assert session.calls == [("GET", url)]


@pytest.mark.parametrize(
    ("fetcher", "query"),
    (
        (directory.CikMapFetcher, {"symbol": "AAPL"}),
        (directory.SymbolMapFetcher, {"query": "Apple"}),
        (directory.EquitySearchFetcher, {"query": "Apple"}),
        (directory.SicSearchFetcher, {"query": "Electronic Computers"}),
    ),
)
def test_directory_models_reject_cache_bypass_before_request(
    fetcher: Any, query: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="use_cache=False is unsupported"):
        fetcher().fetch(ctx=_ctx(), **query, use_cache=False)


def test_sic_search_fixture_and_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        directory,
        "fetch_bytes",
        lambda *_args, **_kwargs: _fixture("sic-search", "positive.html"),
    )
    rows = directory.SicSearchFetcher().fetch(ctx=_ctx(), query="Electronic Computers")
    assert rows[0].sic == 3571
    assert rows[0].industry == "Electronic Computers"
    assert rows[0].sic_code == 3571
    assert rows[0].industry_title == "Electronic Computers"
    assert directory.SicSearchFetcher().fetch(ctx=_ctx(), query="9999") == ()

    monkeypatch.setattr(
        directory,
        "fetch_bytes",
        lambda *_args, **_kwargs: _fixture("sic-search", "negative.html"),
    )
    with pytest.raises(common.SecProviderError, match="SEC_SIC_EMPTY"):
        directory.SicSearchFetcher().fetch(ctx=_ctx(), query="3571")


@pytest.mark.parametrize(
    ("name", "fetcher", "expected_field", "expected_value", "negative_code"),
    (
        (
            "balance-sheet",
            financial.BalanceSheetFetcher,
            "total_assets",
            100.0,
            "SEC_XBRL_BAD_FACT",
        ),
        (
            "balance-sheet-growth",
            financial.BalanceSheetGrowthFetcher,
            "growth_total_assets",
            0.25,
            "SEC_XBRL_BAD_FACT",
        ),
        (
            "cash-flow-statement",
            financial.CashFlowStatementFetcher,
            "net_cash_from_operating_activities",
            80.0,
            "SEC_XBRL_BAD_FACT",
        ),
        (
            "cash-flow-statement-growth",
            financial.CashFlowStatementGrowthFetcher,
            "growth_net_cash_from_operating_activities",
            80.0 / 60.0 - 1.0,
            "missing-prior",
        ),
        (
            "income-statement",
            financial.IncomeStatementFetcher,
            "net_income",
            80.0,
            "SEC_XBRL_BAD_FACT",
        ),
        (
            "income-statement-growth",
            financial.IncomeStatementGrowthFetcher,
            "growth_net_income",
            80.0 / 60.0 - 1.0,
            "missing-prior",
        ),
    ),
)
def test_companyfacts_statement_models_have_independent_positive_negative_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    fetcher: type,
    expected_field: str,
    expected_value: float,
    negative_code: str,
) -> None:
    _patch_json(monkeypatch, financial, _json(name, "positive.json"))
    positive = fetcher().fetch(ctx=_ctx(), cik="320193", market="us", source="sec")
    assert positive
    assert all(row.period_ending for row in positive)
    if name.startswith(("cash-flow-statement", "income-statement")):
        assert all(row.period_beginning for row in positive)
    assert getattr(positive[0], expected_field) == pytest.approx(expected_value)

    _patch_json(monkeypatch, financial, _json(name, "negative.json"))
    if negative_code == "missing-prior":
        missing = fetcher().fetch(ctx=_ctx(), cik="320193")
        assert len(missing) == 1
        assert getattr(missing[0], expected_field) is None
    elif name == "balance-sheet":
        # The fixture has an instant Assets concept with a duration range, so it is excluded.
        assert fetcher().fetch(ctx=_ctx(), cik="320193") == ()
    else:
        with pytest.raises(common.SecProviderError, match=negative_code):
            fetcher().fetch(ctx=_ctx(), cik="320193")


def test_companyfacts_quarter_normalization_ttm_and_growth_require_contiguous_periods(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[dict[str, object]] = []
    annual_values = {
        2024: (5, 10, 15, 20),
        2025: (10, 25, 45, 70),
    }
    for year, (q1, q2, q3, annual) in annual_values.items():
        accession = f"0000320193-{str(year)[-2:]}-000001"
        filed = f"{year + 1}-02-01"
        period_rows = (
            (f"{year}-01-01", f"{year}-03-31", "Q1", q1),
            (f"{year}-01-01", f"{year}-06-30", "Q2", q2),
            (f"{year}-01-01", f"{year}-09-30", "Q3", q3),
            (f"{year}-01-01", f"{year}-12-31", "FY", annual),
        )
        observations.extend(
            {
                "start": start,
                "end": end,
                "filed": filed,
                "form": "10-K",
                "accn": accession,
                "fy": year,
                "fp": period,
                "val": value,
            }
            for start, end, period, value in period_rows
        )
    document = {
        "cik": 320193,
        "facts": {
            "us-gaap": {
                "NetCashProvidedByUsedInOperatingActivities": {
                    "label": "Net cash provided by operating activities",
                    "units": {"USD": observations},
                }
            }
        },
    }
    _patch_json(monkeypatch, financial, document)

    quarters = financial.CashFlowStatementFetcher().fetch(
        ctx=_ctx(), cik="320193", period="quarterly", limit=20
    )
    by_period = {row.period_ending: row for row in quarters}
    assert len(by_period) == 8
    assert [
        by_period[date(2025, month, day)].fiscal_period
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
    ] == ["Q1", "Q2", "Q3", "Q4"]
    assert [
        by_period[date(2025, month, day)].net_cash_from_operating_activities
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
    ] == [10.0, 15.0, 20.0, 25.0]
    q2_fact = next(
        fact
        for fact in by_period[date(2025, 6, 30)].sec_facts
        if fact.tag == "NetCashProvidedByUsedInOperatingActivities"
    )
    assert q2_fact.period_beginning == date(2025, 1, 1)
    assert q2_fact.value == 25
    assert by_period[date(2025, 6, 30)].period_beginning == date(2025, 4, 1)

    ttm = financial.CashFlowStatementFetcher().fetch(
        ctx=_ctx(), cik="320193", period="ttm", limit=20
    )
    ttm_2025 = next(row for row in ttm if row.period_ending == date(2025, 12, 31))
    assert ttm_2025.net_cash_from_operating_activities == 70.0

    quarterly_growth = financial.CashFlowStatementGrowthFetcher().fetch(
        ctx=_ctx(), cik="320193", period="quarterly", limit=20
    )
    q2_growth = next(row for row in quarterly_growth if row.period_ending == date(2025, 6, 30))
    assert q2_growth.growth_net_cash_from_operating_activities == pytest.approx(0.5)

    ttm_growth = financial.CashFlowStatementGrowthFetcher().fetch(
        ctx=_ctx(), cik="320193", period="ttm", limit=20
    )
    ttm_growth_2025 = next(row for row in ttm_growth if row.period_ending == date(2025, 12, 31))
    assert ttm_growth_2025.growth_net_cash_from_operating_activities == pytest.approx(2.5)


_TTM_ALIAS_CASES = {
    "balance": {
        "instant": True,
        "columns": {
            "CashAndCashEquivalentsAtCarryingValue": ("cash_and_equivalents", "currency"),
            "ShortTermInvestments": ("short_term_investments", "currency"),
        },
        "values": {
            "cash_and_equivalents": (10.0, 20.0, 30.0, 40.0),
            "short_term_investments": (1.0, 2.0, 3.0, 4.0),
        },
        "canonical": {"cash_and_equivalents": 25.0, "short_term_investments": 2.5},
        "alias": "cash_and_cash_equivalents",
        "derived": "cash_and_short_term_investments",
        "expected_alias": 25.0,
        "expected_derived": 27.5,
        "null_source": "cash_and_equivalents",
    },
    "income": {
        "instant": False,
        "columns": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": (
                "total_revenue",
                "currency",
            ),
        },
        "values": {"total_revenue": (10.0, 15.0, 20.0, 25.0)},
        "canonical": {"total_revenue": 70.0},
        "alias": "revenue",
        "derived": "revenue",
        "expected_alias": 70.0,
        "expected_derived": 70.0,
        "null_source": "total_revenue",
    },
    "cash_flow": {
        "instant": False,
        "columns": {
            "NetCashProvidedByUsedInOperatingActivities": (
                "net_cash_from_operating_activities",
                "currency",
            ),
            "PaymentsToAcquirePropertyPlantAndEquipment": (
                "purchase_of_plant_property_and_equipment",
                "currency",
            ),
        },
        "values": {
            "net_cash_from_operating_activities": (10.0, 15.0, 20.0, 25.0),
            "purchase_of_plant_property_and_equipment": (-1.0, -2.0, -3.0, -4.0),
        },
        "canonical": {
            "net_cash_from_operating_activities": 70.0,
            "purchase_of_plant_property_and_equipment": -10.0,
        },
        "alias": "operating_cash_flow",
        "derived": "free_cash_flow",
        "expected_alias": 70.0,
        "expected_derived": 60.0,
        "null_source": "net_cash_from_operating_activities",
    },
}


def _ttm_alias_quarters(
    kind: str,
    field_values: dict[str, tuple[float | None, ...]],
) -> tuple[dict[str, object], ...]:
    quarter_metadata = (
        (date(2024, 3, 31), date(2024, 1, 1), "Q1"),
        (date(2024, 6, 30), date(2024, 4, 1), "Q2"),
        (date(2024, 9, 30), date(2024, 7, 1), "Q3"),
        (date(2024, 12, 31), date(2024, 10, 1), "Q4"),
    )
    rows: list[dict[str, object]] = []
    for index, (period_ending, period_beginning, fiscal_period) in enumerate(quarter_metadata):
        row: dict[str, object] = {
            "period_ending": period_ending,
            "period_beginning": period_beginning,
            "fiscal_period": fiscal_period,
            "fiscal_year": 2024,
            "filing_date": date(2025, 2, 1),
            "cik": 320193,
            "symbol": "AAPL",
            "reported_currency": "USD",
            "accession": f"0000320193-25-00000{index + 1}",
            "sec_facts": (),
        }
        for field_name, values in field_values.items():
            row[field_name] = values[index]
        row["_field_sources"] = {
            field_name: ((period_ending, field_name, "USD"),)
            for field_name, values in field_values.items()
            if values[index] is not None
        }
        financial._add_statement_aliases(row, kind)
        rows.append(row)
    return tuple(rows)


def _ttm_alias_result(kind: str, values: dict[str, tuple[float | None, ...]]) -> dict[str, object]:
    case = _TTM_ALIAS_CASES[kind]
    rows = financial._ttm_rows(
        _ttm_alias_quarters(kind, values),
        columns=case["columns"],
        instant=case["instant"],
        kind=kind,
    )
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize("kind", ("balance", "income", "cash_flow"))
def test_ttm_rows_recompute_aliases_and_derived_fields_from_aggregates(kind: str) -> None:
    case = _TTM_ALIAS_CASES[kind]
    row = _ttm_alias_result(kind, case["values"])

    for field_name, expected in case["canonical"].items():
        assert row[field_name] == pytest.approx(expected)
    assert row[case["alias"]] == pytest.approx(case["expected_alias"])
    assert row[case["derived"]] == pytest.approx(case["expected_derived"])
    if kind == "cash_flow":
        # The previous implementation leaked the copied Q4 alias value (25) here.
        assert row["operating_cash_flow"] == pytest.approx(70.0)
        assert row["operating_cash_flow"] != pytest.approx(25.0)


@pytest.mark.parametrize("kind", ("balance", "income", "cash_flow"))
@pytest.mark.parametrize("null_quarter", range(4))
def test_ttm_rows_clear_aliases_when_any_quarter_is_null(kind: str, null_quarter: int) -> None:
    case = _TTM_ALIAS_CASES[kind]
    values = {field: tuple(observations) for field, observations in case["values"].items()}
    source_field = case["null_source"]
    source_values = list(values[source_field])
    source_values[null_quarter] = None
    values[source_field] = tuple(source_values)

    row = _ttm_alias_result(kind, values)

    assert row[source_field] is None
    assert row.get(case["alias"]) is None
    assert row.get(case["derived"]) is None


@pytest.mark.parametrize("kind", ("balance", "income", "cash_flow"))
def test_ttm_rows_preserve_zero_in_aliases_and_derived_fields(kind: str) -> None:
    case = _TTM_ALIAS_CASES[kind]
    values = dict.fromkeys(case["values"], (0.0, 0.0, 0.0, 0.0))

    row = _ttm_alias_result(kind, values)

    for field_name in case["canonical"]:
        assert row[field_name] == pytest.approx(0.0)
    assert row[case["alias"]] == pytest.approx(0.0)
    assert row[case["derived"]] == pytest.approx(0.0)


def test_companyfacts_statement_rows_group_tags_and_do_not_mix_currency_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def companyfacts(cash_units: dict[str, list[dict[str, object]]]) -> dict[str, object]:
        common = {
            "end": "2025-09-27",
            "filed": "2025-11-01",
            "form": "10-K",
            "accn": "0000320193-25-000001",
            "fy": 2025,
            "fp": "FY",
        }
        return {
            "cik": 320193,
            "facts": {
                "us-gaap": {
                    "Assets": {
                        "label": "Assets",
                        "units": {"USD": [{**common, "val": 100}]},
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "label": "Cash and cash equivalents",
                        "units": cash_units,
                    },
                }
            },
        }

    _patch_json(
        monkeypatch,
        financial,
        companyfacts(
            {
                "USD": [
                    {
                        "end": "2025-09-27",
                        "filed": "2025-11-01",
                        "form": "10-K",
                        "accn": "0000320193-25-000001",
                        "fy": 2025,
                        "fp": "FY",
                        "val": 20,
                    }
                ]
            }
        ),
    )
    grouped = financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193")
    assert len(grouped) == 1
    assert grouped[0].total_assets == 100.0
    assert grouped[0].cash_and_equivalents == 20.0
    assert grouped[0].reported_currency == "USD"
    assert len(grouped[0].sec_facts) == 2

    _patch_json(
        monkeypatch,
        financial,
        companyfacts(
            {
                "USD": [
                    {
                        "end": "2025-09-27",
                        "filed": "2025-11-01",
                        "form": "10-K",
                        "accn": "0000320193-25-000001",
                        "fy": 2025,
                        "fp": "FY",
                        "val": 20,
                    }
                ],
                "EUR": [
                    {
                        "end": "2025-09-27",
                        "filed": "2025-11-01",
                        "form": "10-K",
                        "accn": "0000320193-25-000001",
                        "fy": 2025,
                        "fp": "FY",
                        "val": 30,
                    }
                ],
            }
        ),
    )
    mixed = financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193")
    assert len(mixed) == 1
    assert mixed[0].reported_currency is None
    assert mixed[0].total_assets == 100.0
    assert mixed[0].cash_and_equivalents is None
    assert len(mixed[0].sec_facts) == 3


def test_companyfacts_statement_vintages_distinguish_latest_pit_and_preliminary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = [
        {
            "end": "2025-09-27",
            "filed": "2025-10-20",
            "form": "8-K",
            "accn": "0000320193-25-000003",
            "fy": 2025,
            "fp": "FY",
            "val": 90,
        },
        {
            "end": "2025-09-27",
            "filed": "2025-11-01",
            "form": "10-K",
            "accn": "0000320193-25-000001",
            "fy": 2025,
            "fp": "FY",
            "val": 100,
        },
        {
            "end": "2025-09-27",
            "filed": "2026-01-15",
            "form": "10-K/A",
            "accn": "0000320193-26-000001",
            "fy": 2025,
            "fp": "FY",
            "val": 120,
        },
    ]
    document = {
        "cik": 320193,
        "facts": {"us-gaap": {"Assets": {"label": "Assets", "units": {"USD": facts}}}},
    }
    _patch_json(monkeypatch, financial, document)

    latest = financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193")
    point_in_time = financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193", pit_mode=True)
    include_preliminary = financial.BalanceSheetFetcher().fetch(
        ctx=_ctx(), cik="320193", include_preliminary=True
    )
    assert latest[0].total_assets == 120.0
    assert point_in_time[0].total_assets == 100.0
    assert include_preliminary[0].total_assets == 120.0

    preliminary_only = {
        "cik": 320193,
        "facts": {"us-gaap": {"Assets": {"label": "Assets", "units": {"USD": [facts[0]]}}}},
    }
    _patch_json(monkeypatch, financial, preliminary_only)
    assert financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193") == ()
    preliminary_result = financial.BalanceSheetFetcher().fetch(
        ctx=_ctx(), cik="320193", include_preliminary=True
    )
    assert preliminary_result[0].total_assets == 90.0


def test_companyfacts_uses_cik_prefixed_official_route_via_governed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _assets_companyfacts_document()
    session = _install_sec_fixture_client(
        monkeypatch, json.dumps(document, allow_nan=False).encode("utf-8")
    )

    rows = financial.BalanceSheetFetcher().fetch(
        ctx=_ctx(), cik="320193", market="us", source="sec"
    )

    assert len(rows) == 1
    assert rows[0].total_assets == 359241000000
    assert rows[0].period_ending == date(2025, 9, 27)
    assert session.calls == [("GET", _COMPANYFACTS_URL)]


@pytest.mark.parametrize(
    ("content", "error_code"),
    (
        (b"", "SEC_EMPTY_RESPONSE"),
        (b'{"cik":320193}', "SEC_XBRL_BAD_SHAPE"),
        (
            json.dumps(_assets_companyfacts_document(cik=789)).encode("utf-8"),
            "SEC_XBRL_CIK_MISMATCH",
        ),
    ),
)
def test_companyfacts_governed_client_rejects_empty_missing_and_counterfact_documents(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    error_code: str,
) -> None:
    session = _install_sec_fixture_client(monkeypatch, content)

    with pytest.raises(common.SecProviderError, match=error_code):
        financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193", market="us", source="sec")

    assert session.calls == [("GET", _COMPANYFACTS_URL)]


def test_companyfacts_zero_budget_stops_before_governed_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_sec_fixture_client(monkeypatch, b"{}")
    context = FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=0,
            source_attempts=0,
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        ),
    )
    fetcher = financial.BalanceSheetFetcher()
    params = fetcher.transform_query(cik="320193")

    with pytest.raises(RequestAttemptLimitError, match="SEC_REQUEST_BUDGET_EXCEEDED") as exhausted:
        fetcher.extract_data(params, context)
    assert isinstance(exhausted.value, common.SecProviderError)

    assert session.calls == []


def test_companyfacts_separates_filed_date_from_period_and_preflights_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_json(monkeypatch, financial, _json("cash-flow-statement", "positive.json"))
    result = financial.CashFlowStatementFetcher().fetch(
        ctx=_ctx(),
        cik="320193",
        start_date="2024-01-01",
        end_date="2024-12-31",
        filed_start_date="2025-02-01",
        filed_end_date="2025-02-01",
    )
    assert result[0].period_ending == date(2024, 12, 31)
    assert result[0].filing_date == date(2025, 2, 1)

    def should_not_fetch(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid query performed source I/O")

    monkeypatch.setattr(financial, "fetch_json", should_not_fetch)
    with pytest.raises(ValueError):
        financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193", symbol="AAPL", market="us")
    with pytest.raises(ValueError):
        financial.BalanceSheetFetcher().fetch(ctx=_ctx(), cik="320193", market="ca")


def test_compare_companyfacts_public_multi_symbol_default_fact_and_bad_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker_document = _json("compare-company-facts", "tickers-positive.json")
    aapl = {
        "cik": 320193,
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "label": "Revenue from Contract with Customer",
                    "units": {
                        "USD": [
                            {
                                "start": "2024-09-29",
                                "end": "2025-09-27",
                                "filed": "2025-10-31",
                                "form": "10-K",
                                "accn": "0000320193-25-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "val": 100,
                            }
                        ]
                    },
                }
            }
        },
    }
    msft = {
        "cik": 789019,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "units": {
                        "USD": [
                            {
                                "start": "2024-07-01",
                                "end": "2025-06-30",
                                "filed": "2025-07-30",
                                "form": "10-K",
                                "accn": "0000789019-25-000001",
                                "fy": 2025,
                                "fp": "FY",
                                "val": 90,
                            }
                        ]
                    },
                }
            }
        },
    }
    routes = {
        common.SEC_COMPANY_TICKERS_URL: (200, json.dumps(ticker_document).encode()),
        f"{financial.SEC_COMPANYFACTS_URL}/CIK0000320193.json": (200, json.dumps(aapl).encode()),
        f"{financial.SEC_COMPANYFACTS_URL}/CIK0000789019.json": (200, json.dumps(msft).encode()),
    }
    session = _install_sec_fixture_routes(monkeypatch, routes)
    result = financial.CompareCompanyFactsFetcher().fetch(
        ctx=_ctx(),
        symbol=["AAPL", "MSFT"],
        year=2025,
        market="us",
        source="sec",
    )
    assert {row.symbol for row in result} == {"AAPL", "MSFT"}
    assert all(type(row) is SecCompanyFactComparison for row in result)
    assert {row.value for row in result} == {90.0, 100.0}
    assert all(row.period_ending is not None and row.unit == "USD" for row in result)
    assert [url for _method, url in session.calls] == [
        common.SEC_COMPANY_TICKERS_URL,
        f"{financial.SEC_COMPANYFACTS_URL}/CIK0000320193.json",
        f"{financial.SEC_COMPANYFACTS_URL}/CIK0000789019.json",
    ]

    malformed = {
        "cik": 320193,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "label": "Revenues",
                    "units": {
                        "USD": [
                            {
                                "end": "2025-99-27",
                                "filed": "2025-10-31",
                                "form": "10-K",
                                "accn": "0000320193-25-000001",
                                "val": 100,
                            }
                        ]
                    },
                }
            }
        },
    }
    monkeypatch.setattr(
        financial,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(ticker_document),
    )
    _patch_json(monkeypatch, financial, malformed)
    with pytest.raises(common.SecProviderError, match="SEC_XBRL_BAD_FACT"):
        financial.CompareCompanyFactsFetcher().fetch(ctx=_ctx(), symbol="AAPL")


def test_equity_ftd_archive_fixture_paging_and_invalid_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        _fixture("equity-ftd", "positive-index.html"),
        _fixture("equity-ftd", "positive.zip"),
    ]
    monkeypatch.setattr(ftd, "fetch_bytes", lambda *_args, **_kwargs: responses.pop(0))
    result = ftd.EquityFtdFetcher().fetch(
        ctx=_ctx(),
        symbol="AAPL",
        limit=1,
        market="us",
        source="sec",
    )
    # Public limit counts half-month reports, not the rows returned from one report.
    assert len(result) == 2
    assert type(result[0]) is SecFtdRecord
    assert result[0].quantity == 250
    assert result[0].quantity_fails == 250
    assert result[0].symbol == "AAPL"
    assert result[0].price == 185.5
    assert type(result[0].price) is float
    assert result[0].prior_close_price == Decimal("185.50")
    assert result[0].description == "APPLE INC"
    serialized = json.loads(json.dumps(result[0].model_dump(mode="json")))
    assert serialized["price"] == 185.5
    assert type(serialized["price"]) is float
    assert type(serialized["quantity"]) is int
    assert serialized["prior_close_price"] == "185.50"
    assert serialized["settlement_date"] == "2026-01-02"
    assert result[1].settlement_date == date(2026, 1, 1)

    skipped_responses = [_fixture("equity-ftd", "positive-index.html")]
    monkeypatch.setattr(ftd, "fetch_bytes", lambda *_args, **_kwargs: skipped_responses.pop(0))
    skipped = ftd.EquityFtdFetcher().fetch(ctx=_ctx(), symbol="AAPL", limit=1, skip_reports=1)
    assert skipped == ()
    assert skipped_responses == []

    negative = [
        _fixture("equity-ftd", "positive-index.html"),
        _fixture("equity-ftd", "negative.zip"),
    ]
    monkeypatch.setattr(ftd, "fetch_bytes", lambda *_args, **_kwargs: negative.pop(0))
    with pytest.raises(common.SecProviderError, match="SEC_FTD_BAD_QUANTITY"):
        ftd.EquityFtdFetcher().fetch(ctx=_ctx(), symbol="AAPL")

    calls: list[str] = []
    monkeypatch.setattr(ftd, "fetch_bytes", lambda *_args, **_kwargs: calls.append("request"))
    with pytest.raises(ValueError):
        ftd.EquityFtdFetcher().fetch(ctx=_ctx(), symbol="AAPL", offset=1)
    assert calls == []


def _ftd_archive_with_price(price: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "cnsfails202601a.txt",
            "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
            f"20260102|037833100|AAPL|250|APPLE INC|{price}\n",
        )
    return buffer.getvalue()


@pytest.mark.parametrize("price", ["not-a-price", "NaN", "Infinity", "9" * 400, f"0.{'0' * 400}1"])
def test_ftd_rejects_malformed_overflow_and_underflow_price(price: str) -> None:
    with pytest.raises(common.SecProviderError, match="SEC_FTD_BAD_PRICE"):
        ftd.parse_ftd_zip(_ftd_archive_with_price(price), symbol="AAPL")


def test_ftd_public_price_json_preserves_null() -> None:
    row = ftd.parse_ftd_zip(_ftd_archive_with_price(""), symbol="AAPL")[0]
    serialized = json.loads(json.dumps(row.model_dump(mode="json")))
    assert serialized["price"] is None
    assert serialized["prior_close_price"] is None


def _insider_fetcher_inputs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    feed_name: str = "feed-positive.xml",
    index_name: str = "index-positive.json",
    xml_name: str = "form4-positive.xml",
) -> None:
    monkeypatch.setattr(
        insider,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(
            _json("insider-trading", "tickers-positive.json")
        ),
    )
    json_responses = [_json("insider-trading", index_name)]
    byte_responses = [
        _fixture("insider-trading", feed_name),
        _fixture("insider-trading", xml_name),
    ]
    monkeypatch.setattr(
        insider,
        "fetch_json",
        lambda *_args, **_kwargs: json_responses.pop(0),
    )
    monkeypatch.setattr(
        insider,
        "fetch_bytes",
        lambda *_args, **_kwargs: byte_responses.pop(0),
    )


def test_insider_trading_multi_stage_fixture_and_negative_index_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insider_fetcher_inputs(monkeypatch)
    rows = insider.InsiderTradingFetcher().fetch(
        ctx=_ctx(),
        symbol="AAPL",
        start_date="2026-05-01",
        end_date="2026-05-01",
        market="us",
        source="sec",
    )
    assert len(rows) == 1
    assert type(rows[0]) is SecInsiderTransaction
    assert rows[0].filing_date == date(2026, 5, 1)
    assert rows[0].report_date == date(2026, 4, 30)
    assert rows[0].transaction_date == date(2026, 4, 30)
    assert rows[0].transaction_index == 1
    assert rows[0].company_cik == rows[0].issuer_cik
    assert rows[0].company_name == rows[0].issuer_name
    assert rows[0].ownership_type == "D"
    assert rows[0].acquisition_or_disposition == "D"
    assert rows[0].director is False
    assert rows[0].officer is True
    assert rows[0].ten_percent_owner is False
    serialized = json.loads(json.dumps(rows[0].model_dump(mode="json")))
    assert serialized["securities_owned"] == 100.0
    assert serialized["securities_transacted"] == 10.0
    assert serialized["transaction_price"] == 200.0
    assert all(
        type(serialized[field]) is float
        for field in ("securities_owned", "securities_transacted", "transaction_price")
    )
    assert serialized["shares"] == "10"
    assert serialized["price_per_share"] == "200"

    _insider_fetcher_inputs(monkeypatch)
    indirect_rows = insider.InsiderTradingFetcher().fetch(
        ctx=_ctx(),
        symbol="AAPL",
        start_date="2026-05-01",
        end_date="2026-05-01",
        ownership_type="I",
    )
    assert indirect_rows == ()
    with pytest.raises(ValueError, match="no verified update timestamp"):
        insider.InsiderTradingFetcher().fetch(ctx=_ctx(), symbol="AAPL", sort_by="updated_on")

    reference = insider.SecForm4Reference(
        issuer_cik="0000320193",
        filing_cik="0000320193",
        accession_number="0000320193-26-000001",
        filing_date=date(2026, 5, 1),
        form="4",
    )
    xml_body = _fixture("insider-trading", "form4-positive.xml")
    ownership_counterfactual = xml_body.replace(
        b"<directOrIndirectOwnership><value>D</value></directOrIndirectOwnership>",
        b"<directOrIndirectOwnership><value>I</value></directOrIndirectOwnership>",
    ).replace(
        b"</ownershipNature>",
        b"<natureOfOwnership><value>Through a trust</value></natureOfOwnership></ownershipNature>",
    )
    indirect = insider.parse_form4_xml(ownership_counterfactual, reference)[0]
    assert indirect.ownership_type == "I"
    assert indirect.acquisition_or_disposition == "D"
    assert indirect.nature_of_ownership == "Through a trust"

    missing_optional_amounts = (
        xml_body.replace(
            b"<transactionPricePerShare><value>200</value></transactionPricePerShare>", b""
        )
        .replace(
            b"<transactionShares><value>10</value></transactionShares>",
            b"<transactionTotalValue><value>2000</value></transactionTotalValue>",
        )
        .replace(
            b"<postTransactionAmounts><sharesOwnedFollowingTransaction><value>100</value>"
            b"</sharesOwnedFollowingTransaction></postTransactionAmounts>",
            b"",
        )
    )
    null_amount_row = insider.parse_form4_xml(missing_optional_amounts, reference)[0]
    null_amount_json = json.loads(json.dumps(null_amount_row.model_dump(mode="json")))
    assert null_amount_json["transaction_price"] is None
    assert null_amount_json["securities_owned"] is None
    assert null_amount_json["securities_transacted"] is None
    assert null_amount_json["transaction_total_value"] == "2000"

    for bad_value in ("not-a-number", "NaN", "Infinity", "9" * 400, f"0.{'0' * 400}1"):
        bad_price_xml = xml_body.replace(
            b"<value>200</value></transactionPricePerShare>",
            f"<value>{bad_value}</value></transactionPricePerShare>".encode(),
        )
        with pytest.raises(common.SecProviderError, match="SEC_INSIDER_XML_BAD_NUMERIC_FIELD"):
            insider.parse_form4_xml(bad_price_xml, reference)

    too_large = "9" * 400
    public_numeric_fields = (
        (
            b"<transactionPricePerShare><value>200</value></transactionPricePerShare>",
            f"<transactionPricePerShare><value>{too_large}</value>"
            "</transactionPricePerShare>".encode(),
        ),
        (
            b"<transactionShares><value>10</value></transactionShares>",
            f"<transactionShares><value>{too_large}</value></transactionShares>".encode(),
        ),
        (
            b"<sharesOwnedFollowingTransaction><value>100</value></sharesOwnedFollowingTransaction>",
            f"<sharesOwnedFollowingTransaction><value>{too_large}</value>"
            "</sharesOwnedFollowingTransaction>".encode(),
        ),
    )
    for original, replacement in public_numeric_fields:
        bad_xml = xml_body.replace(original, replacement)
        with pytest.raises(common.SecProviderError, match="SEC_INSIDER_XML_BAD_NUMERIC_FIELD"):
            insider.parse_form4_xml(bad_xml, reference)

    xml_text = xml_body.decode("utf-8").replace(
        "<transactionDate>", "<reportLineNumber>7</reportLineNumber><transactionDate>", 1
    )
    transaction_xml = xml_text.split("<nonDerivativeTable>", 1)[1].split(
        "</nonDerivativeTable>", 1
    )[0]
    two_transactions = xml_text.replace(
        "</nonDerivativeTable>", transaction_xml + "</nonDerivativeTable>", 1
    ).encode("utf-8")
    repeated = insider.parse_form4_xml(two_transactions, reference)
    assert [row.transaction_index for row in repeated] == [1, 2]
    assert [row.report_line_number for row in repeated] == [7, 7]
    assert (
        len(
            {
                (row.company_cik, row.accession_number, row.security_type, row.transaction_index)
                for row in repeated
            }
        )
        == 2
    )

    _insider_fetcher_inputs(monkeypatch, index_name="index-negative.json")
    with pytest.raises(common.SecProviderError, match="SEC_INSIDER_PRIMARY_XML_AMBIGUOUS"):
        insider.InsiderTradingFetcher().fetch(
            ctx=_ctx(), symbol="AAPL", start_date="2026-05-01", end_date="2026-05-01"
        )

    _insider_fetcher_inputs(monkeypatch, xml_name="form4-negative.xml")
    with pytest.raises(common.SecProviderError, match="SEC_INSIDER_ISSUER_MISMATCH"):
        insider.InsiderTradingFetcher().fetch(
            ctx=_ctx(), symbol="AAPL", start_date="2026-05-01", end_date="2026-05-01"
        )


def test_provider_model_query_injects_sec_identity_and_serializes_typed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = directory.CikMapFetcher()
    capability = fetcher.capability
    received_query: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def record_query(**values: object) -> object:
        received_query.update(values)
        return original_transform_query(**values)

    monkeypatch.setattr(fetcher, "transform_query", record_query)
    monkeypatch.setattr(
        directory,
        "company_ticker_rows",
        lambda _ctx: common.parse_ticker_directory(_json("cik-map", "positive.json")),
    )
    descriptor = SimpleNamespace(
        source="sec",
        model="CikMap",
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
        provider_model_query, "_resolve_fetcher", lambda *_args: (descriptor, fetcher)
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, SecCikMapRecord),
    )

    class Principal:
        def allows_domain(self, domain_name: str) -> bool:
            return domain_name == capability.domain

    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="sec",
            model="CikMap",
            query={"symbol": "AAPL", "limit": 1},
            principal=Principal(),
            ctx=_ctx(),
        )
    )
    assert response["source"] == "sec"
    assert response["model"] == "CikMap"
    assert received_query["source"] == "sec"
    assert received_query["market"] == "us"
    assert received_query["symbol"] == "AAPL"
    assert response["results"] == [{"ticker": "AAPL", "cik": "0000320193", "name": "Apple Inc."}]


def test_ftd_and_insider_provider_queries_emit_public_numeric_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current: dict[str, object] = {}

    def bind(fetcher: Any, model: str, contract: type[Any]) -> None:
        capability = fetcher.capability
        current["fetcher"] = fetcher
        current["contract"] = contract
        current["descriptor"] = SimpleNamespace(
            source="sec",
            model=model,
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
        lambda *_args: (current["descriptor"], current["fetcher"]),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, current["contract"]),
    )

    class Principal:
        def allows_domain(self, domain_name: str) -> bool:
            descriptor = current["descriptor"]
            return domain_name == descriptor.domain

    bind(ftd.EquityFtdFetcher(), "EquityFTD", SecFtdRecord)
    ftd_responses = [
        _fixture("equity-ftd", "positive-index.html"),
        _fixture("equity-ftd", "positive.zip"),
    ]
    monkeypatch.setattr(
        ftd,
        "fetch_bytes",
        lambda *_args, **_kwargs: ftd_responses.pop(0),
    )
    ftd_response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="sec",
            model="EquityFTD",
            query={"symbol": "AAPL", "limit": 1},
            principal=Principal(),
            ctx=_ctx(),
        )
    )
    ftd_json = json.loads(json.dumps(ftd_response))
    assert type(ftd_json["results"][0]["price"]) is float
    assert ftd_json["results"][0]["price"] == 185.5

    bind(insider.InsiderTradingFetcher(), "InsiderTrading", SecInsiderTransaction)
    _insider_fetcher_inputs(monkeypatch)
    insider_response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=object(),
            source="sec",
            model="InsiderTrading",
            query={"symbol": "AAPL", "start_date": "2026-05-01", "end_date": "2026-05-01"},
            principal=Principal(),
            ctx=_ctx(),
        )
    )
    insider_json = json.loads(json.dumps(insider_response))
    insider_row = insider_json["results"][0]
    assert [
        type(insider_row[field])
        for field in ("securities_owned", "securities_transacted", "transaction_price")
    ] == [float, float, float]
    assert [
        insider_row[field]
        for field in ("securities_owned", "securities_transacted", "transaction_price")
    ] == [100.0, 10.0, 200.0]
    assert insider_row["shares"] == "10"


def test_sec_user_agent_and_multistage_budget_fail_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoNetwork:
        calls = 0

        def get(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("the fake transport must not be called")

    transport = NoNetwork()
    monkeypatch.setattr(common, "get_shared_http_client", lambda: transport)
    monkeypatch.delenv(common.SEC_USER_AGENT_ENV, raising=False)
    with pytest.raises(common.SecProviderError, match="SEC_USER_AGENT_MISSING_OR_INVALID"):
        common.fetch_bytes(_ctx(), "https://www.sec.gov/files/company_tickers.json")
    assert transport.calls == 0

    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    context = FetchContext(timeout=5.0, request_budget=budget)
    query = insider.InsiderTradingFetcher().transform_query(
        symbol="AAPL", start_date="2026-05-01", end_date="2026-05-01", market="us", source="sec"
    )
    monkeypatch.setattr(
        insider,
        "company_ticker_rows",
        lambda *_args: (_ for _ in ()).throw(AssertionError("budget preflight must precede I/O")),
    )
    with pytest.raises(RequestAttemptLimitError, match="SEC_REQUEST_BUDGET_EXCEEDED") as exhausted:
        insider.InsiderTradingFetcher().extract_data(query, context)
    assert isinstance(exhausted.value, common.SecProviderError)

    limited_grant = RequestGrant(
        source="sec",
        canonical_model="InsiderTrading",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:limited exact-model grant",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts={"www.sec.gov", "data.sec.gov"},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    limited_budget = RequestBudget(
        task_attempts=10,
        source_attempts=10,
        grants=(limited_grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    with pytest.raises(RequestAttemptLimitError, match="SEC_REQUEST_BUDGET_EXCEEDED") as exhausted:
        insider.InsiderTradingFetcher().fetch(
            ctx=FetchContext(timeout=5.0, request_budget=limited_budget),
            symbol="AAPL",
            start_date="2026-05-01",
            end_date="2026-05-01",
        )
    assert isinstance(exhausted.value, common.SecProviderError)
