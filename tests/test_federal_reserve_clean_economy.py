"""Offline clean-room contracts for three Federal Reserve economy models."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from openpyxl import Workbook
from pydantic import ValidationError
from requests.adapters import BaseAdapter

from opendata.data import domains
from opendata.data.domains import DomainSpec
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.models.federal_reserve_clean_economy import (
    CentralBankHoldings,
    InflationExpectations,
    TotalFactorProductivity,
)
from opendata.data.protocol import FetchContext
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import (
    ProviderModelQueryAuthorizationError,
    ProviderModelQueryBudgetError,
    ProviderModelQueryCancelledError,
    ProviderModelQueryUpstreamError,
    ProviderModelQueryValidationError,
    query_provider_model,
)

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "opendata/data/providers/federal_reserve/models/clean_economy.py"
)
_SPEC = importlib.util.spec_from_file_location("federal_reserve_clean_economy_test", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
clean_economy = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = clean_economy
_SPEC.loader.exec_module(clean_economy)

_CONTRACTS: dict[str, type[Any]] = {
    "central_bank_holdings": CentralBankHoldings,
    "inflation_expectations": InflationExpectations,
    "total_factor_productivity": TotalFactorProductivity,
}
_IDENTITIES: dict[str, tuple[str, type[Any], str]] = {
    "central_bank_holdings": (
        "CentralBankHoldings",
        clean_economy.CentralBankHoldingsFetcher,
        "markets.newyorkfed.org",
    ),
    "inflation_expectations": (
        "InflationExpectations",
        clean_economy.InflationExpectationsFetcher,
        "www.philadelphiafed.org",
    ),
    "total_factor_productivity": (
        "TotalFactorProductivity",
        clean_economy.TotalFactorProductivityFetcher,
        "www.frbsf.org",
    ),
}


class FixtureAdapter(BaseAdapter):
    """Socket-free adapter that returns manually authored official-shape fixtures."""

    max_retries = requests.adapters.Retry(total=0)

    def __init__(self, replies: dict[str, tuple[int, bytes, str]]) -> None:
        self.replies = replies
        self.calls: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        self.calls.append(request)
        path = urlsplit(request.url).path
        status, content, content_type = self.replies.get(
            path,
            (404, b'{"error":"fixture route missing"}', "application/json"),
        )
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.encoding = "utf-8"
        response.headers["Content-Type"] = content_type
        response.url = request.url
        response.request = request
        response.connection = self
        return response

    def close(self) -> None:
        """The in-memory adapter owns no resources."""


class WamStrictFixtureAdapter(FixtureAdapter):
    """Serve WAM captures only for the exact official GET routes."""

    _origin = "https://markets.newyorkfed.org"

    def send(self, request: requests.PreparedRequest, **_kwargs: object) -> requests.Response:
        self.calls.append(request)
        request_url = request.url or ""
        path = urlsplit(request_url).path
        exact_url = f"{self._origin}{path}"
        if request.method == "GET" and request_url == exact_url and path in self.replies:
            status, content, content_type = self.replies[path]
        else:
            status, content, content_type = (
                404,
                b'{"error":"fixture route missing"}',
                "application/json",
            )
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.encoding = "utf-8"
        response.headers["Content-Type"] = content_type
        response.url = request_url
        response.request = request
        response.connection = self
        return response

    def close(self) -> None:
        """The in-memory adapter owns no resources."""


def _client(
    monkeypatch: pytest.MonkeyPatch,
    replies: dict[str, tuple[int, bytes, str]],
    *,
    adapter: FixtureAdapter | None = None,
) -> tuple[GovernedHttpClient, FixtureAdapter]:
    selected_adapter = adapter if adapter is not None else FixtureAdapter(replies)
    session = requests.Session()
    session.mount("https://", selected_adapter)
    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=1,
            rate_limit_per_host=None,
            backoff_base=0,
            backoff_jitter=0,
            breaker_threshold=3,
        ),
        session=session,
        sleep=lambda _seconds: None,
        jitter_fn=lambda: 0.0,
        raw_response_cache=None,
    )
    monkeypatch.setattr(clean_economy, "get_shared_http_client", lambda: client)
    return client, selected_adapter


def _json_reply(value: object, status: int = 200) -> tuple[int, bytes, str]:
    import json

    return status, json.dumps(value, allow_nan=False).encode("utf-8"), "application/json"


def _official_wam_replies() -> dict[str, tuple[int, bytes, str]]:
    root = Path(__file__).resolve().parents[1]
    captures = {
        "/api/soma/tsy/wam/all/asof/2026-10-07.json": (
            root / "docs/evidence/C100/acceptance-federal-reserve-clean-extra/sources/soma/"
            "20261012T141015.619661Z-91514-a5be40c3/treasury_wam_all_2026-10-07.json",
            "b714a03440c9e82bedc19d35833dfb95aa9edc6e162e70e8935733ab2eabcd0f",
        ),
        "/api/soma/agency/wam/agency%20debts/asof/2026-10-07.json": (
            root / "docs/evidence/C100/fed-remaining-date-wam-review-20261013/"
            "source-captures/20261013T125709Z/agency-wam.body",
            "a797a3bc667d2f4fa5d28602bbd283191c657c2f3593169618330f6142d29f2f",
        ),
    }
    replies: dict[str, tuple[int, bytes, str]] = {}
    for route, (path, expected_sha256) in captures.items():
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == expected_sha256
        replies[route] = (200, body, "application/json")
    return replies


def _xlsx_reply(workbook: Workbook) -> tuple[int, bytes, str]:
    from io import BytesIO

    output = BytesIO()
    workbook.save(output)
    return (
        200,
        output.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _add_sheet(workbook: Workbook, title: str, rows: list[tuple[object, ...]]) -> None:
    sheet = workbook.create_sheet(title)
    for row in rows:
        sheet.append(row)


def _philly_workbook(*, bad_value: bool = False) -> Workbook:
    workbook = Workbook()
    workbook.remove(workbook.active)
    _add_sheet(
        workbook,
        "INFLATION",
        [
            ("YEAR", "QUARTER", "INFPGDP1YR", "INFCPI1YR", "INFCPI10YR"),
            (1970, 1, "#N/A", "#N/A", "#N/A"),
            (1970, 2, "not-a-number" if bad_value else 2.9851, "#N/A", "#N/A"),
            (1981, 3, 3.25, 2.1, "#N/A"),
            (1991, 4, 2.4, 2.5, 3.1),
        ],
    )
    return workbook


def _tfp_workbook() -> Workbook:
    headers = (
        "date",
        "dY_prod",
        "dY_inc",
        "dY",
        "dhours",
        "dLP",
        "dk",
        "dLQ_BLS_interpolated",
        "dLQ_Aaronson_Sullivan",
        "dLQ",
        "alpha",
        "dtfp",
        "dutil",
        "dtfp_util",
        "relativePrice",
        "invShare",
        "dtfp_I",
        "dtfp_C",
        "du_invest",
        "du_consumption",
        "dtfp_I_util",
        "dtfp_C_util",
    )
    q1 = (
        "1947:Q1",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0.32,
        None,
        None,
        None,
        None,
        0.19,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    q2 = (
        "1947:Q2",
        1.25,
        1.75,
        1.5,
        -0.5,
        2.0,
        4.88,
        0.59,
        None,
        0.59,
        0.325,
        -0.15,
        1.18,
        -1.33,
        -4.48,
        0.19,
        -3.77,
        0.71,
        0.45,
        1.35,
        -4.22,
        -0.64,
    )
    workbook = Workbook()
    workbook.remove(workbook.active)
    _add_sheet(
        workbook,
        "quarterly",
        [
            (
                "Note:  All variables are percent change at an annual rate (=400 * change in "
                "natural log).",
            ),
            headers,
            q1,
            q2,
            (None,) * len(headers),
            *(
                (label, *[float(index) for _ in range(21)])
                for index, label in enumerate(
                    (
                        "Full sample mean",
                        "1947:1-1973:1",
                        "1973:1-1995:4",
                        "1995:4-2004:4",
                        "2004:4-2019:4",
                        "Since 2019:4",
                        "Past 8 qtrs",
                        "Past 4 qtrs",
                    ),
                    start=1,
                )
            ),
        ],
    )
    _add_sheet(workbook, "annual", [headers, (1947, *([None] * 21)), (1948, *([2.5] * 21))])
    return workbook


def _grant(source: str, model: str, host: str, *, attempts: int = 3) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture:clean-economy-source-shape",
        task_attempts=attempts,
        source_attempts=attempts,
        allowed_hosts={host},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _context(source: str, model: str, host: str) -> FetchContext:
    grant = _grant(source, model, host)
    return FetchContext(
        timeout=8,
        request_budget=RequestBudget(
            task_attempts=10,
            source_attempts=3,
            grants=(grant,),
        ),
    )


def _domain_spec(domain: str) -> DomainSpec:
    contract = _CONTRACTS[domain]
    if domain == "central_bank_holdings":
        semantics = (
            "series",
            "date",
            ("date", "record_type", "security_type", "cusip"),
            ("record_type", "security_type", "cusip"),
        )
    elif domain == "inflation_expectations":
        semantics = ("series", "date", ("date",), ())
    else:
        semantics = ("series", "date", ("date", "variable"), ("variable",))
    temporal_kind, time_field, natural_key, filter_dims = semantics
    return DomainSpec(
        display_name=domain,
        rest_path=f"federal-reserve/{domain.replace('_', '-')}",
        contract=contract.__name__,
        temporal_kind=temporal_kind,
        time_field=time_field,
        natural_key=natural_key,
        filter_dims=filter_dims,
        storage_mode="transient",
        permissions=("query",),
    )


def _service_harness(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    replies: dict[str, tuple[int, bytes, str]],
    *,
    adapter: FixtureAdapter | None = None,
) -> tuple[ProviderRegistry, FixtureAdapter, Any, str, str]:
    model_id, fetcher_type, host = _IDENTITIES[domain]
    fetcher = fetcher_type()
    registry = ProviderRegistry()
    registry.register_provider_models("federal_reserve", (model_id,), fetcher)
    monkeypatch.setattr(domains, "require_domain_semantics", lambda name: _domain_spec(name))
    monkeypatch.setattr(domains, "contract_model", lambda name: _CONTRACTS[name])
    _, selected_adapter = _client(monkeypatch, replies, adapter=adapter)
    return registry, selected_adapter, fetcher, model_id, host


async def _query(
    registry: ProviderRegistry,
    *,
    model: str,
    host: str,
    query: dict[str, object],
    context: FetchContext,
) -> dict[str, object]:
    return await query_provider_model(
        registry=registry,
        source="federal_reserve",
        model=model,
        query=query,
        principal=type("Principal", (), {"allows_domain": lambda _self, _domain: True})(),
        ctx=context,
    )


def test_central_holdings_summary_service_injects_identity_and_serializes_thousands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = {
        "asOfDate": "2026-10-01",
        "mbs": "1000000000",
        "cmbs": "200000000",
        "tips": "300000000",
        "frn": "400000000",
        "tipsInflationCompensation": "50000000",
        "notesbonds": "6000000000",
        "bills": "2000000000",
        "agencies": "300000000",
        "total": "10300000000",
    }
    current = {**previous, "asOfDate": "2026-10-07", "total": "10367890000"}
    replies = {"/api/soma/summary.json": _json_reply({"soma": {"summary": [previous, current]}})}
    registry, adapter, fetcher, model, host = _service_harness(
        monkeypatch, "central_bank_holdings", replies
    )
    captured: dict[str, object] = {}
    original = fetcher.transform_query

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture)
    result = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"date": "2026-10-07", "summary": True, "source": "auto"},
            context=_context("federal_reserve", model, host),
        )
    )

    row = result["results"][0]
    assert row["date"] == "2026-10-07"
    assert row["record_type"] == "summary"
    assert row["total"] == 10_367_890.0
    assert row["change_prior_week"] == 67_890.0
    assert row["change_prior_year"] is None
    assert row["mbs"] == 1_000_000.0
    assert result["domain"] == "central_bank_holdings"
    assert result["verified"] is False
    assert captured["source"] == "federal_reserve"
    assert captured["market"] == "us"
    assert len(adapter.calls) == 1
    assert urlsplit(adapter.calls[0].url).query == ""
    assert not any(
        key.lower() in {"authorization", "x-api-key", "api-key"} for key in adapter.calls[0].headers
    )


def test_central_holdings_default_detail_converts_api_dollars_and_preserves_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding = {
        "asOfDate": "2026-10-07",
        "cusip": "912797UJ4",
        "maturityDate": "2026-10-08",
        "issuer": "",
        "spread": "",
        "coupon": "",
        "parValue": "20489792900",
        "inflationCompensation": "",
        "percentOutstanding": "0.0734993916240696",
        "changeFromPriorWeek": "0",
        "changeFromPriorYear": "",
        "securityType": "Bills",
    }
    replies = {
        "/api/soma/asofdates/latest.json": _json_reply({"soma": {"asOfDates": ["2026-10-07"]}}),
        "/api/soma/tsy/get/all/asof/2026-10-07.json": _json_reply(
            {"soma": {"holdings": [holding]}}
        ),
    }
    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch, "central_bank_holdings", replies
    )
    result = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={},
            context=_context("federal_reserve", model, host),
        )
    )
    row = result["results"][0]
    assert row["record_type"] == "holding"
    assert row["par_value"] == 20_489_792.9
    assert row["face_value"] is None
    assert row["coupon"] is None
    assert row["maturity_date"] == "2026-10-08"
    assert row["percent_outstanding"] == pytest.approx(0.0734993916240696)
    assert len(adapter.calls) == 2
    assert all("api_key" not in urlsplit(call.url).query.lower() for call in adapter.calls)


def test_central_holdings_monthly_and_wam_use_official_typed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holding = {
        "asOfDate": "2026-09-30",
        "cusip": "912797UJ4",
        "maturityDate": "2026-10-08",
        "issuer": "",
        "spread": "",
        "coupon": "",
        "parValue": "1000000000",
        "inflationCompensation": "",
        "percentOutstanding": "0.05",
        "changeFromPriorWeek": "0",
        "changeFromPriorYear": "",
        "securityType": "Bills",
    }
    monthly_replies = {
        "/api/soma/tsy/get/monthly.json": _json_reply({"soma": {"holdings": [holding]}})
    }
    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch, "central_bank_holdings", monthly_replies
    )
    monthly = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"date": "2026-09-30", "monthly": True},
            context=_context("federal_reserve", model, host),
        )
    )
    assert monthly["results"][0]["par_value"] == 1_000_000.0
    assert monthly["results"][0]["record_type"] == "holding"
    assert len(adapter.calls) == 1
    assert urlsplit(adapter.calls[0].url).path == "/api/soma/tsy/get/monthly.json"

    wam_replies = _official_wam_replies()
    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "central_bank_holdings",
        wam_replies,
        adapter=WamStrictFixtureAdapter(wam_replies),
    )
    wam = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={
                "date": "2026-10-07",
                "wam": True,
                "holding_type": "mbs",
                "monthly": True,
                "cusip": "912797UJ4",
            },
            context=_context("federal_reserve", model, host),
        )
    )
    rows = wam["results"]
    assert len(rows) == 2
    assert [row["date"] for row in rows] == ["2026-10-07", "2026-10-07"]
    assert [row["record_type"] for row in rows] == ["wam", "wam"]
    assert [row["wam"] for row in rows] == [8.2, 3.7]
    assert [row["security_types"] for row in rows] == [
        ["Bills", "FRNs", "NotesBonds", "TIPS"],
        ["Agency Debts"],
    ]
    assert all(set(row) == set(CentralBankHoldings.model_fields) for row in rows)
    assert all(row["term"] is None and row["security_type"] is None for row in rows)
    assert [call.method for call in adapter.calls] == ["GET", "GET"]
    assert [call.url for call in adapter.calls] == [
        "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2026-10-07.json",
        "https://markets.newyorkfed.org/api/soma/agency/wam/agency%20debts/asof/2026-10-07.json",
    ]


@pytest.mark.parametrize(
    ("method", "url"),
    [
        (
            "POST",
            "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2026-10-07.json",
        ),
        (
            "GET",
            "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2026-10-07.json?format=json",
        ),
        (
            "GET",
            "https://example.invalid/api/soma/tsy/wam/all/asof/2026-10-07.json",
        ),
    ],
)
def test_central_holdings_wam_fixture_rejects_nonexact_http_routes(
    method: str,
    url: str,
) -> None:
    replies = _official_wam_replies()
    adapter = WamStrictFixtureAdapter(replies)
    request = requests.Request(method=method, url=url).prepare()

    response = adapter.send(request)

    treasury_path = "/api/soma/tsy/wam/all/asof/2026-10-07.json"
    assert response.status_code == 404
    assert response.content != replies[treasury_path][1]
    assert adapter.calls == [request]


@pytest.mark.parametrize(
    ("mutation", "include_agency_route"),
    [
        ("missing_bucket", False),
        ("wrong_date", True),
        ("wrong_security_type", True),
    ],
)
def test_central_holdings_wam_rejects_missing_or_mismatched_agency_bucket(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    include_agency_route: bool,
) -> None:
    replies = _official_wam_replies()
    agency_path = "/api/soma/agency/wam/agency%20debts/asof/2026-10-07.json"
    agency_body = json.loads(replies[agency_path][1])
    agency_soma = agency_body["soma"]
    if mutation == "wrong_date":
        agency_soma["asOfDate"] = "2026-10-08"
    elif mutation == "wrong_security_type":
        agency_soma["securityTypes"] = ["Bills"]
    if not include_agency_route:
        del replies[agency_path]
    else:
        replies[agency_path] = _json_reply(agency_body)

    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "central_bank_holdings",
        replies,
        adapter=WamStrictFixtureAdapter(replies),
    )
    with pytest.raises(ProviderModelQueryUpstreamError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query={"date": "2026-10-07", "wam": True},
                context=_context("federal_reserve", model, host),
            )
        )
    assert [call.method for call in adapter.calls] == ["GET", "GET"]
    assert [call.url for call in adapter.calls] == [
        "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2026-10-07.json",
        "https://markets.newyorkfed.org/api/soma/agency/wam/agency%20debts/asof/2026-10-07.json",
    ]


@pytest.mark.parametrize(
    "query",
    [
        {"date": "2026-10-07", "wam": True, "monthly": "true"},
        {"date": "2026-10-07", "wam": True, "cusip": "invalid"},
    ],
)
def test_central_holdings_wam_rejects_invalid_typed_lower_priority_fields_before_io(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "central_bank_holdings",
        {},
        adapter=WamStrictFixtureAdapter({}),
    )
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(_grant("federal_reserve", model, host, attempts=3),),
    )
    with pytest.raises(ProviderModelQueryValidationError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query=query,
                context=FetchContext(timeout=8, request_budget=budget),
            )
        )
    assert budget.attempts_used == 0
    assert adapter.calls == []


def test_central_holdings_wam_budget_exhaustion_does_not_return_one_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _official_wam_replies()
    registry, adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "central_bank_holdings",
        replies,
        adapter=WamStrictFixtureAdapter(replies),
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=3,
        grants=(_grant("federal_reserve", model, host, attempts=3),),
    )
    with pytest.raises(ProviderModelQueryBudgetError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query={"date": "2026-10-07", "wam": True},
                context=FetchContext(timeout=8, request_budget=budget),
            )
        )
    assert [call.method for call in adapter.calls] == ["GET"]
    assert [call.url for call in adapter.calls] == [
        "https://markets.newyorkfed.org/api/soma/tsy/wam/all/asof/2026-10-07.json"
    ]
    assert budget.attempts_used == 1


def test_central_holdings_wam_valid_query_rejects_zero_task_attempt_budget_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _official_wam_replies()
    adapter = WamStrictFixtureAdapter(replies)
    _registry, _adapter, fetcher, model, host = _service_harness(
        monkeypatch,
        "central_bank_holdings",
        replies,
        adapter=adapter,
    )
    budget = RequestBudget(
        task_attempts=0,
        source_attempts=3,
        grants=(_grant("federal_reserve", model, host, attempts=3),),
    )
    context = FetchContext(timeout=8, request_budget=budget)

    with pytest.raises(RequestAttemptLimitError):
        fetcher.fetch(
            ctx=context,
            date="2026-10-07",
            wam=True,
            source="federal_reserve",
            market="us",
        )

    assert budget.attempts_used == 0
    assert budget.attempts_used_for("federal_reserve") == 0
    assert adapter.calls == []


def test_philly_inflation_service_keeps_quarter_start_na_nulls_and_injected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbook = _philly_workbook()
    replies = {
        "/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
        "historical-data/Inflation.xlsx": _xlsx_reply(workbook)
    }
    registry, adapter, fetcher, model, host = _service_harness(
        monkeypatch, "inflation_expectations", replies
    )
    captured: dict[str, object] = {}
    original = fetcher.transform_query

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture)
    result = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"start_date": "1970-04-01", "end_date": "1970-04-01"},
            context=_context("federal_reserve", model, host),
        )
    )
    assert result["results"] == [
        {
            "date": "1970-04-01",
            "infpgdp1yr": 2.9851,
            "infcpi1yr": None,
            "infcpi10yr": None,
        }
    ]
    assert captured["source"] == "federal_reserve"
    assert captured["market"] == "us"
    assert len(adapter.calls) == 1
    assert (
        urlsplit(adapter.calls[0].url).query == "hash=0E7FC9E86818CF8A1C512953603AA6D8&sc_lang=en"
    )
    assert not any(
        "api_key" in key.lower() or "authorization" in key.lower()
        for key in adapter.calls[0].headers
    )


def test_sf_tfp_quarter_annual_and_summary_are_read_from_their_official_tabs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workbook = _tfp_workbook()
    replies = {"/wp-content/uploads/quarterly_tfp.xlsx": _xlsx_reply(workbook)}
    registry, adapter, fetcher, model, host = _service_harness(
        monkeypatch, "total_factor_productivity", replies
    )
    captured: dict[str, object] = {}
    original = fetcher.transform_query

    def capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture)
    quarter = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"frequency": "quarter", "start_date": "1947-04-01", "end_date": "1947-06-30"},
            context=_context("federal_reserve", model, host),
        )
    )
    assert len(quarter["results"]) == 1
    assert quarter["results"][0]["date"] == "1947:Q2"
    assert quarter["results"][0]["d_y"] == 1.5
    assert quarter["results"][0]["alpha"] == 0.325
    assert captured["source"] == "federal_reserve"
    assert captured["market"] == "us"
    assert len(adapter.calls) == 1
    assert urlsplit(adapter.calls[0].url).query == "2026-10-09="

    registry, _adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "total_factor_productivity",
        {"/wp-content/uploads/quarterly_tfp.xlsx": _xlsx_reply(workbook)},
    )
    annual = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"frequency": "annual", "start_date": "1948-01-01", "end_date": "1948-12-31"},
            context=_context("federal_reserve", model, host),
        )
    )
    assert annual["results"] == [
        {
            "date": "1948",
            **dict.fromkeys(_TFP_VALUE_FIELDS, 2.5),
            "variable": None,
            "variable_title": None,
            "full_sample_mean": None,
            "past_4_quarters": None,
            "past_8_quarters": None,
            "since_2019": None,
            "period_2004_2019": None,
            "period_1995_2004": None,
            "period_1973_1995": None,
            "period_1947_1973": None,
        }
    ]

    registry, _adapter, _fetcher, model, host = _service_harness(
        monkeypatch,
        "total_factor_productivity",
        {"/wp-content/uploads/quarterly_tfp.xlsx": _xlsx_reply(workbook)},
    )
    summary = asyncio.run(
        _query(
            registry,
            model=model,
            host=host,
            query={"frequency": "summary"},
            context=_context("federal_reserve", model, host),
        )
    )
    tfp_row = next(row for row in summary["results"] if row["variable"] == "d_tfp")
    assert len(summary["results"]) == 21
    assert tfp_row["full_sample_mean"] == 1.0
    assert tfp_row["past_4_quarters"] == 8.0
    assert tfp_row["period_1947_1973"] == 2.0
    assert tfp_row["date"] is None
    assert tfp_row["variable_title"] == "Business-sector total factor productivity growth"
    assert all(
        "authorization" not in key.lower() and "api-key" not in key.lower()
        for key in adapter.calls[0].headers
    )


_TFP_VALUE_FIELDS = tuple(clean_economy._TFP_SOURCE_TO_FIELD.values())


@pytest.mark.parametrize(
    ("domain", "model", "host", "query"),
    [
        (
            "central_bank_holdings",
            "CentralBankHoldings",
            "markets.newyorkfed.org",
            {"monthly": True, "holding_type": "mbs"},
        ),
        (
            "inflation_expectations",
            "InflationExpectations",
            "www.philadelphiafed.org",
            {"start_date": "1982-01-01", "end_date": "1981-01-01"},
        ),
        (
            "total_factor_productivity",
            "TotalFactorProductivity",
            "www.frbsf.org",
            {"frequency": "summary", "start_date": "2020-01-01"},
        ),
    ],
)
def test_invalid_queries_are_rejected_before_any_source_io(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    model: str,
    host: str,
    query: dict[str, object],
) -> None:
    registry, adapter, _fetcher, _model, _host = _service_harness(monkeypatch, domain, {})
    with pytest.raises(ProviderModelQueryValidationError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query=query,
                context=_context("federal_reserve", model, host),
            )
        )
    assert adapter.calls == []


@pytest.mark.parametrize(
    ("domain", "model", "host", "query", "replies"),
    [
        (
            "central_bank_holdings",
            "CentralBankHoldings",
            "markets.newyorkfed.org",
            {"summary": True, "date": "2026-10-07"},
            {"/api/soma/summary.json": _json_reply({"soma": {"summary": []}})},
        ),
        (
            "central_bank_holdings",
            "CentralBankHoldings",
            "markets.newyorkfed.org",
            {"date": "2026-10-07"},
            {"/api/soma/asofdates/latest.json": _json_reply({"soma": {}})},
        ),
        (
            "central_bank_holdings",
            "CentralBankHoldings",
            "markets.newyorkfed.org",
            {"date": "2026-10-07"},
            {
                "/api/soma/asofdates/latest.json": _json_reply(
                    {"soma": {"asOfDates": ["2026-10-07"]}}
                ),
                "/api/soma/tsy/get/all/asof/2026-10-07.json": _json_reply(
                    {"soma": {"holdings": [{"asOfDate": "2026-10-07", "securityType": "Bills"}]}}
                ),
            },
        ),
    ],
)
def test_central_missing_rows_empty_exact_date_and_missing_fields_stay_distinct(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    model: str,
    host: str,
    query: dict[str, object],
    replies: dict[str, tuple[int, bytes, str]],
) -> None:
    registry, _adapter, _fetcher, _model, _host = _service_harness(monkeypatch, domain, replies)
    expected = ProviderModelQueryUpstreamError
    if query.get("summary") is True:
        with pytest.raises(expected):
            asyncio.run(
                _query(
                    registry,
                    model=model,
                    host=host,
                    query=query,
                    context=_context("federal_reserve", model, host),
                )
            )
    else:
        with pytest.raises(expected):
            asyncio.run(
                _query(
                    registry,
                    model=model,
                    host=host,
                    query=query,
                    context=_context("federal_reserve", model, host),
                )
            )


def test_rate_limit_is_counted_once_and_missing_grant_fails_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "InflationExpectations"
    host = "www.philadelphiafed.org"
    workbook_reply = _xlsx_reply(_philly_workbook())
    registry, adapter, _fetcher, _model, _host = _service_harness(
        monkeypatch,
        "inflation_expectations",
        {
            "/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
            "historical-data/Inflation.xlsx": (
                429,
                b"busy",
                "text/plain",
            )
        },
    )
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=3,
        grants=(_grant("federal_reserve", model, host, attempts=3),),
    )
    with pytest.raises(ProviderModelQueryUpstreamError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query={},
                context=FetchContext(timeout=8, request_budget=budget),
            )
        )
    assert len(adapter.calls) == 1
    assert budget.attempts_used == 1

    registry, adapter, _fetcher, _model, _host = _service_harness(
        monkeypatch,
        "inflation_expectations",
        {
            "/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
            "historical-data/Inflation.xlsx": workbook_reply
        },
    )
    denied_budget = RequestBudget()
    with pytest.raises(ProviderModelQueryAuthorizationError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query={},
                context=FetchContext(timeout=8, request_budget=denied_budget),
            )
        )
    assert adapter.calls == []
    assert denied_budget.attempts_used == 0


@pytest.mark.parametrize(
    ("domain", "model", "host", "query"),
    [
        (
            "central_bank_holdings",
            "CentralBankHoldings",
            "markets.newyorkfed.org",
            {"summary": True},
        ),
        ("inflation_expectations", "InflationExpectations", "www.philadelphiafed.org", {}),
        (
            "total_factor_productivity",
            "TotalFactorProductivity",
            "www.frbsf.org",
            {"frequency": "quarter"},
        ),
    ],
)
def test_pre_cancelled_service_calls_use_no_source_attempt(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    model: str,
    host: str,
    query: dict[str, object],
) -> None:
    registry, adapter, _fetcher, _model, _host = _service_harness(monkeypatch, domain, {})
    event = Event()
    event.set()
    grant = _grant("federal_reserve", model, host)
    budget = RequestBudget(task_attempts=10, source_attempts=3, grants=(grant,))
    context = FetchContext(
        timeout=8,
        _thread_cancel_event=event,
        request_budget=budget,
    )
    with pytest.raises(ProviderModelQueryCancelledError):
        asyncio.run(
            _query(
                registry,
                model=model,
                host=host,
                query=query,
                context=context,
            )
        )
    assert adapter.calls == []
    assert budget.attempts_used == 0


def test_spf_invalid_value_and_tfp_unknown_frequency_fail_as_source_shape_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "InflationExpectations"
    host = "www.philadelphiafed.org"
    registry, _adapter, fetcher, _model, _host = _service_harness(
        monkeypatch,
        "inflation_expectations",
        {
            "/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
            "historical-data/Inflation.xlsx": _xlsx_reply(_philly_workbook(bad_value=True))
        },
    )
    with pytest.raises(clean_economy.FederalReserveSourceError):
        fetcher.fetch(ctx=_context("federal_reserve", model, host))

    tfp = clean_economy.TotalFactorProductivityFetcher()
    with pytest.raises(ValidationError):
        tfp.transform_query(frequency="monthly")

    workbook = _tfp_workbook()
    workbook["quarterly"]["B4"] = "not-a-number"
    _registry, _adapter, tfp, model, host = _service_harness(
        monkeypatch,
        "total_factor_productivity",
        {"/wp-content/uploads/quarterly_tfp.xlsx": _xlsx_reply(workbook)},
    )
    with pytest.raises(clean_economy.FederalReserveSourceError):
        tfp.fetch(ctx=_context("federal_reserve", model, host), frequency="quarter")


def test_central_source_malformed_number_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "CentralBankHoldings"
    host = "markets.newyorkfed.org"
    holding = {
        "asOfDate": "2026-10-07",
        "cusip": "912797UJ4",
        "maturityDate": "2026-10-08",
        "issuer": "",
        "spread": "",
        "coupon": "",
        "parValue": "$20 billion",
        "inflationCompensation": "",
        "percentOutstanding": "0.07",
        "changeFromPriorWeek": "0",
        "changeFromPriorYear": "",
        "securityType": "Bills",
    }
    replies = {
        "/api/soma/tsy/get/all/asof/2026-10-07.json": _json_reply({"soma": {"holdings": [holding]}})
    }
    _registry, _adapter, fetcher, _model, _host = _service_harness(
        monkeypatch, "central_bank_holdings", replies
    )
    with pytest.raises(clean_economy.FederalReserveSourceError):
        fetcher.fetch(
            ctx=_context("federal_reserve", model, host),
            date="2026-10-07",
        )


def test_central_exact_date_does_not_fallback_to_another_asof_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "CentralBankHoldings"
    host = "markets.newyorkfed.org"
    holding = {
        "asOfDate": "2026-10-07",
        "cusip": "912797UJ4",
        "maturityDate": "2026-10-08",
        "issuer": "",
        "spread": "",
        "coupon": "",
        "parValue": "20489792900",
        "inflationCompensation": "",
        "percentOutstanding": "0.07",
        "changeFromPriorWeek": "0",
        "changeFromPriorYear": "",
        "securityType": "Bills",
    }
    replies = {
        "/api/soma/tsy/get/all/asof/2026-10-06.json": _json_reply({"soma": {"holdings": [holding]}})
    }
    _registry, _adapter, fetcher, _model, _host = _service_harness(
        monkeypatch, "central_bank_holdings", replies
    )
    with pytest.raises(clean_economy.FederalReserveNoDataError):
        fetcher.fetch(
            ctx=_context("federal_reserve", model, host),
            date="2026-10-06",
        )


def test_inflation_empty_valid_range_returns_empty_and_preserves_quarterly_missingness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "InflationExpectations"
    host = "www.philadelphiafed.org"
    replies = {
        "/-/media/FRBP/Assets/Surveys-And-Data/survey-of-professional-forecasters/"
        "historical-data/Inflation.xlsx": _xlsx_reply(_philly_workbook())
    }
    _registry, _adapter, fetcher, _model, _host = _service_harness(
        monkeypatch, "inflation_expectations", replies
    )
    rows = fetcher.fetch(
        ctx=_context("federal_reserve", model, host),
        start_date="1960-01-01",
        end_date="1960-12-31",
    )
    assert rows == ()
