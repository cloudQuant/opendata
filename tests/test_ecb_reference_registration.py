"""Offline registration and query-surface checks for ECB reference rates.

The XML response below is a synthetic fixture shaped like SDMX 2.1
GenericData. It is not an ECB response and no live source is contacted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import opendata.data.http_client as http_client_module
import opendata.data.providers.ecb.models._reference_client as reference_client
from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_query import (
    get_provider_model_query_context,
    get_provider_model_query_registry,
)
from opendata.api.provider_models import get_provider_model_registry, router
from opendata.data.domains import contract_model, load_domains, require_domain_semantics
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models import CurrencyReferenceRate, SdmxGroupContext
from opendata.data.protocol import FetchContext
from opendata.data.providers import register_provider, register_providers
from opendata.data.providers.catalog import get_provider, list_providers
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


_HOST = "data-api.ecb.europa.eu"
_MODEL = "CurrencyReferenceRates"
_MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
_COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
_GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_SYNTHETIC_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData xmlns:message="{_MESSAGE_NS}" xmlns:common="{_COMMON_NS}"
  xmlns:generic="{_GENERIC_NS}" xmlns:xsi="{_XSI_NS}"
  xsi:schemaLocation="{_MESSAGE_NS} SDMXMessage.xsd {_GENERIC_NS} SDMXDataGeneric.xsd">
  <message:Header>
    <message:ID>synthetic-registration-test</message:ID>
    <message:Test>false</message:Test>
    <message:Prepared>2026-10-08T00:00:00Z</message:Prepared>
    <message:Sender id="SYNTHETIC"/>
    <message:Structure structureID="ECB_REF" dimensionAtObservation="TIME_PERIOD">
      <common:Structure><Ref agencyID="ECB" id="EXR" version="1.0"
        class="DataStructure" package="datastructure"/></common:Structure>
    </message:Structure>
  </message:Header>
  <message:DataSet structureRef="ECB_REF">
    <generic:Attributes>
      <generic:Value id="UNKNOWN_DATASET" value="preserved dataset value"/>
    </generic:Attributes>
    <generic:Group type="EXR_GROUP">
      <generic:GroupKey><generic:Value id="FREQ" value="D"/></generic:GroupKey>
      <generic:Attributes>
        <generic:Value id="COMMENT" value="preserved group value"/>
      </generic:Attributes>
    </generic:Group>
    <generic:Series>
      <generic:SeriesKey>
        <generic:Value id="FREQ" value="D"/>
        <generic:Value id="CURRENCY" value="USD"/>
        <generic:Value id="CURRENCY_DENOM" value="EUR"/>
        <generic:Value id="EXR_TYPE" value="SP00"/>
        <generic:Value id="EXR_SUFFIX" value="A"/>
      </generic:SeriesKey>
      <generic:Attributes>
        <generic:Value id="SERIES_NOTE" value="raw series"/>
      </generic:Attributes>
      <generic:Obs>
        <generic:ObsDimension id="TIME_PERIOD" value="2026-10-08"/>
        <generic:ObsValue id="OBS_VALUE" value="1.234500"/>
        <generic:Attributes>
          <generic:Value id="OBS_STATUS" value="A"/>
          <generic:Value id="UNKNOWN_OBSERVATION" value="kept raw"/>
        </generic:Attributes>
      </generic:Obs>
    </generic:Series>
  </message:DataSet>
</message:GenericData>'''.encode()


class RecordingSession(requests.Session):
    """Real requests.Session surface that records fake governed sends."""

    def __init__(self, outcomes: Sequence[requests.Response] = ()) -> None:
        super().__init__()
        self._outcomes = deque(outcomes)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._lock = threading.Lock()

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        with self._lock:
            self.calls.append((method, url, dict(kwargs)))
            outcome = self._outcomes.popleft() if self._outcomes else _response(b"")
        return outcome


def _response(body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/vnd.sdmx.genericdata+xml;version=2.1"
    return response


def _context() -> FetchContext:
    grant = RequestGrant(
        source="ecb",
        canonical_model=_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:synthetic-only",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({_HOST}),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,)),
        operation=RequestOperation.QUERY,
    )


def _client(
    registry: ProviderRegistry,
    *,
    ctx: FetchContext | None,
    allowed_domain: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(
        allows_domain=lambda domain: (
            allowed_domain
            and domain in {"currency_reference_rates", "yield_curve", "balance_of_payments"}
        )
    )
    app.dependency_overrides[get_provider_model_registry] = lambda: registry
    app.dependency_overrides[get_provider_model_query_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_provider_model_query_context] = lambda: ctx
    return TestClient(app)


def _ecb_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("ecb", registry)
    return registry


def test_real_ecb_bindings_register_three_canonical_models_without_auto_promotion() -> None:
    ecb_registry = ProviderRegistry()
    added = register_provider("ecb", ecb_registry)
    provider = get_provider("ecb")

    assert len(provider.fetcher_bindings) == 6
    assert [(binding.module, binding.class_name) for binding in provider.fetcher_bindings[:3]] == [
        ("opendata.data.providers.ecb.models.cpi", "EcbCpiFetcher"),
        ("opendata.data.providers.ecb.models.gdp", "EcbGdpFetcher"),
        ("opendata.data.providers.ecb.models.rate", "EcbRateFetcher"),
    ]
    reference_binding = provider.fetcher_bindings[3]
    assert reference_binding.canonical_model_ids == (_MODEL,)
    assert reference_binding.scenario == "保留ECB源维度与属性的欧元日参考汇率研究"
    assert len(added) == 6
    assert {item.model for item in ecb_registry.list_model_descriptors()} == {
        _MODEL,
        "YieldCurve",
        "BalanceOfPayments",
    }
    assert ecb_registry.resolve_model("ecb", _MODEL).capability.verified is False
    registered_model_ids = {item.model for item in ecb_registry.list_model_descriptors()}
    assert "CurrencyHistorical" not in registered_model_ids

    registry = ProviderRegistry()
    register_providers(registry)
    descriptors = registry.list_model_descriptors()
    assert len(list_providers()) == 34
    assert len(registry.capabilities()) == 44
    assert len(descriptors) == 11
    assert {item.model for item in descriptors if item.source == "ecb"} == {
        _MODEL,
        "YieldCurve",
        "BalanceOfPayments",
    }
    assert sum(capability.participates_in_auto() for capability in registry.capabilities()) == 23


def test_domain_exports_preserve_nested_contract_and_defer_legacy_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_domains().keys() >= {"currency_reference_rates"}
    spec = require_domain_semantics("currency_reference_rates")
    assert spec.display_name == "欧元日参考汇率"
    assert spec.rest_path == "currency/reference-rates"
    assert spec.contract == "CurrencyReferenceRate"
    assert spec.temporal_kind == "series"
    assert spec.time_field == "date"
    assert spec.natural_key == ("series_key", "date")
    assert spec.filter_dims == ("series_key", "date", "quote_currency", "base_currency")
    assert spec.storage_mode == "transient"
    assert spec.permissions == ("query",)
    assert contract_model("currency_reference_rates") is CurrencyReferenceRate

    record = CurrencyReferenceRate(
        series_key="D.USD.EUR.SP00.A",
        date="2026-10-08",
        frequency="D",
        quote_currency="USD",
        base_currency="EUR",
        rate_type="SP00",
        rate_suffix="A",
        value=1.2345,
        source_value="1.234500",
        dataset_attributes={"UNKNOWN_DATASET": "preserved dataset value"},
        series_attributes={"SERIES_NOTE": "raw series"},
        observation_attributes={"OBS_STATUS": "A", "UNKNOWN_OBSERVATION": "kept raw"},
        group_context=(
            SdmxGroupContext(
                group_type="EXR_GROUP",
                key={"FREQ": "D"},
                attributes={"COMMENT": "preserved group value"},
            ),
        ),
    )
    assert CurrencyReferenceRate.model_validate_json(record.model_dump_json()) == record

    registry = ProviderRegistry()
    register_providers(registry)
    import opendata.data.registry as registry_module

    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    from opendata.pipeline.alert_matrix import registered_legs, warehouse_tables
    from opendata.pipeline.provider_model_schema import model_dwd_table_ddl

    legs = registered_legs()
    tables = warehouse_tables()
    assert legs["currency_reference_rates"] == ("ecb",)
    assert not any(
        table.startswith(("dwd_currency_reference_rates", "ods_currency_reference_rates_"))
        for table in tables
    )
    with pytest.raises(ValueError, match="does not support this domain"):
        model_dwd_table_ddl("currency_reference_rates")


def test_provider_metadata_lists_ecb_binding_without_loading_adapter_or_transport() -> None:
    script = r"""
import sys
from opendata.data.providers.catalog import get_provider, list_providers

provider = get_provider("ecb")
assert len(list_providers()) == 34
assert len(provider.fetcher_bindings) == 6
binding = provider.fetcher_bindings[3]
assert binding.module == "opendata.data.providers.ecb.models.reference_rates"
assert binding.canonical_model_ids == ("CurrencyReferenceRates",)
assert binding.scenario == "保留ECB源维度与属性的欧元日参考汇率研究"
assert "opendata.data.providers.ecb.models.reference_rates" not in sys.modules
assert "opendata.data.providers.ecb.models.yield_curve" not in sys.modules
assert "opendata.data.providers.ecb.models.balance_of_payments" not in sys.modules
assert "opendata.data.http_client" not in sys.modules
"""
    result = subprocess.run(  # noqa: S603 - isolated test interpreter runs fixed source
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_schema_and_query_api_use_registered_model_and_preserve_source_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _ecb_registry()
    session = RecordingSession((_response(_SYNTHETIC_XML),))
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr(reference_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    trusted_context = _context()

    with _client(registry, ctx=trusted_context) as api:
        model_list = api.get("/providers/models?source=ecb")
        schema = api.get("/providers/ecb/models/CurrencyReferenceRates/schema")
        response = api.post(
            "/providers/ecb/models/CurrencyReferenceRates/query",
            json={
                "query": {
                    "quote_currencies": ["USD"],
                    "start_date": "2026-10-08",
                    "end_date": "2026-10-08",
                }
            },
        )

    assert model_list.status_code == 200
    listed_models = [
        (item["source"], item["model"], item["domain"]) for item in model_list.json()["data"]
    ]
    assert set(listed_models) == {
        ("ecb", _MODEL, "currency_reference_rates"),
        ("ecb", "YieldCurve", "yield_curve"),
        ("ecb", "BalanceOfPayments", "balance_of_payments"),
    }
    assert schema.status_code == 200
    schema_data = schema.json()["data"]
    assert (schema_data["source"], schema_data["model"], schema_data["domain"]) == (
        "ecb",
        _MODEL,
        "currency_reference_rates",
    )
    assert schema_data["verified"] is False
    assert "quote_currencies" in schema_data["schema"]["required"]

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["source"], data["model"], data["domain"]) == (
        "ecb",
        _MODEL,
        "currency_reference_rates",
    )
    assert data["verified"] is False
    assert data["completeness"] == "NOT_ASSESSED"
    row = data["results"][0]
    assert row["series_key"] == "D.USD.EUR.SP00.A"
    assert row["date"] == "2026-10-08"
    assert row["frequency"] == "D"
    assert row["quote_currency"] == "USD"
    assert row["base_currency"] == "EUR"
    assert row["rate_type"] == "SP00"
    assert row["rate_suffix"] == "A"
    assert row["value"] == 1.2345
    assert row["source_value"] == "1.234500"
    assert row["dataset_attributes"] == {"UNKNOWN_DATASET": "preserved dataset value"}
    assert row["series_attributes"] == {"SERIES_NOTE": "raw series"}
    assert row["observation_attributes"] == {
        "OBS_STATUS": "A",
        "UNKNOWN_OBSERVATION": "kept raw",
    }
    assert row["group_context"] == [
        {
            "group_type": "EXR_GROUP",
            "key": {"FREQ": "D"},
            "attributes": {"COMMENT": "preserved group value"},
        }
    ]
    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
    assert kwargs["params"] == {
        "format": "genericdata",
        "detail": "full",
        "includeHistory": "false",
        "startPeriod": "2026-10-08",
        "endPeriod": "2026-10-08",
    }
    assert kwargs["headers"]["Accept"] == "application/vnd.sdmx.genericdata+xml;version=2.1"
    assert client._session_override is session
    assert trusted_context.request_budget is not None
    assert trusted_context.request_budget.attempts_used == 1


def test_query_api_rejects_missing_grant_acl_bad_query_and_unknown_model_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _ecb_registry()
    session = RecordingSession()
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr(reference_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)

    valid_request = {"query": {"quote_currencies": ["USD"]}}
    with _client(registry, ctx=None) as api:
        no_grant = api.post(
            "/providers/ecb/models/CurrencyReferenceRates/query", json=valid_request
        )
    assert no_grant.status_code == 403
    assert session.calls == []

    with _client(registry, ctx=_context(), allowed_domain=False) as api:
        denied = api.post("/providers/ecb/models/CurrencyReferenceRates/query", json=valid_request)
    assert denied.status_code == 403
    assert session.calls == []

    with _client(registry, ctx=_context()) as api:
        invalid = api.post(
            "/providers/ecb/models/CurrencyReferenceRates/query",
            json={"query": {"quote_currencies": ["USD"], "unknown": True}},
        )
        unknown_model = api.post(
            "/providers/ecb/models/CurrencyHistorical/query",
            json=valid_request,
        )

    assert invalid.status_code == 400
    assert unknown_model.status_code == 404
    assert session.calls == []
