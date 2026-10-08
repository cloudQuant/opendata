"""Offline Registry, source-query, and transient-domain checks for ECB YC/BPS."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opendata.api.provider_model_warehouse as warehouse_api
import opendata.data.http_client as http_client_module
import opendata.data.providers.ecb.models._series_client as series_client
from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_query import (
    get_provider_model_query_context,
    get_provider_model_query_registry,
)
from opendata.api.provider_model_warehouse import (
    get_provider_model_warehouse_context,
    get_provider_model_warehouse_registry,
)
from opendata.api.provider_models import get_provider_model_registry, router
from opendata.data.domains import contract_model, require_domain_semantics
from opendata.data.http_client import GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.pipeline.alert_matrix import warehouse_tables
from opendata.services.provider_model_export import (
    ProviderModelExportValidationError,
    export_provider_model_snapshot,
)
from opendata.services.provider_model_store import (
    ProviderModelStoreError,
    write_provider_model_rows,
)
from opendata.services.provider_model_warehouse import (
    ProviderModelWarehouseNotFoundError,
    query_provider_model_warehouse,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_HOST = "data-api.ecb.europa.eu"
_YC_MODEL = "YieldCurve"
_BPS_MODEL = "BalanceOfPayments"
_YC_KEY = "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"
_BPS_M_KEY = "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_BPS_Q_KEY = "BPS.Q.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
_COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_YC_DIMENSIONS = (
    "FREQ",
    "REF_AREA",
    "CURRENCY",
    "PROVIDER_FM",
    "INSTRUMENT_FM",
    "PROVIDER_FM_ID",
    "DATA_TYPE_FM",
)
_BPS_DIMENSIONS = (
    "FREQ",
    "ADJUSTMENT",
    "REF_AREA",
    "COUNTERPART_AREA",
    "REF_SECTOR",
    "COUNTERPART_SECTOR",
    "FLOW_STOCK_ENTRY",
    "ACCOUNTING_ENTRY",
    "INT_ACC_ITEM",
    "FUNCTIONAL_CAT",
    "INSTR_ASSET",
    "MATURITY",
    "UNIT_MEASURE",
    "CURRENCY_DENOM",
    "VALUATION",
    "COMP_METHOD",
    "TYPE_ENTITY",
)
_YC_VALUES = {
    "FREQ": "B",
    "REF_AREA": "U2",
    "CURRENCY": "EUR",
    "PROVIDER_FM": "4F",
    "INSTRUMENT_FM": "G_N_A",
    "PROVIDER_FM_ID": "SV_C_YM",
    "DATA_TYPE_FM": "SR_3M",
}
_BPS_VALUES = {
    "FREQ": "M",
    "ADJUSTMENT": "N",
    "REF_AREA": "I10",
    "COUNTERPART_AREA": "W1",
    "REF_SECTOR": "S121",
    "COUNTERPART_SECTOR": "S1",
    "FLOW_STOCK_ENTRY": "T",
    "ACCOUNTING_ENTRY": "A",
    "INT_ACC_ITEM": "FA",
    "FUNCTIONAL_CAT": "R",
    "INSTR_ASSET": "F",
    "MATURITY": "_Z",
    "UNIT_MEASURE": "EUR",
    "CURRENCY_DENOM": "X1",
    "VALUATION": "_X",
    "COMP_METHOD": "N",
    "TYPE_ENTITY": "ALL",
}


class RecordingSession(requests.Session):
    """Record governed request sends while returning only synthetic responses."""

    def __init__(self, outcomes: Sequence[requests.Response] = ()) -> None:
        super().__init__()
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method, url, dict(kwargs)))
        if self._outcomes:
            return self._outcomes.popleft()
        return _response(b"")


def _response(body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/vnd.sdmx.genericdata+xml;version=2.1"
    return response


def _series_body(
    product: str,
    dimensions: dict[str, str],
    observations: Sequence[tuple[str, str | None]],
) -> bytes:
    dimension_ids = _YC_DIMENSIONS if product == "YC" else _BPS_DIMENSIONS
    dimension_values = "".join(
        f'<generic:Value id="{name}" value="{dimensions[name]}"/>' for name in dimension_ids
    )
    observation_values = "".join(
        "<generic:Obs>"
        f'<generic:ObsDimension id="TIME_PERIOD" value="{period}"/>'
        + (f'<generic:ObsValue id="OBS_VALUE" value="{value}"/>' if value is not None else "")
        + "<generic:Attributes>"
        '<generic:Value id="OBS_STATUS" value="raw-status"/>'
        '<generic:Value id="UNKNOWN_OBS_ATTRIBUTE" value="kept-observation"/>'
        "</generic:Attributes></generic:Obs>"
        for period, value in observations
    )
    schema_location = f"{_MESSAGE_NS} SDMXMessage.xsd {_GENERIC_NS} SDMXDataGeneric.xsd"
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData xmlns:message="{_MESSAGE_NS}" xmlns:common="{_COMMON_NS}"
 xmlns:generic="{_GENERIC_NS}" xmlns:xsi="{_XSI_NS}"
 xsi:schemaLocation="{schema_location}">
 <message:Header>
  <message:Structure structureID="SYNTHETIC" dimensionAtObservation="TIME_PERIOD">
   <common:Structure><Ref agencyID="ECB" id="SYNTHETIC" version="1.0"
    class="DataStructure" package="datastructure"/></common:Structure>
  </message:Structure>
 </message:Header>
 <message:DataSet structureRef="SYNTHETIC">
  <generic:Attributes><generic:Value id="UNKNOWN_DATASET"\
 value="kept-dataset"/></generic:Attributes>
  <generic:Group type="SYNTHETIC_GROUP">
   <generic:GroupKey><generic:Value id="FREQ" value="{dimensions["FREQ"]}"/></generic:GroupKey>
   <generic:Attributes><generic:Value id="UNKNOWN_GROUP" value="kept-group"/></generic:Attributes>
  </generic:Group>
  <generic:Series>
   <generic:SeriesKey>{dimension_values}</generic:SeriesKey>
   <generic:Attributes><generic:Value id="UNKNOWN_SERIES" value="kept-series"/></generic:Attributes>
   {observation_values}
  </generic:Series>
 </message:DataSet>
</message:GenericData>'''
    return xml.encode("utf-8")


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("ecb", registry)
    return registry


def _grant(
    model: str,
    operation: RequestOperation,
    *,
    host: str = _HOST,
    task_attempts: int = 1,
    source_attempts: int = 1,
) -> RequestGrant:
    return RequestGrant(
        source="ecb",
        canonical_model=model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="fixture:synthetic-only",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts=(host,),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def _context(
    grants: Sequence[RequestGrant],
    operation: RequestOperation,
    *,
    task_attempts: int | None = None,
    source_attempts: int | None = None,
) -> FetchContext:
    return FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(
            task_attempts=task_attempts if task_attempts is not None else len(grants),
            source_attempts=source_attempts if source_attempts is not None else len(grants),
            grants=tuple(grants),
        ),
        operation=operation,
    )


def _install_recording_client(
    monkeypatch: pytest.MonkeyPatch,
    *outcomes: requests.Response,
) -> RecordingSession:
    session = RecordingSession(outcomes)
    client = GovernedHttpClient(
        HttpClientConfig(max_attempts=1, rate_limit_per_host=None),
        session=session,
        raw_response_cache=None,
    )
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    monkeypatch.setattr(series_client, "get_shared_http_client", lambda: client)
    return session


def _api(
    registry: ProviderRegistry,
    *,
    ctx: FetchContext | None,
    allowed_domains: frozenset[str] = frozenset(
        {"yield_curve", "balance_of_payments", "currency_reference_rates"}
    ),
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(allows_domain=lambda domain: domain in allowed_domains)
    app.dependency_overrides[get_provider_model_registry] = lambda: registry
    app.dependency_overrides[get_provider_model_query_registry] = lambda: registry
    app.dependency_overrides[get_provider_model_warehouse_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_provider_model_query_context] = lambda: ctx
    app.dependency_overrides[get_provider_model_warehouse_context] = lambda: ctx
    return TestClient(app)


def _expected_url(product: str, key: str) -> str:
    return f"https://{_HOST}/service/data/{product}/{key.removeprefix(f'{product}.')}"


def test_registry_schema_and_query_paths_preserve_yc_and_bps_source_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    quarterly_values = {**_BPS_VALUES, "FREQ": "Q"}
    session = _install_recording_client(
        monkeypatch,
        _response(_series_body("YC", _YC_VALUES, [("2026-10-07", "-0.000")])),
        _response(_series_body("BPS", _BPS_VALUES, [("2026-06", "123.4500")])),
        _response(_series_body("BPS", quarterly_values, [("2026-Q2", None)])),
    )
    context = _context(
        (
            _grant(
                _YC_MODEL,
                RequestOperation.QUERY,
                source_attempts=3,
            ),
            _grant(
                _BPS_MODEL,
                RequestOperation.QUERY,
                task_attempts=3,
                source_attempts=3,
            ),
        ),
        RequestOperation.QUERY,
        task_attempts=3,
        source_attempts=3,
    )

    with _api(registry, ctx=context) as api:
        listed = api.get("/providers/models?source=ecb")
        yc_schema = api.get(f"/providers/ecb/models/{_YC_MODEL}/schema")
        bps_schema = api.get(f"/providers/ecb/models/{_BPS_MODEL}/schema")
        yc = api.post(
            f"/providers/ecb/models/{_YC_MODEL}/query",
            json={
                "query": {
                    "series_key": _YC_KEY,
                    "start_date": "2026-10-07",
                    "end_date": "2026-10-09",
                }
            },
        )
        bps_month = api.post(
            f"/providers/ecb/models/{_BPS_MODEL}/query",
            json={
                "query": {
                    "series_key": _BPS_M_KEY,
                    "start_period": "2026-06",
                    "end_period": "2026-07",
                }
            },
        )
        bps_quarter = api.post(
            f"/providers/ecb/models/{_BPS_MODEL}/query",
            json={
                "query": {
                    "series_key": _BPS_Q_KEY,
                    "start_period": "2026-Q2",
                    "end_period": "2026-Q3",
                }
            },
        )

    assert listed.status_code == 200
    assert {
        (item["source"], item["model"], item["domain"], item["verified"])
        for item in listed.json()["data"]
    } == {
        ("ecb", "CurrencyReferenceRates", "currency_reference_rates", False),
        ("ecb", _YC_MODEL, "yield_curve", False),
        ("ecb", _BPS_MODEL, "balance_of_payments", False),
    }
    for response, expected_model in ((yc_schema, _YC_MODEL), (bps_schema, _BPS_MODEL)):
        assert response.status_code == 200
        schema_data = response.json()["data"]
        assert schema_data["model"] == expected_model
        assert schema_data["verified"] is False
        assert "series_key" in schema_data["schema"]["required"]

    assert [response.status_code for response in (yc, bps_month, bps_quarter)] == [200, 200, 200]
    yc_data = yc.json()["data"]
    yc_row = yc_data["results"][0]
    assert (yc_data["domain"], yc_data["verified"]) == ("yield_curve", False)
    assert (yc_row["series_key"], yc_row["date"], yc_row["frequency"]) == (
        _YC_KEY,
        "2026-10-07",
        "B",
    )
    assert yc_row["source_value"] == "-0.000"
    assert math.copysign(1.0, yc_row["value"]) == -1.0
    assert yc_row["dataset_attributes"] == {"UNKNOWN_DATASET": "kept-dataset"}
    assert yc_row["series_attributes"] == {"UNKNOWN_SERIES": "kept-series"}
    assert yc_row["observation_attributes"] == {
        "OBS_STATUS": "raw-status",
        "UNKNOWN_OBS_ATTRIBUTE": "kept-observation",
    }
    assert yc_row["group_context"] == [
        {
            "group_type": "SYNTHETIC_GROUP",
            "key": {"FREQ": "B"},
            "attributes": {"UNKNOWN_GROUP": "kept-group"},
        }
    ]

    bps_month_row = bps_month.json()["data"]["results"][0]
    assert bps_month.json()["data"]["domain"] == "balance_of_payments"
    assert (bps_month_row["series_key"], bps_month_row["period"], bps_month_row["frequency"]) == (
        _BPS_M_KEY,
        "2026-06",
        "M",
    )
    assert bps_month_row["source_value"] == "123.4500"
    assert bps_month_row["dataset_attributes"] == {"UNKNOWN_DATASET": "kept-dataset"}
    assert bps_month_row["series_attributes"] == {"UNKNOWN_SERIES": "kept-series"}
    assert bps_month_row["observation_attributes"]["UNKNOWN_OBS_ATTRIBUTE"] == "kept-observation"

    bps_quarter_row = bps_quarter.json()["data"]["results"][0]
    assert (
        bps_quarter_row["series_key"],
        bps_quarter_row["period"],
        bps_quarter_row["frequency"],
    ) == (
        _BPS_Q_KEY,
        "2026-Q2",
        "Q",
    )
    assert bps_quarter_row["value"] is None
    assert bps_quarter_row["source_value"] is None

    assert [method for method, _url, _kwargs in session.calls] == ["GET", "GET", "GET"]
    assert [url for _method, url, _kwargs in session.calls] == [
        _expected_url("YC", _YC_KEY),
        _expected_url("BPS", _BPS_M_KEY),
        _expected_url("BPS", _BPS_Q_KEY),
    ]
    assert [kwargs["params"]["startPeriod"] for _method, _url, kwargs in session.calls] == [
        "2026-10-07",
        "2026-06",
        "2026-Q2",
    ]
    assert [kwargs["params"]["endPeriod"] for _method, _url, kwargs in session.calls] == [
        "2026-10-09",
        "2026-07",
        "2026-Q3",
    ]
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 3


@pytest.mark.parametrize(
    ("model", "domain", "query"),
    [
        (_YC_MODEL, "yield_curve", {"series_key": _YC_KEY}),
        (_BPS_MODEL, "balance_of_payments", {"series_key": _BPS_M_KEY}),
    ],
)
def test_zero_grant_acl_and_strict_query_reject_before_source_send(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    domain: str,
    query: dict[str, str],
) -> None:
    registry = _registry()
    session = _install_recording_client(monkeypatch)
    path = f"/providers/ecb/models/{model}/query"

    with _api(registry, ctx=None) as api:
        no_grant = api.post(path, json={"query": query})
    assert no_grant.status_code == 403
    assert session.calls == []

    authorized = _context((_grant(model, RequestOperation.QUERY),), RequestOperation.QUERY)
    with _api(registry, ctx=authorized, allowed_domains=frozenset()) as api:
        denied = api.post(path, json={"query": query})
    assert denied.status_code == 403
    assert session.calls == []

    with _api(registry, ctx=authorized) as api:
        invalid = api.post(path, json={"query": {**query, "unexpected": "rejected"}})
    assert invalid.status_code == 400
    assert session.calls == []
    assert domain in {"yield_curve", "balance_of_payments"}


@pytest.mark.parametrize(
    ("model", "query", "product", "dimensions", "observations"),
    [
        (
            _YC_MODEL,
            {"series_key": _YC_KEY},
            "YC",
            {**_YC_VALUES, "DATA_TYPE_FM": "SR_6M"},
            [("2026-10-07", "1.0")],
        ),
        (
            _BPS_MODEL,
            {"series_key": _BPS_M_KEY, "start_period": "2026-06", "end_period": "2026-06"},
            "BPS",
            _BPS_VALUES,
            [("2026-06", "1.0"), ("2026-05", "2.0")],
        ),
    ],
)
def test_wrong_response_key_or_out_of_window_row_fails_without_partial_results(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    query: dict[str, str],
    product: str,
    dimensions: dict[str, str],
    observations: Sequence[tuple[str, str | None]],
) -> None:
    registry = _registry()
    session = _install_recording_client(
        monkeypatch,
        _response(_series_body(product, dimensions, observations)),
    )
    context = _context((_grant(model, RequestOperation.QUERY),), RequestOperation.QUERY)

    with _api(registry, ctx=context) as api:
        response = api.post(
            f"/providers/ecb/models/{model}/query",
            json={"query": query},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "Provider model query failed"
    assert "results" not in response.json()
    assert len(session.calls) == 1


def test_transient_domains_remain_outside_legacy_warehouse_store_and_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opendata.data.registry as registry_module

    registry = _registry()
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    domains = ("yield_curve", "balance_of_payments")
    assert warehouse_tables(domains) == ()

    expected_semantics = {
        "yield_curve": (
            "EcbYieldCurveObservation",
            "date",
            ("series_key", "date"),
            ("series_key", "date", "frequency", "ref_area", "currency"),
        ),
        "balance_of_payments": (
            "EcbBalanceOfPaymentsObservation",
            "period",
            ("series_key", "period"),
            ("series_key", "period", "frequency", "ref_area", "counterpart_area", "unit_measure"),
        ),
    }
    for domain, (contract, time_field, natural_key, filter_dims) in expected_semantics.items():
        spec = require_domain_semantics(domain)
        assert spec.contract == contract
        assert contract_model(domain).__name__ == contract
        assert (spec.time_field, spec.natural_key, spec.filter_dims) == (
            time_field,
            natural_key,
            filter_dims,
        )
        assert (spec.storage_mode, spec.permissions) == ("transient", ("query",))

    unsupported = (
        (_YC_MODEL, "yield_curve"),
        (_BPS_MODEL, "balance_of_payments"),
    )
    for model, domain in unsupported:
        query_context = _context(
            (_grant(model, RequestOperation.QUERY, host="warehouse.example.test"),),
            RequestOperation.QUERY,
        )
        with pytest.raises(ProviderModelWarehouseNotFoundError):
            asyncio.run(
                query_provider_model_warehouse(
                    registry=registry,
                    source="ecb",
                    model=model,
                    query={},
                    principal=SimpleNamespace(
                        allows_domain=lambda value, domain=domain: value == domain
                    ),
                    engine_factory=lambda: pytest.fail(
                        "transient warehouse query opened an engine"
                    ),
                    ctx=query_context,
                )
            )

        store_context = _context(
            (_grant(model, RequestOperation.STORE, host="warehouse.example.test"),),
            RequestOperation.STORE,
        )
        with pytest.raises(ProviderModelStoreError, match="not enabled for native DWD storage"):
            write_provider_model_rows(
                engine=object(),  # type: ignore[arg-type] - rejection must precede engine use
                registry=registry,
                source="ecb",
                model=model,
                rows=(),
                observed_at=datetime.now(timezone.utc),
                ctx=store_context,
            )

        export_context = _context(
            (_grant(model, RequestOperation.EXPORT, host="warehouse.example.test"),),
            RequestOperation.EXPORT,
        )
        with pytest.raises(ProviderModelExportValidationError, match="not supported"):
            export_provider_model_snapshot(
                engine_factory=lambda: pytest.fail("transient export opened an engine"),
                registry=registry,
                source="ecb",
                model=model,
                principal=SimpleNamespace(
                    allows_domain=lambda value, domain=domain: value == domain
                ),
                ctx=export_context,
                max_records=1,
                max_bytes=1024,
            )

    warehouse_engine_calls: list[bool] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: warehouse_engine_calls.append(True),
    )
    query_context = _context(
        (
            _grant(_YC_MODEL, RequestOperation.QUERY, host="warehouse.example.test"),
            _grant(_BPS_MODEL, RequestOperation.QUERY, host="warehouse.example.test"),
        ),
        RequestOperation.QUERY,
        task_attempts=2,
        source_attempts=2,
    )
    with _api(registry, ctx=query_context) as api:
        responses = [
            api.post(f"/providers/ecb/models/{model}/warehouse/query", json={"query": {}})
            for model, _domain in unsupported
        ]
    assert [response.status_code for response in responses] == [404, 404]
    assert warehouse_engine_calls == []
