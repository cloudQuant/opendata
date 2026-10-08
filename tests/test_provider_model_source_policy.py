"""Offline tests for request-scoped provider source-policy API wiring."""

from __future__ import annotations

import json
import os
import socket
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from opendata.api import (
    provider_model_context,
    provider_model_export,
    provider_model_ingest,
    provider_model_query,
    provider_model_warehouse,
    provider_models,
)
from opendata.api.dependencies import get_current_principal
from opendata.core.config import Settings
from opendata.data.http_client import GovernedHttpClient
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.providers.fred.models import _sofr_client
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import GrantDecision, RequestOperation
from opendata.services.provider_model_export_request import (
    ProviderModelExportRequestUnavailableError,
)

_POLICY_UNAVAILABLE = "Provider source policy is unavailable"
_USER_ID = 17
_API_KEY_ID = 991
_SOFR_HOST = "api.stlouisfed.org"
_SENTINEL = "synthetic-private-key-sentinel"

if TYPE_CHECKING:
    from pathlib import Path

    from opendata.data.source_policy import SourcePolicyDocument


@pytest.fixture(autouse=True)
def block_socket_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a recording transport accidentally reaches a socket."""

    def reject_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access is blocked in source-policy API tests")

    monkeypatch.setattr(socket.socket, "connect", reject_connect)
    monkeypatch.setattr(socket, "create_connection", reject_connect)


def _policy(
    *,
    source: str = "fred",
    model: str = "SOFR",
    operation: RequestOperation = RequestOperation.QUERY,
    principal_user_id: int = _USER_ID,
    decision: GrantDecision = GrantDecision.ALLOWED,
    expires_at: str = "2099-01-01T00:00:00Z",
    conditions: tuple[str, ...] = (),
    task_attempts: int = 10,
    source_attempts: int = 3,
    policy_id: str = "synthetic-policy-01",
) -> dict[str, object]:
    """Return one synthetic policy declaration for offline request tests."""
    return {
        "policy_id": policy_id,
        "source": source,
        "canonical_model": model,
        "operation": operation.value,
        "principal_user_id": principal_user_id,
        "product": "offline API integration",
        "purpose": "exercise request scoped policy wiring",
        "decision": decision.value,
        "rights_evidence": f"synthetic-reference-{policy_id}",
        "allowed_hosts": [_SOFR_HOST] if decision is GrantDecision.ALLOWED else [],
        "expires_at": expires_at,
        "conditions": list(conditions),
        "task_attempts": task_attempts,
        "source_attempts": source_attempts,
    }


def _write_policy(path: Path, *policies: dict[str, object]) -> bytes:
    body = json.dumps(
        {"version": 1, "policies": list(policies)},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(body)
    return body


def _settings_for(monkeypatch: pytest.MonkeyPatch, policy_path: Path | None) -> None:
    settings = SimpleNamespace(provider_source_policy_file=policy_path)
    monkeypatch.setattr(provider_model_context, "get_settings", lambda: settings)


def _principal(
    user_id: object = _USER_ID,
    *,
    allowed_domains: tuple[str, ...] = ("sofr", "fred_series"),
    include_user: bool = True,
) -> SimpleNamespace:
    """Represent the JWT user or API-key owner used by authenticated routes."""
    values: dict[str, object] = {
        "allows_domain": lambda domain: domain in allowed_domains,
        "api_key_id": _API_KEY_ID,
        "scopes": None,
    }
    if include_user:
        values["user"] = SimpleNamespace(id=user_id)
    return SimpleNamespace(**values)


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    register_provider("fred", registry)
    return registry


def _settings_without_dotenv() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _client(
    registry: ProviderRegistry,
    principal: object,
    *,
    all_routes: bool = False,
) -> TestClient:
    app = FastAPI()
    selected_router = provider_models.router if all_routes else provider_model_query.router
    app.include_router(selected_router, prefix="/providers")
    app.dependency_overrides[get_current_principal] = lambda: principal
    for dependency in (
        provider_model_query.get_provider_model_query_registry,
        provider_model_ingest.get_provider_model_ingest_registry,
        provider_model_warehouse.get_provider_model_warehouse_registry,
        provider_model_export.get_provider_model_export_registry,
    ):
        app.dependency_overrides[dependency] = lambda: registry
    return TestClient(app)


def _row(day: str, value: str) -> dict[str, str]:
    return {
        "date": day,
        "value": value,
        "realtime_start": day,
        "realtime_end": "9999-12-31",
    }


def _sofr_page() -> dict[str, object]:
    return {
        "count": 1,
        "offset": 0,
        "limit": 1,
        "units": "lin",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "2025-01-02",
        "observation_end": "2025-01-02",
        "observations": [_row("2025-01-02", "4.2500")],
    }


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(
            {"method": method, "url": url, "params": dict(kwargs.get("params") or {})}
        )
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(_sofr_page(), separators=(",", ":")).encode("utf-8")
        response.headers["Content-Type"] = "application/json"
        response.url = url
        return response


def _install_recording_transport(monkeypatch: pytest.MonkeyPatch) -> _RecordingSession:
    session = _RecordingSession()
    client = GovernedHttpClient(session=cast("requests.Session", session), raw_response_cache=None)
    monkeypatch.setattr(_sofr_client, "get_shared_http_client", lambda: client)
    monkeypatch.setattr(_sofr_client._client, "require_api_key", lambda: "synthetic-fixture-key")
    return session


def _sofr_query() -> dict[str, object]:
    return {
        "series_id": "SOFR",
        "start_date": "2025-01-02",
        "end_date": "2025-01-02",
        "page_size": 1,
        "max_records": 1,
        "max_pages": 1,
    }


def test_source_policy_setting_is_optional_empty_or_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("PROVIDER_SOURCE_POLICY_FILE", raising=False)
    assert _settings_without_dotenv().provider_source_policy_file is None

    monkeypatch.setenv("PROVIDER_SOURCE_POLICY_FILE", "")
    assert _settings_without_dotenv().provider_source_policy_file is None

    policy_path = tmp_path / "policy.json"
    monkeypatch.setenv("PROVIDER_SOURCE_POLICY_FILE", str(policy_path))
    assert _settings_without_dotenv().provider_source_policy_file == policy_path

    monkeypatch.setenv("PROVIDER_SOURCE_POLICY_FILE", "relative/policy.json")
    with pytest.raises(ValidationError):
        _settings_without_dotenv()


def test_missing_configuration_keeps_zero_grant_and_does_not_require_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_for(monkeypatch, None)
    session = _install_recording_transport(monkeypatch)
    principal = _principal(include_user=False)

    with _client(_registry(), principal) as client:
        response = client.post(
            "/providers/fred/models/SOFR/query",
            json={"query": _sofr_query()},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Provider model query is not authorized"
    assert session.calls == []


def test_synthetic_policy_runs_registered_sofr_through_governed_recording_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    with _client(_registry(), _principal()) as client:
        response = client.post(
            "/providers/fred/models/SOFR/query",
            json={"query": _sofr_query()},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["source"], data["model"], data["domain"]) == ("fred", "SOFR", "sofr")
    assert data["results"][0]["source_value"] == "4.2500"
    assert len(session.calls) == 1
    assert session.calls[0]["method"] == "GET"
    assert str(session.calls[0]["url"]).startswith(f"https://{_SOFR_HOST}/")
    params = session.calls[0]["params"]
    assert isinstance(params, dict)
    assert params["series_id"] == "SOFR"


@pytest.mark.parametrize(
    "policy_overrides",
    (
        {"principal_user_id": 18},
        {"source": "bls"},
        {"model": "FredSeries"},
        {"operation": RequestOperation.STORE},
        {"decision": GrantDecision.DENIED},
        {"decision": GrantDecision.UNKNOWN},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"conditions": ("operator review is still required",)},
    ),
    ids=(
        "wrong-user",
        "wrong-source",
        "wrong-model",
        "wrong-operation",
        "denied",
        "unknown",
        "expired",
        "conditional",
    ),
)
def test_nonmatching_or_nonusable_policy_denies_before_http(
    policy_overrides: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy(**cast("Any", policy_overrides)))
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    with _client(_registry(), _principal()) as client:
        response = client.post(
            "/providers/fred/models/SOFR/query",
            json={"query": _sofr_query()},
        )

    assert response.status_code == 403
    assert session.calls == []


def test_missing_or_nonexact_principal_owner_id_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    for principal in (_principal(include_user=False), _principal(True), _principal(0)):
        with _client(_registry(), principal) as client:
            response = client.post(
                "/providers/fred/models/SOFR/query",
                json={"query": _sofr_query()},
            )
        assert response.status_code == 403
    assert session.calls == []


def test_request_contexts_use_exact_route_operation_and_owner_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(
        policy_path,
        _policy(model="FredSeries", operation=RequestOperation.QUERY, policy_id="query"),
        _policy(model="FredSeries", operation=RequestOperation.STORE, policy_id="store"),
        _policy(model="FredSeries", operation=RequestOperation.EXPORT, policy_id="export"),
    )
    _settings_for(monkeypatch, policy_path)
    principal = _principal()

    query_context = provider_model_query.get_provider_model_query_context(
        "fred", "FredSeries", current_principal=principal
    )
    warehouse_context = provider_model_warehouse.get_provider_model_warehouse_context(
        "fred", "FredSeries", current_principal=principal
    )
    ingest_context = provider_model_ingest.get_provider_model_ingest_context(
        "fred", "FredSeries", current_principal=principal
    )
    export_context = provider_model_export.get_provider_model_export_context(
        "fred", "FredSeries", current_principal=principal
    )

    assert query_context is not None and query_context.operation is RequestOperation.QUERY
    assert warehouse_context is not None and warehouse_context.operation is RequestOperation.QUERY
    assert ingest_context is not None and ingest_context.operation is RequestOperation.STORE
    assert export_context is not None and export_context.operation is RequestOperation.EXPORT
    budgets = (
        query_context.request_budget,
        warehouse_context.request_budget,
        ingest_context.request_budget,
        export_context.request_budget,
    )
    assert all(budget is not None for budget in budgets)
    assert len({id(budget) for budget in budgets}) == len(budgets)
    assert all(budget.task_attempt_limit <= 10 for budget in budgets if budget is not None)
    assert all(budget.source_attempt_limit <= 3 for budget in budgets if budget is not None)


def test_each_query_request_gets_a_fresh_bounded_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)
    built_contexts: list[FetchContext] = []
    build = provider_model_context.build_source_policy_context

    def capture_build(
        document: SourcePolicyDocument,
        *,
        source: str,
        canonical_model: str,
        operation: RequestOperation,
        principal_user_id: int,
        timeout: float = 30.0,
    ) -> FetchContext:
        context = build(
            document,
            source=source,
            canonical_model=canonical_model,
            operation=operation,
            principal_user_id=principal_user_id,
            timeout=timeout,
        )
        built_contexts.append(context)
        return context

    monkeypatch.setattr(provider_model_context, "build_source_policy_context", capture_build)
    with _client(_registry(), _principal()) as client:
        first = client.post("/providers/fred/models/SOFR/query", json={"query": _sofr_query()})
        second = client.post("/providers/fred/models/SOFR/query", json={"query": _sofr_query()})

    assert first.status_code == second.status_code == 200
    assert len(session.calls) == 2
    assert len(built_contexts) == 2
    first_budget = built_contexts[0].request_budget
    second_budget = built_contexts[1].request_budget
    assert first_budget is not None and second_budget is not None
    assert first_budget is not second_budget
    assert first_budget.task_attempt_limit <= 10 and second_budget.task_attempt_limit <= 10
    assert first_budget.source_attempt_limit <= 3 and second_budget.source_attempt_limit <= 3
    assert first_budget.attempts_used_for("fred") == 1
    assert second_budget.attempts_used_for("fred") == 1


def test_atomic_policy_replacement_revokes_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)
    replacement = tmp_path / "replacement.json"
    _write_policy(replacement, _policy(expires_at="2000-01-01T00:00:00Z"))

    with _client(_registry(), _principal()) as client:
        admitted = client.post("/providers/fred/models/SOFR/query", json={"query": _sofr_query()})
        os.replace(replacement, policy_path)
        revoked = client.post("/providers/fred/models/SOFR/query", json={"query": _sofr_query()})

    assert admitted.status_code == 200
    assert revoked.status_code == 403
    assert len(session.calls) == 1


def test_policy_loader_allows_symlink_to_regular_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "policy-target.json"
    _write_policy(target, _policy())
    link = tmp_path / "policy-link.json"
    link.symlink_to(target)
    _settings_for(monkeypatch, link)

    context = provider_model_context.get_provider_model_context(
        source="fred",
        model="SOFR",
        operation=RequestOperation.QUERY,
        principal=_principal(),
    )

    assert context is not None
    assert context.request_budget is not None
    assert context.request_budget.grants[0].decision is GrantDecision.ALLOWED


@pytest.mark.parametrize(
    "bad_kind", ("missing", "directory", "duplicate", "schema", "utf8", "oversize", "depth")
)
def test_bad_policy_files_return_fixed_503_without_reflecting_contents(
    bad_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / f"{_SENTINEL}-policy.json"
    if bad_kind == "missing":
        path = secret_path
    elif bad_kind == "directory":
        secret_path.mkdir()
        path = secret_path
    else:
        path = secret_path
        if bad_kind == "duplicate":
            path.write_bytes(b'{"version":1,"version":1,"policies":[]}')
        elif bad_kind == "schema":
            malformed = _policy()
            malformed["rights_evidence"] = _SENTINEL
            malformed["unexpected"] = _SENTINEL
            _write_policy(path, malformed)
        elif bad_kind == "utf8":
            path.write_bytes(b"\xff")
        elif bad_kind == "oversize":
            path.write_bytes(b" " * (1024 * 1024 + 1))
        else:
            path.write_bytes(b'{"version":1,"policies":[],"x":' + b"[" * 65 + b"]" * 65 + b"}")

    _settings_for(monkeypatch, path)
    session = _install_recording_transport(monkeypatch)
    with _client(_registry(), _principal()) as client:
        response = client.post(
            "/providers/fred/models/SOFR/query",
            json={"query": _sofr_query()},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == _POLICY_UNAVAILABLE
    assert str(secret_path) not in response.text
    assert _SENTINEL not in response.text
    assert session.calls == []


def test_fifo_policy_is_rejected_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fifo_path = tmp_path / "source-policy.fifo"
    os.mkfifo(fifo_path)
    _settings_for(monkeypatch, fifo_path)

    with pytest.raises(HTTPException) as error:
        provider_model_context.get_provider_model_context(
            source="fred",
            model="SOFR",
            operation=RequestOperation.QUERY,
            principal=_principal(),
        )

    assert error.value.status_code == 503
    assert error.value.detail == _POLICY_UNAVAILABLE


def test_existing_domain_acl_still_denies_allowed_source_policy_before_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    with _client(_registry(), _principal(allowed_domains=())) as client:
        response = client.post(
            "/providers/fred/models/SOFR/query",
            json={"query": _sofr_query()},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Not authorized for this provider model"
    assert session.calls == []


@pytest.mark.parametrize(
    "payload",
    (
        {"query": {"series_id": "SOFR"}, "provider_source_policy_file": "/tmp/other.json"},
        {"query": {"series_id": "SOFR", "ctx": {"operation": "query"}}},
        {"query": {"series_id": "SOFR", "request_budget": {"grants": []}}},
        {"query": {"series_id": "SOFR", "decision": "ALLOWED"}},
        {"query": {"series_id": "SOFR", "rights_evidence": _SENTINEL}},
        {"query": {"series_id": "SOFR", "allowed_hosts": [_SOFR_HOST]}},
    ),
)
def test_http_cannot_supply_policy_or_execution_controls(
    payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    with _client(_registry(), _principal()) as client:
        response = client.post("/providers/fred/models/SOFR/query", json=payload)

    assert response.status_code == 400
    assert _SENTINEL not in response.text
    assert session.calls == []


def test_invalid_route_identity_keeps_service_400_and_404_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(policy_path, _policy())
    _settings_for(monkeypatch, policy_path)
    session = _install_recording_transport(monkeypatch)

    with _client(_registry(), _principal()) as client:
        malformed = client.post(
            "/providers/auto/models/SOFR/query",
            json={"query": _sofr_query()},
        )
        unknown = client.post(
            "/providers/fred/models/sofr/query",
            json={"query": _sofr_query()},
        )

    assert malformed.status_code == 400
    assert unknown.status_code == 404
    assert session.calls == []


def test_ingest_warehouse_and_export_routes_receive_fresh_operation_contexts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    _write_policy(
        policy_path,
        _policy(model="FredSeries", operation=RequestOperation.QUERY, policy_id="series-query"),
        _policy(model="FredSeries", operation=RequestOperation.STORE, policy_id="series-store"),
        _policy(model="FredSeries", operation=RequestOperation.EXPORT, policy_id="series-export"),
    )
    _settings_for(monkeypatch, policy_path)
    registry = _registry()
    engine_stub = object()
    factory_calls: list[str] = []
    observed: dict[str, FetchContext] = {}

    def ingest_engine_factory() -> object:
        factory_calls.append("ingest")
        return engine_stub

    def warehouse_engine_factory() -> object:
        factory_calls.append("warehouse")
        return engine_stub

    def export_engine_factory() -> object:
        factory_calls.append("export")
        return engine_stub

    monkeypatch.setattr(provider_model_ingest, "ingest_engine_factory", ingest_engine_factory)
    monkeypatch.setattr(
        provider_model_warehouse,
        "warehouse_engine_factory",
        warehouse_engine_factory,
    )
    monkeypatch.setattr(provider_model_export, "export_engine_factory", export_engine_factory)

    async def capture_ingest(**kwargs: object) -> dict[str, object]:
        context = kwargs["ctx"]
        assert isinstance(context, FetchContext)
        observed["ingest"] = context
        engine_factory = kwargs["engine_factory"]
        assert callable(engine_factory)
        assert engine_factory() is engine_stub
        return {"stored": True}

    async def capture_warehouse(**kwargs: object) -> dict[str, object]:
        context = kwargs["ctx"]
        assert isinstance(context, FetchContext)
        observed["warehouse"] = context
        engine_factory = kwargs["engine_factory"]
        assert callable(engine_factory)
        assert engine_factory() is engine_stub
        return {"read": True}

    async def capture_export(**kwargs: object) -> None:
        context = kwargs["ctx"]
        assert isinstance(context, FetchContext)
        observed["export"] = context
        engine_factory = kwargs["engine_factory"]
        assert callable(engine_factory)
        assert engine_factory() is engine_stub
        raise ProviderModelExportRequestUnavailableError

    monkeypatch.setattr(provider_model_ingest, "ingest_provider_model", capture_ingest)
    monkeypatch.setattr(
        provider_model_warehouse,
        "query_provider_model_warehouse",
        capture_warehouse,
    )
    monkeypatch.setattr(
        provider_model_export.provider_model_export_request,
        "export_provider_model_request",
        capture_export,
    )

    with _client(registry, _principal(), all_routes=True) as client:
        ingest_response = client.post(
            "/providers/fred/models/FredSeries/ingest",
            json={"query": {"series_id": "SOFR"}},
        )
        warehouse_response = client.post(
            "/providers/fred/models/FredSeries/warehouse/query",
            json={"query": {"limit": 1}},
        )
        export_response = client.post(
            "/providers/fred/models/FredSeries/warehouse/export",
            json={"query": {}},
        )

    assert ingest_response.status_code == 200
    assert warehouse_response.status_code == 200
    assert export_response.status_code == 503
    assert observed["ingest"].operation is RequestOperation.STORE
    assert observed["warehouse"].operation is RequestOperation.QUERY
    assert observed["export"].operation is RequestOperation.EXPORT
    assert factory_calls == ["ingest", "warehouse", "export"]
