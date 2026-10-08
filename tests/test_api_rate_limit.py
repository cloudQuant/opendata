"""ASGI coverage for auth rate limiting and its test-mode bypass."""

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from slowapi.errors import RateLimitExceeded

from opendata.api.auth import router as auth_router
from opendata.api.dependencies import get_db
from opendata.api.rate_limit import get_limiter


@pytest.fixture
def rate_limited_app(monkeypatch, test_db):
    """Mount the real auth router with enforcement enabled and an isolated DB."""
    monkeypatch.setenv("TESTING", "false")
    limiter = get_limiter()
    assert limiter is not None

    app = FastAPI()
    app.state.limiter = limiter

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please try again later."},
        )

    app.include_router(auth_router, prefix="/auth")

    async def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _client(app: FastAPI, *, remote_ip: str) -> AsyncClient:
    """Create an ASGI client with a stable per-test limiter key."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(remote_ip, 12345)),
        base_url="http://test",
    )


def _email() -> str:
    """Return a unique valid email for isolated registration requests."""
    return f"rate-limit-{uuid4().hex}@example.com"


class TestAuthRateLimit:
    async def test_login_body_does_not_replace_the_fastapi_request(
        self, rate_limited_app, monkeypatch
    ):
        monkeypatch.setenv("TESTING", "false")

        async with _client(rate_limited_app, remote_ip="198.51.100.21") as client:
            response = await client.post(
                "/auth/login",
                json={"email": "missing@example.com", "password": "Password123!"},
            )

        assert response.status_code == 401
        assert response.json()["detail"] == "邮箱或密码错误"

    async def test_login_and_register_enforce_separate_declared_limits(
        self, rate_limited_app, monkeypatch
    ):
        monkeypatch.setenv("TESTING", "false")
        registration = {
            "email": _email(),
            "password": "Password123!",
            "password_confirm": "Password123!",
        }

        async with _client(rate_limited_app, remote_ip="198.51.100.22") as client:
            register_responses = []
            for _ in range(5):
                registration["email"] = _email()
                register_responses.append(await client.post("/auth/register", json=registration))
            register_over_limit = await client.post("/auth/register", json=registration)

            login_responses = [
                await client.post(
                    "/auth/login",
                    json={"email": "missing@example.com", "password": "Password123!"},
                )
                for _ in range(10)
            ]
            login_over_limit = await client.post(
                "/auth/login",
                json={"email": "missing@example.com", "password": "Password123!"},
            )

        assert [response.status_code for response in register_responses] == [201] * 5
        assert register_over_limit.status_code == 429
        assert [response.status_code for response in login_responses] == [401] * 10
        assert login_over_limit.status_code == 429
        assert login_over_limit.json() == {"detail": "Rate limit exceeded. Please try again later."}

    async def test_testing_mode_bypasses_both_auth_limits(self, rate_limited_app, monkeypatch):
        monkeypatch.setenv("TESTING", "true")

        async with _client(rate_limited_app, remote_ip="198.51.100.23") as client:
            login_responses = [
                await client.post(
                    "/auth/login",
                    json={"email": "missing@example.com", "password": "Password123!"},
                )
                for _ in range(11)
            ]
            register_responses = [
                await client.post(
                    "/auth/register",
                    json={
                        "email": _email(),
                        "password": "Password123!",
                        "password_confirm": "Password123!",
                    },
                )
                for _ in range(6)
            ]

        assert [response.status_code for response in login_responses] == [401] * 11
        assert [response.status_code for response in register_responses] == [201] * 6

    def test_openapi_keeps_the_login_and_register_json_body_models(self):
        from opendata.main import app

        paths = app.openapi()["paths"]
        for route, model_name in (
            ("/api/v1/auth/login", "LoginRequest"),
            ("/api/v1/auth/register", "RegisterRequest"),
        ):
            body_schema = paths[route]["post"]["requestBody"]["content"]["application/json"]
            assert body_schema["schema"]["$ref"] == f"#/components/schemas/{model_name}"
