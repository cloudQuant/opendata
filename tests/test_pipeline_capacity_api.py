"""Request-boundary tests for the manual pipeline capacity controls."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from opendata.api import pipeline as run_api
from opendata.api.dependencies import get_current_user


class _SyntheticResult:
    """Small result value used to finish an accepted background task."""

    failures = 0

    def as_dict(self) -> dict[str, Any]:
        """Return the status payload expected by the API task wrapper."""
        return {"synthetic": True}


class _AsgiResponse:
    """Captured response from a direct synthetic ASGI invocation."""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        """Decode the captured JSON body."""
        return json.loads(self._body)


@pytest.fixture(autouse=True)
def _isolated_run_registry():
    """Keep the module-level in-memory run registry local to each test."""
    run_api._RUNS.clear()
    run_api._TASKS.clear()
    yield
    run_api._RUNS.clear()
    run_api._TASKS.clear()


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a standalone ASGI app with authentication and job doubles."""
    app = FastAPI()

    async def fake_current_user() -> object:
        return object()

    app.state.job_calls = []

    async def fake_job(**kwargs: Any) -> _SyntheticResult:
        app.state.job_calls.append(kwargs)
        return _SyntheticResult()

    app.dependency_overrides[get_current_user] = fake_current_user
    app.include_router(run_api.router, prefix="/api/v1")
    monkeypatch.setattr(run_api, "run_incremental_job", fake_job)
    return app


async def _post_run(app: FastAPI, params: list[tuple[str, str]] | None = None):
    """Issue an in-process ASGI request without starting application services."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/pipeline/run", params=params)
    if response.status_code == 202:
        run_id = response.json()["data"]["run_id"]
        await run_api._TASKS[run_id]
    return response


async def _post_repeated_symbols_directly(app: FastAPI, symbols: list[str]) -> _AsgiResponse:
    """Invoke the ASGI app with a synthetic query scope beyond client URL caps.

    This exercises server-side query parsing and validation. It says nothing
    about whether an HTTP client, reverse proxy, or production server accepts
    query strings of this size.
    """
    query_string = urlencode([("symbols", symbol) for symbol in symbols]).encode("ascii")
    request_sent = False
    response_status = 500
    response_body = bytearray()

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal response_status
        if message["type"] == "http.response.start":
            response_status = message["status"]
        elif message["type"] == "http.response.body":
            response_body.extend(message.get("body", b""))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/pipeline/run",
        "raw_path": b"/api/v1/pipeline/run",
        "query_string": query_string,
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": ("test", 80),
        "state": {},
    }
    await app(scope, receive, send)
    response = _AsgiResponse(response_status, bytes(response_body))
    if response.status_code == 202:
        run_id = response.json()["data"]["run_id"]
        await run_api._TASKS[run_id]
    return response


@pytest.mark.asyncio
async def test_default_limit_remains_200(api_app: FastAPI) -> None:
    response = await _post_run(api_app)

    assert response.status_code == 202
    assert response.json()["data"]["request"]["limit"] == 200
    assert response.json()["data"]["request"]["symbols"] is None
    assert api_app.state.job_calls[0]["limit"] == 200
    assert api_app.state.job_calls[0]["symbols"] is None


@pytest.mark.asyncio
async def test_limit_accepts_10000(api_app: FastAPI) -> None:
    response = await _post_run(api_app, [("limit", "10000")])

    assert response.status_code == 202
    assert response.json()["data"]["request"]["limit"] == 10000
    assert api_app.state.job_calls[0]["limit"] == 10000


@pytest.mark.asyncio
async def test_limit_above_10000_is_rejected(api_app: FastAPI) -> None:
    response = await _post_run(api_app, [("limit", "10001")])

    assert response.status_code == 422
    assert run_api._RUNS == {}
    assert api_app.state.job_calls == []


@pytest.mark.asyncio
async def test_10000_repeated_symbol_parameters_are_preserved(api_app: FastAPI) -> None:
    symbols = [f"{index:06d}" for index in range(10000)]
    response = await _post_repeated_symbols_directly(api_app, symbols)

    assert response.status_code == 202
    assert response.json()["data"]["request"]["symbols"] == symbols
    assert api_app.state.job_calls[0]["symbols"] == symbols


@pytest.mark.asyncio
async def test_more_than_10000_repeated_symbol_parameters_are_rejected(
    api_app: FastAPI,
) -> None:
    symbols = [f"{index:06d}" for index in range(10001)]
    response = await _post_repeated_symbols_directly(api_app, symbols)

    assert response.status_code == 422
    assert run_api._RUNS == {}
    assert api_app.state.job_calls == []
