"""Offline official-shape tests for FMP estimates, ESG, N-PORT, and YieldCurve."""

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
    AnalystEstimate,
    FmpEsgScore,
    FmpYieldCurveObservation,
    ForwardEbitdaEstimate,
    ForwardEpsEstimate,
    NportDisclosureHolding,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.fmp import provider as fmp_provider
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models import iter2_analyst_macro as analyst_macro
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query

_FIXTURE_ROOT = Path(__file__).parents[1] / "docs/evidence/C100/fmp/fixtures"


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


def _fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((_FIXTURE_ROOT / name).read_text())


def _official_treasury_rates() -> dict[str, object]:
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


@pytest.mark.parametrize(
    ("fetcher_type", "model", "contract", "route", "query"),
    [
        (
            analyst_macro.AnalystEstimatesFetcher,
            "AnalystEstimates",
            AnalystEstimate,
            "analyst-estimates",
            {"symbol": "aapl", "period": "annual"},
        ),
        (
            analyst_macro.ForwardEbitdaEstimatesFetcher,
            "ForwardEbitdaEstimates",
            ForwardEbitdaEstimate,
            "analyst-estimates",
            {"symbol": "aapl", "period": "annual"},
        ),
        (
            analyst_macro.ForwardEpsEstimatesFetcher,
            "ForwardEpsEstimates",
            ForwardEpsEstimate,
            "analyst-estimates",
            {"symbol": "aapl", "period": "annual"},
        ),
        (
            analyst_macro.EsgScoreFetcher,
            "EsgScore",
            FmpEsgScore,
            "esg-disclosures",
            {"symbol": "aapl"},
        ),
        (
            analyst_macro.NportDisclosureFetcher,
            "NportDisclosure",
            NportDisclosureHolding,
            "funds/disclosure",
            {"symbol": "vwo", "year": 2023, "quarter": 4},
        ),
        (
            analyst_macro.YieldCurveFetcher,
            "YieldCurve",
            FmpYieldCurveObservation,
            "treasury-rates",
            {"start_date": "2026-07-01", "end_date": "2026-07-30"},
        ),
    ],
    ids=["analyst-estimates", "forward-ebitda", "forward-eps", "esg-score", "nport", "yield-curve"],
)
def test_official_shape_and_route(
    http_client: _HTTPClient,
    fetcher_type: type,
    model: str,
    contract: type,
    route: str,
    query: dict[str, object],
) -> None:
    if model in {"AnalystEstimates", "ForwardEbitdaEstimates", "ForwardEpsEstimates"}:
        rows = _fixture("analyst-estimate.json")
        expected_params = {
            "symbol": "AAPL",
            "period": "annual",
            "page": "0",
            "limit": "10",
            "apikey": "test-secret-key",
        }
    elif model == "EsgScore":
        rows = _fixture("esg-score.json")
        expected_params = {"symbol": "AAPL", "apikey": "test-secret-key"}
    elif model == "NportDisclosure":
        rows = _fixture("nport-disclosure.json")
        expected_params = {
            "symbol": "VWO",
            "year": "2023",
            "quarter": "4",
            "apikey": "test-secret-key",
        }
    else:
        rows = [_official_treasury_rates()]
        expected_params = {
            "from": "2026-07-01",
            "to": "2026-07-30",
            "apikey": "test-secret-key",
        }
    http_client.responses = [_ok(rows)]

    result = fetcher_type().fetch(ctx=_context(model), **query)

    assert result
    assert isinstance(result[0], contract)
    assert http_client.calls == [
        (
            f"https://financialmodelingprep.com/stable/{route}",
            expected_params,
            "fmp",
        )
    ]
    if model == "ForwardEbitdaEstimates":
        assert result[0].model_dump(mode="json") == {
            "symbol": "AAPL",
            "date": "2030-09-27",
            "ebitda_low": 233968328102,
            "ebitda_high": 265295486763,
            "ebitda_average": 245074834838,
        }
    elif model == "ForwardEpsEstimates":
        assert result[0].eps_average == 13.565
        assert result[0].num_analysts_eps == 7
    elif model == "NportDisclosure":
        assert result[0].fund_symbol == "VWO"
        assert result[0].symbol == "000089.SZ"
    elif model == "YieldCurve":
        assert len(result) == 12
        assert (result[0].maturity, result[0].rate) == ("1M", 3.73)
        assert (result[-1].maturity, result[-1].rate) == ("30Y", 5.2)


@pytest.mark.parametrize(
    ("fetcher_type", "kwargs"),
    [
        (analyst_macro.AnalystEstimatesFetcher, {"symbol": "AAPL", "period": "monthly"}),
        (
            analyst_macro.AnalystEstimatesFetcher,
            {"symbol": "AAPL", "period": "annual", "page": True},
        ),
        (
            analyst_macro.ForwardEpsEstimatesFetcher,
            {"symbol": "AAPL", "period": "annual", "start_date": "2026-01-01"},
        ),
        (analyst_macro.EsgScoreFetcher, {"symbol": "AAPL", "end_date": "2026-01-01"}),
        (
            analyst_macro.NportDisclosureFetcher,
            {"symbol": "VWO", "year": 2023, "quarter": 5},
        ),
        (
            analyst_macro.NportDisclosureFetcher,
            {"symbol": "VWO", "year": 2023, "quarter": 4, "cik": "not-a-cik"},
        ),
        (
            analyst_macro.YieldCurveFetcher,
            {"start_date": "2026-07-30", "end_date": "2026-07-01"},
        ),
    ],
    ids=[
        "bad-estimate-period",
        "bad-page-type",
        "estimate-date-filter",
        "esg-date-filter",
        "nport-quarter",
        "nport-cik",
        "yield-reversed-window",
    ],
)
def test_invalid_queries_fail_before_http(
    http_client: _HTTPClient,
    fetcher_type: type,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(fmp_client.FMPProviderError) as error:
        fetcher_type().fetch(**kwargs)
    assert error.value.code == "FMP_BAD_QUERY"
    assert http_client.calls == []


def test_analyst_page_and_limit_are_passed_as_documented(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [_ok([])]
    analyst_macro.AnalystEstimatesFetcher().fetch(
        ctx=_context("AnalystEstimates"),
        symbol="AAPL",
        period="quarter",
        page=2,
        limit=7,
    )
    assert http_client.calls[0][0] == ("https://financialmodelingprep.com/stable/analyst-estimates")
    assert http_client.calls[0][1] == {
        "symbol": "AAPL",
        "period": "quarter",
        "page": "2",
        "limit": "7",
        "apikey": "test-secret-key",
    }


def test_estimate_response_over_requested_page_limit_is_rejected(
    http_client: _HTTPClient,
) -> None:
    row = _fixture("analyst-estimate.json")[0]
    http_client.responses = [_ok([row, {**row, "date": "2031-09-27"}])]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        analyst_macro.AnalystEstimatesFetcher().fetch(
            ctx=_context("AnalystEstimates"),
            symbol="AAPL",
            period="annual",
            limit=1,
        )
    assert error.value.code == "FMP_PROVIDER_LIMIT_VIOLATION"


@pytest.mark.parametrize(
    ("fetcher_type", "model", "kwargs", "rows", "error_code"),
    [
        (
            analyst_macro.AnalystEstimatesFetcher,
            "AnalystEstimates",
            {"symbol": "AAPL", "period": "annual"},
            [{**_fixture("analyst-estimate.json")[0], "epsAvg": float("inf")}],
            "FMP_BAD_SHAPE",
        ),
        (
            analyst_macro.ForwardEbitdaEstimatesFetcher,
            "ForwardEbitdaEstimates",
            {"symbol": "AAPL", "period": "annual"},
            [{**_fixture("analyst-estimate.json")[0], "symbol": "MSFT"}],
            "FMP_SYMBOL_MISMATCH",
        ),
        (
            analyst_macro.EsgScoreFetcher,
            "EsgScore",
            {"symbol": "AAPL"},
            [{**_fixture("esg-score.json")[0], "extraField": 1}],
            "FMP_BAD_SHAPE",
        ),
        (
            analyst_macro.NportDisclosureFetcher,
            "NportDisclosure",
            {"symbol": "VWO", "year": 2023, "quarter": 4},
            [{**_fixture("nport-disclosure.json")[0], "balance": "2438784"}],
            "FMP_BAD_SHAPE",
        ),
        (
            analyst_macro.YieldCurveFetcher,
            "YieldCurve",
            {"start_date": "2026-07-01", "end_date": "2026-07-30"},
            [{**_official_treasury_rates(), "date": "2026-08-01"}],
            "FMP_OUT_OF_WINDOW_ROW",
        ),
    ],
    ids=[
        "analyst-nonfinite",
        "forward-wrong-symbol",
        "esg-extra",
        "nport-wrong-number",
        "yield-out-of-window",
    ],
)
def test_wrong_source_rows_fail_closed(
    http_client: _HTTPClient,
    fetcher_type: type,
    model: str,
    kwargs: dict[str, object],
    rows: list[dict[str, object]],
    error_code: str,
) -> None:
    http_client.responses = [_ok(rows)]
    with pytest.raises(fmp_client.FMPProviderError) as error:
        fetcher_type().fetch(ctx=_context(model), **kwargs)
    assert error.value.code == error_code
    assert "test-secret-key" not in str(error.value)


def test_yield_curve_output_budget_counts_expanded_maturity_rows(
    http_client: _HTTPClient,
) -> None:
    http_client.responses = [_ok([_official_treasury_rates()])]
    with pytest.raises(fmp_client.FMPQueryLimitError) as error:
        analyst_macro.YieldCurveFetcher().fetch(
            ctx=_context("YieldCurve"),
            start_date="2026-07-01",
            end_date="2026-07-30",
            max_records=11,
        )
    assert error.value.code == "FMP_INCOMPLETE_RECORD_BUDGET"


@pytest.mark.parametrize(
    ("fetcher_type", "model", "contract", "query", "rows"),
    [
        (
            analyst_macro.AnalystEstimatesFetcher,
            "AnalystEstimates",
            AnalystEstimate,
            {"symbol": "AAPL", "period": "annual"},
            _fixture("analyst-estimate.json"),
        ),
        (
            analyst_macro.ForwardEbitdaEstimatesFetcher,
            "ForwardEbitdaEstimates",
            ForwardEbitdaEstimate,
            {"symbol": "AAPL", "period": "annual"},
            _fixture("analyst-estimate.json"),
        ),
        (
            analyst_macro.ForwardEpsEstimatesFetcher,
            "ForwardEpsEstimates",
            ForwardEpsEstimate,
            {"symbol": "AAPL", "period": "annual"},
            _fixture("analyst-estimate.json"),
        ),
        (
            analyst_macro.EsgScoreFetcher,
            "EsgScore",
            FmpEsgScore,
            {"symbol": "AAPL"},
            _fixture("esg-score.json"),
        ),
        (
            analyst_macro.NportDisclosureFetcher,
            "NportDisclosure",
            NportDisclosureHolding,
            {"symbol": "VWO", "year": 2023, "quarter": 4},
            _fixture("nport-disclosure.json"),
        ),
        (
            analyst_macro.YieldCurveFetcher,
            "YieldCurve",
            FmpYieldCurveObservation,
            {"start_date": "2026-07-01", "end_date": "2026-07-30"},
            [_official_treasury_rates()],
        ),
    ],
    ids=["analyst", "forward-ebitda", "forward-eps", "esg", "nport", "yield-curve"],
)
def test_provider_model_query_injects_identity_and_serializes_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
    http_client: _HTTPClient,
    fetcher_type: type,
    model: str,
    contract: type,
    query: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    if model == "YieldCurve":
        response_rows: list[dict[str, object]] = rows
    elif model == "NportDisclosure":
        response_rows = rows
    else:
        response_rows = rows
    http_client.responses = [_ok(response_rows)]
    instance = fetcher_type()
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
    typed_result = instance.transform_data(response_rows, instance.transform_query(**query))
    expected = [record.model_dump(mode="json", by_alias=False) for record in typed_result]
    assert response["results"] == expected


def test_provider_binds_new_models_with_unique_domains() -> None:
    bindings = fmp_provider.PROVIDER.fetcher_dict
    fetchers = fmp_provider.PROVIDER.fetchers
    expected = {
        "AnalystEstimates": ("AnalystEstimatesFetcher", "fmp_analyst_estimates"),
        "ForwardEbitdaEstimates": (
            "ForwardEbitdaEstimatesFetcher",
            "fmp_forward_ebitda_estimates",
        ),
        "ForwardEpsEstimates": ("ForwardEpsEstimatesFetcher", "fmp_forward_eps_estimates"),
        "EsgScore": ("EsgScoreFetcher", "fmp_esg_score"),
        "NportDisclosure": ("NportDisclosureFetcher", "fmp_nport_disclosure"),
        "YieldCurve": ("YieldCurveFetcher", "fmp_yield_curve"),
    }
    for model, (fetcher_name, _domain) in expected.items():
        assert bindings[model].class_name == fetcher_name
    assert {
        fetcher.canonical_model: fetcher.capability.domain
        for fetcher in fetchers
        if fetcher.canonical_model in expected
    } == {model: domain for model, (_fetcher, domain) in expected.items()}
    all_domains = [fetcher.capability.domain for fetcher in fetchers]
    assert len(all_domains) == len(set(all_domains))


@pytest.mark.parametrize(
    ("model", "fixture", "contract"),
    [
        ("AnalystEstimates", "analyst-estimate.json", AnalystEstimate),
        ("EsgScore", "esg-score.json", FmpEsgScore),
        ("NportDisclosure", "nport-disclosure.json", NportDisclosureHolding),
    ],
)
def test_official_fixtures_validate_without_extra_fields(
    model: str,
    fixture: str,
    contract: type,
) -> None:
    del model
    row = _fixture(fixture)[0]
    if contract is NportDisclosureHolding:
        row = {**row, "fund_symbol": "VWO"}
    typed = contract.model_validate(row)
    assert contract.model_validate_json(typed.model_dump_json()) == typed
    with pytest.raises(ValidationError):
        contract.model_validate({**row, "unexpected": "not documented"})


@pytest.mark.parametrize(
    ("contract", "fixture", "missing_field"),
    [
        (AnalystEstimate, "analyst-estimate.json", "epsAvg"),
        (ForwardEbitdaEstimate, "analyst-estimate.json", "ebitdaAvg"),
        (ForwardEpsEstimate, "analyst-estimate.json", "epsAvg"),
        (FmpEsgScore, "esg-score.json", "url"),
        (NportDisclosureHolding, "nport-disclosure.json", "valUsd"),
    ],
    ids=["analyst", "forward-ebitda", "forward-eps", "esg", "nport"],
)
def test_contracts_reject_missing_documented_fields(
    contract: type,
    fixture: str,
    missing_field: str,
) -> None:
    row = _fixture(fixture)[0]
    if contract is NportDisclosureHolding:
        row = {**row, "fund_symbol": "VWO"}
    row.pop(missing_field)
    with pytest.raises(ValidationError):
        contract.model_validate(row)
