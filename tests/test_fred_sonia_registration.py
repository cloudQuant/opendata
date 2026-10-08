"""Offline registration and governed API contracts for canonical SONIA."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_query import (
    get_provider_model_query_context,
    get_provider_model_query_registry,
)
from opendata.api.provider_models import router
from opendata.data.domains import contract_model, require_domain_semantics
from opendata.data.http_client import GovernedHttpClient
from opendata.data.models import FredSoniaObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import get_provider, register_provider
from opendata.data.providers.fred.models import _client, _sonia_client
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_export, provider_model_store, provider_model_warehouse

_SELECTORS = (
    ("rate", "IUDSOIA", "Percent"),
    ("index", "IUDZOS2", "Index"),
    ("10th_percentile", "IUDZLS6", "Percent"),
    ("25th_percentile", "IUDZLS7", "Percent"),
    ("75th_percentile", "IUDZLS8", "Percent"),
    ("90th_percentile", "IUDZLS9", "Percent"),
    ("total_nominal_value", "IUDZLT2", "Millions of Pounds"),
)
_FIELDS = (
    "series_id",
    "date",
    "value",
    "realtime_start",
    "realtime_end",
    "transform_units",
    "output_type",
    "requested_frequency",
    "requested_aggregation_method",
    "parameter",
    "source_value",
    "native_units",
)
_DOMAIN_FIELDS = (
    "series_id",
    "parameter",
    "date",
    "realtime_start",
    "realtime_end",
    "transform_units",
    "output_type",
    "requested_frequency",
    "requested_aggregation_method",
)
_HOST = "api.stlouisfed.org"
_SCENARIO = "保留SONIA七测度、原始数值与修订区间的英镑融资成本研究"


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fred", registry)
    return registry


def _context(operation: RequestOperation = RequestOperation.QUERY) -> FetchContext:
    grant = RequestGrant(
        source="fred",
        canonical_model="SONIA",
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-only synthetic SONIA registration grant",
        task_attempts=10,
        source_attempts=3,
        allowed_hosts={_HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(task_attempts=10, source_attempts=3, grants=(grant,)),
        operation=operation,
    )


def _api_client(
    registry: ProviderRegistry,
    *,
    allowed: bool = True,
    ctx: FetchContext | None = None,
    inject_context: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(allows_domain=lambda domain: allowed and domain == "sonia")
    app.dependency_overrides[get_provider_model_query_registry] = lambda: registry
    app.dependency_overrides[get_current_principal] = lambda: principal
    if inject_context:
        app.dependency_overrides[get_provider_model_query_context] = lambda: ctx
    return TestClient(app)


def _query_path(source: str = "fred", model: str = "SONIA") -> str:
    return f"/providers/{source}/models/{model}/query"


def _query(*, parameter: str | None = None, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "start_date": "2025-01-02",
        "end_date": "2025-01-03",
        "page_size": 1,
        "max_records": 2,
        "max_pages": 2,
    }
    if parameter is not None:
        values["parameter"] = parameter
    values.update(overrides)
    return values


def _row(day: str, value: str) -> dict[str, str]:
    return {
        "date": day,
        "value": value,
        "realtime_start": day,
        "realtime_end": "9999-12-31",
    }


def _page(series_id: str, offset: int) -> dict[str, Any]:
    day = "2025-01-02" if offset == 0 else "2025-01-03"
    return {
        "count": 2,
        "offset": offset,
        "limit": 1,
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "2025-01-02",
        "observation_end": "2025-01-03",
        "series_id": series_id,
        "observations": [_row(day, "1.2500" if offset == 0 else "2.5")],
        "fixture_page_marker": f"{series_id}-page-{offset}",
    }


class _RecordingSession(requests.Session):
    def __init__(self, pages: tuple[dict[str, Any], ...]) -> None:
        super().__init__()
        self.pages = {(page["series_id"], page["offset"]): page for page in pages}
        self.calls: list[dict[str, object]] = []
        self.thread_ids: list[int] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append({"method": method, "url": url, "params": params})
        self.thread_ids.append(threading.get_ident())
        page = self.pages[(params["series_id"], int(params["offset"]))]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(page, separators=(",", ":")).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.url = url
        return response


def _install_recording_transport(
    monkeypatch: pytest.MonkeyPatch,
    pages: tuple[dict[str, Any], ...],
) -> _RecordingSession:
    import opendata.data.http_client as http_client_module

    session = _RecordingSession(pages)
    client = GovernedHttpClient(session=session, raw_response_cache=None)
    monkeypatch.setattr(http_client_module, "get_configured_raw_response_cache", lambda: None)
    monkeypatch.setattr(_sonia_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(_client, "require_api_key", lambda: "fixture-only-key")
    return session


@pytest.fixture(autouse=True)
def block_socket_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("SONIA registration test attempted a socket connection")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)


def test_binding_and_query_only_domain_are_exact() -> None:
    provider = get_provider("fred")
    binding = provider.fetcher_dict["SONIA"]
    registry = _registry()
    fetcher = registry.resolve_model("fred", "SONIA")
    descriptor = next(
        item
        for item in registry.list_model_descriptors()
        if (item.source, item.model) == ("fred", "SONIA")
    )
    spec = require_domain_semantics("sonia")

    assert binding.module == "opendata.data.providers.fred.models.sonia"
    assert binding.class_name == "FredSoniaFetcher"
    assert binding.canonical_model_ids == ("SONIA",)
    assert binding.scenario == _SCENARIO
    assert type(fetcher).__name__ == "FredSoniaFetcher"
    assert fetcher.canonical_model == "SONIA"
    assert fetcher.async_mode == "bounded_thread"
    assert fetcher.capability.model_dump() == {
        "asset_class": "macro",
        "domain": "sonia",
        "period": "variable",
        "market": "gb",
        "source": "fred",
        "verified": False,
        "notes": "",
    }
    assert descriptor.verified is False
    assert contract_model("sonia") is FredSoniaObservation
    assert (spec.rest_path, spec.contract) == ("macro/sonia", "FredSoniaObservation")
    assert spec.temporal_kind == "series"
    assert spec.time_field == "date"
    assert spec.natural_key == spec.filter_dims == _DOMAIN_FIELDS
    assert spec.storage_mode == "transient"
    assert spec.permissions == ("query",)
    assert spec.priority is None
    assert tuple(FredSoniaObservation.model_fields) == _FIELDS


@pytest.mark.parametrize(("parameter", "series_id", "native_units"), _SELECTORS)
def test_registered_api_queries_each_selector_with_typed_identity(
    parameter: str,
    series_id: str,
    native_units: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = (_page(series_id, 0), _page(series_id, 1))
    session = _install_recording_transport(monkeypatch, pages)
    registry = _registry()
    body = _query(parameter=None if parameter == "rate" else parameter)

    with _api_client(registry, ctx=_context()) as client:
        response = client.post(_query_path(), json={"query": body})

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert (payload["source"], payload["model"], payload["domain"]) == (
        "fred",
        "SONIA",
        "sonia",
    )
    assert payload["verified"] is False
    assert [tuple(row) for row in payload["results"]] == [_FIELDS, _FIELDS]
    assert [row["series_id"] for row in payload["results"]] == [series_id, series_id]
    assert [row["parameter"] for row in payload["results"]] == [parameter, parameter]
    assert [row["native_units"] for row in payload["results"]] == [native_units, native_units]
    assert [row["source_value"] for row in payload["results"]] == ["1.2500", "2.5"]
    assert [row["value"] for row in payload["results"]] == [1.25, 2.5]
    assert len(session.calls) == 2
    assert [call["params"]["series_id"] for call in session.calls] == [series_id, series_id]
    assert [call["params"]["offset"] for call in session.calls] == ["0", "1"]
    assert all(call["method"] == "GET" for call in session.calls)
    assert all(call["url"].startswith("https://api.stlouisfed.org/") for call in session.calls)


@pytest.mark.parametrize(
    "query",
    [
        {"parameter": "unsupported"},
        {"parameter": "index", "series_id": "IUDSOIA"},
        {"parameter": "rate", "output_type": True},
        {"parameter": "rate", "start_date": "2025-01-02T00:00:00"},
        {"parameter": "rate", "unexpected": "value"},
    ],
    ids=(
        "unknown-selector",
        "selector-series-conflict",
        "bool-output-type",
        "datetime-date",
        "extra-field",
    ),
)
def test_invalid_registered_queries_are_rejected_before_any_send(
    query: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _install_recording_transport(monkeypatch, (_page("IUDSOIA", 0),))
    registry = _registry()

    with _api_client(registry, ctx=_context()) as client:
        response = client.post(_query_path(), json={"query": query})

    assert response.status_code == 400
    assert session.calls == []


def test_no_grant_and_domain_acl_both_deny_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_recording_transport(monkeypatch, (_page("IUDSOIA", 0),))
    registry = _registry()

    with _api_client(registry, inject_context=False) as client:
        no_grant = client.post(_query_path(), json={"query": _query()})
    with _api_client(registry, allowed=False, ctx=_context()) as client:
        acl_denied = client.post(_query_path(), json={"query": _query()})

    assert no_grant.status_code == acl_denied.status_code == 403
    assert session.calls == []


def test_bounded_async_raw_capture_and_two_page_output_preserve_source_envelopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = (_page("IUDSOIA", 0), _page("IUDSOIA", 1))
    session = _install_recording_transport(monkeypatch, pages)
    fetcher = _registry().resolve_model("fred", "SONIA")
    main_thread = threading.get_ident()

    async def run() -> tuple[Any, Any, Any]:
        result = await fetcher.fetch_async(ctx=_context(), **_query())
        raw = await fetcher.fetch_raw_async(ctx=_context(), **_query())
        captured = await fetcher.fetch_captured_async(
            ctx=_context(), capture=lambda source_pages, _query_model: source_pages, **_query()
        )
        return result, raw, captured

    result, raw, captured = asyncio.run(run())

    assert [item.source_value for item in result] == ["1.2500", "2.5"]
    assert raw == pages
    assert captured.capture == pages
    assert [page["fixture_page_marker"] for page in captured.capture] == [
        "IUDSOIA-page-0",
        "IUDSOIA-page-1",
    ]
    assert len(session.calls) == 6
    assert all(thread_id != main_thread for thread_id in session.thread_ids)
    assert all(call["params"]["api_key"] == "fixture-only-key" for call in session.calls)


def test_unregistered_case_or_source_does_not_fallback_to_sonia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _install_recording_transport(monkeypatch, (_page("IUDSOIA", 0),))
    registry = _registry()

    with _api_client(registry, ctx=_context()) as client:
        lowercase_model = client.post(_query_path(model="sonia"), json={"query": _query()})
        wrong_provider = client.post(_query_path(source="fmp"), json={"query": _query()})
        query_source_mismatch = client.post(
            _query_path(), json={"query": {**_query(), "source": "fmp"}}
        )

    assert lowercase_model.status_code == 404
    assert wrong_provider.status_code == 404
    assert query_source_mismatch.status_code == 400
    assert session.calls == []


def test_transient_sonia_is_rejected_by_store_warehouse_and_export_before_engine() -> None:
    registry = _registry()
    engine_calls: list[str] = []

    def engine_factory() -> object:
        engine_calls.append("called")
        raise AssertionError("transient SONIA reached a local database engine")

    with pytest.raises(provider_model_store.ProviderModelStoreError):
        provider_model_store.write_provider_model_rows(
            engine=object(),
            registry=registry,
            source="fred",
            model="SONIA",
            rows=(),
            observed_at=datetime.now(timezone.utc),
            ctx=_context(RequestOperation.STORE),
        )

    with pytest.raises(provider_model_warehouse.ProviderModelWarehouseNotFoundError):
        asyncio.run(
            provider_model_warehouse.query_provider_model_warehouse(
                registry=registry,
                source="fred",
                model="SONIA",
                query={},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=engine_factory,
                ctx=_context(RequestOperation.QUERY),
            )
        )

    with pytest.raises(provider_model_export.ProviderModelExportValidationError):
        provider_model_export.export_provider_model_snapshot(
            engine_factory=engine_factory,
            registry=registry,
            source="fred",
            model="SONIA",
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            ctx=_context(RequestOperation.EXPORT),
        )

    assert engine_calls == []
