"""Tests for RequestLoggingMiddleware."""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from opendata.middleware.request_logging import RequestLoggingMiddleware


@pytest.fixture
def test_app():
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/test")
    async def test_endpoint():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/error")
    async def error_endpoint():
        raise RuntimeError("boom")

    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app, raise_server_exceptions=False)


class TestRequestLogging:
    def test_normal_request(self, client):
        resp = client.get("/test")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers
        assert len(resp.headers["X-Request-ID"]) == 8

    def test_health_skip_logging(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers

    def test_error_request(self, client):
        resp = client.get("/error")
        assert resp.status_code == 500

    def test_404_request(self, client):
        resp = client.get("/nonexistent")
        assert resp.status_code == 404
        assert "X-Request-ID" in resp.headers
