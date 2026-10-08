"""Offline SDK tests for source-native provider-model ingestion."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
import pytest

from opendata_client import (
    AuthenticationError,
    InvalidQueryError,
    NotFoundError,
    OpendataClient,
    OpendataClientError,
    PermissionDeniedError,
    UnsupportedQueryError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_API_KEY = "od-ingest-sdk-test-key"
_SECRET = "INGEST_SDK_SECRET_SENTINEL"
_UUID4 = "123e4567-e89b-42d3-a456-426614174000"
_OBSERVED_AT = "2026-10-08T02:00:00+00:00"


def _ingest_data(
    source: str,
    model: str,
    domain: str,
    *,
    raw_rows: int = 1,
    stored_rows: int = 1,
) -> dict[str, Any]:
    return {
        "source": source,
        "model": model,
        "domain": domain,
        "verified": False,
        "batch_id": _UUID4,
        "observed_at": _OBSERVED_AT,
        "raw_rows": raw_rows,
        "stored_rows": stored_rows,
        "raw_scope": "extract_data_output",
        "completeness": "NOT_ASSESSED",
        "transaction_scope": "ods_and_dwd_single_transaction",
        "receipt_metadata": {"retained": True, "nested": {"version": 1}},
    }


def _envelope(
    data: Any,
    *,
    success: bool = True,
    message: str = "success",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": success, "message": message, "data": data},
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpendataClient:
    return OpendataClient(
        "http://api.test",
        api_key=_API_KEY,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    ("source", "model", "domain", "query", "raw_rows", "stored_rows"),
    [
        ("fred", "FredSeries", "fred_series", {"series_id": "GDP"}, 0, 0),
        (
            "bls",
            "BlsSeries",
            "bls_series",
            {
                "series_ids": ["LNS14000000"],
                "start_year": 2025,
                "end_year": 2025,
                "max_requests": 2,
            },
            1,
            1,
        ),
        (
            "fmp",
            "EquityHistorical",
            "equity_historical",
            {
                "symbol": "BRK.B",
                "start_date": "2024-01-01",
                "end_date": "2024-01-03",
            },
            1,
            1,
        ),
    ],
    ids=["fred-zero-rows", "bls-series", "fmp-equity-historical"],
)
def test_posts_exact_ingest_query_and_preserves_complete_receipt(
    source: str,
    model: str,
    domain: str,
    query: dict[str, Any],
    raw_rows: int,
    stored_rows: int,
) -> None:
    original_query = copy.deepcopy(query)
    data = _ingest_data(
        source,
        model,
        domain,
        raw_rows=raw_rows,
        stored_rows=stored_rows,
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(data)

    with _client(handler) as client:
        result = client.ingest_provider_model(source, model, query)

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == f"/api/v1/providers/{source}/models/{model}/ingest"
    assert request.headers["x-api-key"] == _API_KEY
    assert request.headers["content-type"] == "application/json"
    assert request.content == json.dumps(
        {"query": query},
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert json.loads(request.content) == {"query": query}
    assert query == original_query
    assert result == data
    assert result["verified"] is False
    assert result["raw_rows"] == raw_rows
    assert result["stored_rows"] == stored_rows
    assert result["raw_scope"] == "extract_data_output"
    assert result["completeness"] == "NOT_ASSESSED"
    assert result["transaction_scope"] == "ods_and_dwd_single_transaction"
    assert result["receipt_metadata"] == {"retained": True, "nested": {"version": 1}}


@pytest.mark.parametrize(
    ("source", "model"),
    [
        ("auto", "FredSeries"),
        ("fred", "auto"),
        ("../fred", "FredSeries"),
        ("fred", "FredSeries?x=1"),
        (None, "FredSeries"),
        ("fred", 7),
    ],
)
def test_invalid_identity_is_rejected_before_transport(source: Any, model: Any) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(_ingest_data("fred", "FredSeries", "fred_series"))

    with (
        _client(handler) as client,
        pytest.raises(ValueError, match="valid provider/model identifier") as error,
    ):
        client.ingest_provider_model(source, model, {})

    assert seen == []
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_json_query_is_rejected_before_transport_without_mutation() -> None:
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(257):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child
    queries: list[Any] = [
        None,
        [],
        {"value": float("nan")},
        {"value": float("inf")},
        {1: "numeric key"},
        {"nested": [{"bad": {False: "boolean key"}}]},
        {"nested": object()},
        cyclic,
        deep,
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _envelope(_ingest_data("fred", "FredSeries", "fred_series"))

    with _client(handler) as client:
        for query in queries:
            with pytest.raises(ValueError) as error:
                client.ingest_provider_model("fred", "FredSeries", query)
            assert error.value.__cause__ is None
            assert error.value.__context__ is None

    assert seen == []
    assert cyclic["self"] is cyclic
    assert len(deep) == 1


def _invalid_receipt_cases() -> list[tuple[str, Any]]:
    base = _ingest_data("fred", "FredSeries", "fred_series")
    cases: list[tuple[str, Any]] = [("list-data", [])]

    def with_data(name: str, data: dict[str, Any]) -> None:
        cases.append((name, data))

    with_data("source-mismatch", {**base, "source": "bls"})
    with_data("model-mismatch", {**base, "model": "BlsSeries"})
    with_data("missing-domain", {key: value for key, value in base.items() if key != "domain"})
    with_data("empty-domain", {**base, "domain": ""})
    with_data("nonboolean-verified", {**base, "verified": 1})
    with_data("missing-batch-id", {key: value for key, value in base.items() if key != "batch_id"})
    with_data("invalid-batch-id", {**base, "batch_id": "not-a-uuid"})
    with_data("non-v4-batch-id", {**base, "batch_id": "123e4567-e89b-12d3-a456-426614174000"})
    with_data("naive-observed-at", {**base, "observed_at": "2026-10-08T02:00:00"})
    with_data("non-utc-observed-at", {**base, "observed_at": "2026-10-08T02:00:00+01:00"})
    with_data("invalid-observed-at", {**base, "observed_at": "not-a-timestamp"})
    with_data("boolean-raw-count", {**base, "raw_rows": True})
    with_data("negative-raw-count", {**base, "raw_rows": -1})
    with_data("raw-count-over-ceiling", {**base, "raw_rows": 10_001})
    with_data("boolean-stored-count", {**base, "stored_rows": False})
    with_data("negative-stored-count", {**base, "stored_rows": -1})
    with_data("stored-count-over-ceiling", {**base, "stored_rows": 10_001})
    with_data("wrong-raw-scope", {**base, "raw_scope": "provider-response"})
    with_data("wrong-completeness", {**base, "completeness": "COMPLETE"})
    with_data("wrong-transaction-scope", {**base, "transaction_scope": "per-row"})
    return cases


@pytest.mark.parametrize(
    ("name", "data"),
    _invalid_receipt_cases(),
    ids=[name for name, _ in _invalid_receipt_cases()],
)
def test_malformed_ingest_receipt_fails_safely(name: str, data: Any) -> None:
    del name

    def handler(request: httpx.Request) -> httpx.Response:
        return _envelope(data, message=_SECRET)

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.ingest_provider_model("fred", "FredSeries", {"series_id": _SECRET})

    assert _SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "nonfinite",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_nonfinite_receipt_values_are_rejected_without_secret_leak(nonfinite: float) -> None:
    data = _ingest_data("fred", "FredSeries", "fred_series")
    data["receipt_metadata"] = {"secret": _SECRET, "bad": nonfinite}

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps(
            {"success": True, "message": "success", "data": data},
            allow_nan=True,
        ).encode("utf-8")
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json"},
            request=request,
        )

    with _client(handler) as client, pytest.raises(OpendataClientError) as error:
        client.ingest_provider_model("fred", "FredSeries", {"series_id": "GDP"})

    assert "invalid JSON values" in str(error.value)
    assert _SECRET not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, InvalidQueryError),
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (422, InvalidQueryError),
        (429, OpendataClientError),
        (501, UnsupportedQueryError),
        (503, OpendataClientError),
        (504, OpendataClientError),
    ],
)
def test_http_errors_keep_status_class_without_reflecting_secrets(
    status: int,
    error_type: type[OpendataClientError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"detail": f"{_SECRET} {_API_KEY}"},
            request=request,
        )

    with (
        _client(handler) as client,
        pytest.raises(error_type) as error,
    ):
        client.ingest_provider_model("fred", "FredSeries", {"series_id": _SECRET})

    assert type(error.value) is error_type
    assert f"HTTP {status}" in str(error.value)
    assert _SECRET not in str(error.value)
    assert _API_KEY not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_failed_envelope_transport_and_invalid_json_are_sanitized() -> None:
    handlers: list[Callable[[httpx.Request], httpx.Response]] = [
        lambda request: httpx.Response(
            200,
            json={"success": False, "message": _SECRET, "data": {}},
            request=request,
        ),
        lambda request: httpx.Response(
            200,
            content=f"invalid-json {_SECRET}".encode(),
            headers={"content-type": "application/json"},
            request=request,
        ),
    ]

    def failed_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"{_API_KEY} {_SECRET}", request=request)

    handlers.append(failed_transport)
    for handler in handlers:
        with _client(handler) as client, pytest.raises(OpendataClientError) as error:
            client.ingest_provider_model("fred", "FredSeries", {"series_id": _SECRET})
        assert _SECRET not in str(error.value)
        assert _API_KEY not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None


def test_sdk_ingest_roundtrips_through_actual_api_with_fake_capture_and_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from opendata.api import provider_model_ingest as ingest_api
    from opendata.api.dependencies import get_current_principal
    from opendata.api.provider_model_ingest import (
        get_provider_model_ingest_context,
        get_provider_model_ingest_registry,
    )
    from opendata.api.provider_models import router
    from opendata.data.protocol import FetchContext
    from opendata.data.providers.catalog import register_provider
    from opendata.data.registry import ProviderRegistry
    from opendata.data.request_budget import (
        GrantDecision,
        RequestBudget,
        RequestGrant,
        RequestOperation,
    )
    from opendata.services import provider_model_store

    registry = ProviderRegistry()
    register_provider("fred", registry)
    fetcher = registry.resolve_model("fred", "FredSeries")
    extraction_calls: list[dict[str, Any]] = []
    raw_row = {
        "date": "2024-01-01",
        "value": "3.25",
        "realtime_start": "2024-01-01",
        "realtime_end": "9999-12-31",
    }

    def fake_extract(params: Any, ctx: Any) -> list[dict[str, Any]]:
        extraction_calls.append({"params": params, "ctx": ctx})
        return [dict(raw_row)]

    monkeypatch.setattr(fetcher, "extract_data", fake_extract)
    engine = object()
    engine_calls: list[str] = []
    write_calls: list[dict[str, Any]] = []

    def engine_factory() -> object:
        engine_calls.append("called")
        return engine

    def fake_write_provider_model_batch(**kwargs: Any) -> int:
        write_calls.append(kwargs)
        return len(kwargs["rows"])

    monkeypatch.setattr(ingest_api, "ingest_engine_factory", engine_factory)
    monkeypatch.setattr(
        provider_model_store, "write_provider_model_batch", fake_write_provider_model_batch
    )

    grant = RequestGrant(
        source="fred",
        canonical_model="FredSeries",
        operation=RequestOperation.STORE,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-ingest-sdk-test-grant",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=("api.stlouisfed.org",),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    context = FetchContext(
        timeout=3.0,
        request_budget=RequestBudget(
            task_attempts=3,
            source_attempts=3,
            grants=(grant,),
        ),
        operation=RequestOperation.STORE,
    )
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = type("Principal", (), {"allows_domain": lambda self, _domain: True})()
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_provider_model_ingest_registry] = lambda: registry
    app.dependency_overrides[get_provider_model_ingest_context] = lambda: context

    api_client = TestClient(app)
    seen: list[httpx.Request] = []

    def in_process_api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path.startswith("/api/v1")
        api_response = api_client.post(
            request.url.path.removeprefix("/api/v1"),
            content=request.content,
            headers={"content-type": request.headers["content-type"]},
        )
        return httpx.Response(
            api_response.status_code,
            content=api_response.content,
            headers={"content-type": api_response.headers.get("content-type", "application/json")},
            request=request,
        )

    query = {"series_id": "GDP"}
    original_query = copy.deepcopy(query)
    with api_client, _client(in_process_api) as client:
        result = client.ingest_provider_model("fred", "FredSeries", query)

    assert len(seen) == 1
    assert seen[0].url.path == "/api/v1/providers/fred/models/FredSeries/ingest"
    assert json.loads(seen[0].content) == {"query": query}
    assert query == original_query
    assert result["source"] == "fred"
    assert result["model"] == "FredSeries"
    assert result["domain"] == "fred_series"
    assert result["verified"] is False
    assert result["raw_rows"] == result["stored_rows"] == 1
    assert result["raw_scope"] == "extract_data_output"
    assert result["completeness"] == "NOT_ASSESSED"
    assert result["transaction_scope"] == "ods_and_dwd_single_transaction"
    assert result["batch_id"] == str(UUID(result["batch_id"], version=4))
    observed_at = datetime.fromisoformat(result["observed_at"])
    assert observed_at.tzinfo is not None and observed_at.utcoffset() == timedelta(0)
    assert len(extraction_calls) == len(write_calls) == len(engine_calls) == 1
    assert write_calls[0]["engine"] is engine
    assert write_calls[0]["batch_id"] == result["batch_id"]
    assert write_calls[0]["rows"][0].value == 3.25
