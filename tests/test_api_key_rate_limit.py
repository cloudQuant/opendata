"""Rolling per-consumer-key rate-limit behavior and dependency integration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from opendata.api import dependencies
from opendata.api.dependencies import CurrentPrincipal, get_db, require_domain_access
from opendata.core.api_key_rate_limit import (
    REDIS_KEY_PREFIX,
    APIKeyRateLimiter,
    APIKeyRateLimitUnavailableError,
    LocalSlidingWindowBackend,
    RedisSlidingWindowBackend,
    api_key_rate_limiter,
    redis_bucket_key,
)
from opendata.core.config import settings
from opendata.models.api_key import ApiKey, ApiKeyStatus
from opendata.models.user import User, UserRole
from opendata.services.api_key_service import ApiKeyService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from sqlalchemy.ext.asyncio import AsyncSession


class FakeClock:
    """Mutable monotonic clock for deterministic window boundaries."""

    def __init__(self, initial: float = 1_000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingBackend:
    """Backend spy for authentication lifecycle and key-ID assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    async def consume(
        self,
        api_key_id: int,
        limit: int,
        *,
        window_seconds: int,
    ) -> int | None:
        self.calls.append((api_key_id, limit, window_seconds))
        return None


class FakeRedis:
    """Small async Redis eval fake that records the atomic script call."""

    def __init__(self, result: object = (1, 0), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.call: tuple[str, int, tuple[str | int, ...]] | None = None

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object:
        self.call = (script, numkeys, keys_and_args)
        if self.error is not None:
            raise self.error
        return self.result


async def _issue_key(
    db: AsyncSession,
    *,
    rate_limit: int = 100,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> tuple[User, ApiKey, str]:
    """Create one isolated owner and API key for an HTTP dependency test."""
    suffix = uuid4().hex
    owner = User(
        username=f"rate_owner_{suffix}",
        email=f"rate_owner_{suffix}@example.test",
        hashed_password="unused-test-hash",
        role=UserRole.USER,
        is_active=True,
    )
    db.add(owner)
    await db.flush()
    issued = await ApiKeyService(db).issue(
        owner=owner,
        name="rate-limit-test",
        scopes=scopes or ["*"],
        rate_limit=rate_limit,
        expires_at=expires_at,
    )
    return owner, issued.record, issued.plaintext


def _principal_app(db: AsyncSession) -> FastAPI:
    """Build a tiny HTTP app that exercises the real shared principal dependency."""
    app = FastAPI()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db

    async def resource(
        principal: CurrentPrincipal,
        domain: str = "stock_daily",
    ) -> dict[str, int | None]:
        require_domain_access(principal, domain)
        return {"user_id": principal.user.id, "api_key_id": principal.api_key_id}

    app.dependency_overrides[get_db] = override_get_db
    app.get("/resource")(resource)
    app.get("/other-resource")(resource)
    return app


@pytest.fixture(autouse=True)
def reset_global_limiter_state() -> Iterator[None]:
    """Keep the production singleton's local buckets isolated in this module."""
    api_key_rate_limiter.reset_local_state()
    yield
    api_key_rate_limiter.reset_local_state()


@pytest.fixture
def local_limiter(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeClock, APIKeyRateLimiter]:
    """Install a fresh local limiter and fake clock for this test only."""
    monkeypatch.setattr(settings, "redis_url", None)
    clock = FakeClock()
    limiter = APIKeyRateLimiter(clock=clock)
    monkeypatch.setattr(dependencies, "api_key_rate_limiter", limiter)
    return clock, limiter


@pytest.mark.asyncio
async def test_local_limit_allows_n_then_returns_positive_retry_after() -> None:
    """A rolling bucket admits exactly N requests and returns a usable delay."""
    clock = FakeClock()
    backend = LocalSlidingWindowBackend(clock=clock)

    assert await backend.consume(11, 2, window_seconds=60) is None
    assert await backend.consume(11, 2, window_seconds=60) is None
    retry_after = await backend.consume(11, 2, window_seconds=60)

    assert retry_after is not None and retry_after > 0
    assert set(backend._windows) == {11}


@pytest.mark.asyncio
async def test_local_window_rolls_over_at_sixty_seconds() -> None:
    """Events at the exact 60-second boundary no longer consume quota."""
    clock = FakeClock()
    backend = LocalSlidingWindowBackend(clock=clock)

    assert await backend.consume(12, 1, window_seconds=60) is None
    assert await backend.consume(12, 1, window_seconds=60) == 60
    clock.advance(60)
    assert await backend.consume(12, 1, window_seconds=60) is None


@pytest.mark.asyncio
async def test_local_concurrent_requests_never_admit_more_than_limit() -> None:
    """The local critical section preserves the cap under concurrent calls."""
    backend = LocalSlidingWindowBackend(clock=FakeClock())
    results = await asyncio.gather(*(backend.consume(13, 7, window_seconds=60) for _ in range(80)))

    assert sum(result is None for result in results) == 7
    assert sum(result is not None for result in results) == 73


@pytest.mark.asyncio
async def test_local_keys_are_independent_and_expired_state_is_pruned() -> None:
    """One consumer cannot consume another key's budget; expired buckets drop."""
    clock = FakeClock()
    backend = LocalSlidingWindowBackend(clock=clock, max_keys=2)

    assert await backend.consume(21, 1, window_seconds=60) is None
    assert await backend.consume(22, 1, window_seconds=60) is None
    with pytest.raises(APIKeyRateLimitUnavailableError):
        await backend.consume(23, 1, window_seconds=60)
    clock.advance(60)
    assert await backend.consume(23, 1, window_seconds=60) is None
    assert set(backend._windows) == {23}


@pytest.mark.asyncio
async def test_local_backend_bounds_total_active_events() -> None:
    """Unusually high configured quotas cannot make local state unbounded."""
    clock = FakeClock()
    backend = LocalSlidingWindowBackend(clock=clock, max_events=2)

    assert await backend.consume(24, 10, window_seconds=60) is None
    assert await backend.consume(24, 10, window_seconds=60) is None
    with pytest.raises(APIKeyRateLimitUnavailableError):
        await backend.consume(24, 10, window_seconds=60)
    assert backend._event_count == 2

    clock.advance(60)
    assert await backend.consume(24, 10, window_seconds=60) is None
    assert backend._event_count == 1


@pytest.mark.asyncio
async def test_api_key_limit_is_shared_across_current_principal_endpoints_and_not_testing_bypassed(
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    local_limiter: tuple[FakeClock, APIKeyRateLimiter],
) -> None:
    """Authenticated key calls share one quota even with TESTING enabled."""
    _, limiter = local_limiter
    monkeypatch.setenv("TESTING", "true")
    _, record, plaintext = await _issue_key(test_db, rate_limit=2)
    _, second_record, second_plaintext = await _issue_key(test_db, rate_limit=2)
    app = _principal_app(test_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/resource", headers={"X-API-Key": plaintext})
        second = await client.get("/other-resource", headers={"X-API-Key": plaintext})
        blocked = await client.get("/resource", headers={"X-API-Key": plaintext})
        independent = await client.get("/resource", headers={"X-API-Key": second_plaintext})

    assert first.status_code == second.status_code == independent.status_code == 200
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    assert plaintext not in repr(limiter._local_backend._windows)
    assert record.id in limiter._local_backend._windows
    assert second_record.id in limiter._local_backend._windows


@pytest.mark.asyncio
async def test_scopes_and_invalid_key_lifecycle_are_checked_before_limiter(
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid scoped keys work; revoked, expired, and disabled-owner keys do not count."""
    monkeypatch.setattr(settings, "redis_url", None)
    backend = RecordingBackend()
    monkeypatch.setattr(
        dependencies,
        "api_key_rate_limiter",
        APIKeyRateLimiter(backend=backend),
    )
    scoped_owner, scoped_record, scoped_secret = await _issue_key(test_db, scopes=["stock_daily"])
    revoked_owner, revoked_record, revoked_secret = await _issue_key(test_db)
    revoked_record.status = ApiKeyStatus.REVOKED
    expired_owner, expired_record, expired_secret = await _issue_key(
        test_db,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    disabled_owner, disabled_record, disabled_secret = await _issue_key(test_db)
    disabled_owner.is_active = False
    await test_db.commit()
    app = _principal_app(test_db)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        scoped = await client.get(
            "/resource?domain=stock_daily",
            headers={"X-API-Key": scoped_secret},
        )
        out_of_scope = await client.get(
            "/resource?domain=index_constituent",
            headers={"X-API-Key": scoped_secret},
        )
        revoked = await client.get("/resource", headers={"X-API-Key": revoked_secret})
        expired = await client.get("/resource", headers={"X-API-Key": expired_secret})
        disabled = await client.get("/resource", headers={"X-API-Key": disabled_secret})

    assert scoped.status_code == 200
    assert out_of_scope.status_code == 403
    assert revoked.status_code == expired.status_code == 401
    assert disabled.status_code == 403
    assert [call[0] for call in backend.calls] == [scoped_record.id, scoped_record.id]
    assert all(
        secret not in repr(backend.calls)
        for secret in (scoped_secret, revoked_secret, expired_secret, disabled_secret)
    )
    assert all(record.id > 0 for record in (revoked_record, expired_record, disabled_record))


@pytest.mark.asyncio
async def test_jwt_principal_does_not_consume_consumer_key_quota(
    test_db: AsyncSession,
    test_user_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer-key gate leaves JWT authentication untouched."""
    backend = RecordingBackend()
    monkeypatch.setattr(
        dependencies,
        "api_key_rate_limiter",
        APIKeyRateLimiter(backend=backend),
    )
    app = _principal_app(test_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/resource",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

    assert response.status_code == 200
    assert response.json()["api_key_id"] is None
    assert backend.calls == []


@pytest.mark.asyncio
async def test_redis_backend_uses_one_atomic_numeric_id_bucket() -> None:
    """Redis updates the timestamp set and sequence inside one Lua invocation."""
    client = FakeRedis(result=(1, 0))
    backend = RedisSlidingWindowBackend(client)

    assert await backend.consume(42, 9, window_seconds=60) is None
    assert client.call is not None
    script, numkeys, args = client.call
    assert numkeys == 2
    assert args == (
        f"{REDIS_KEY_PREFIX}:42",
        f"{REDIS_KEY_PREFIX}:42:sequence",
        60_000,
        9,
    )
    assert "redis.call('TIME')" in script
    assert "redis.call('ZREMRANGEBYSCORE'" in script
    assert "redis.call('ZADD'" in script
    assert redis_bucket_key(42) == f"{REDIS_KEY_PREFIX}:42"

    blocked_client = FakeRedis(result=(0, 3))
    assert await RedisSlidingWindowBackend(blocked_client).consume(42, 9, window_seconds=60) == 3


@pytest.mark.asyncio
async def test_configured_redis_failure_is_503_without_local_fallback_or_secret_logging(
    test_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis outage fails closed and neither response nor logs disclose the key."""
    _, record, plaintext = await _issue_key(test_db, rate_limit=3)

    class FailingBackend:
        async def consume(
            self,
            api_key_id: int,
            limit: int,
            *,
            window_seconds: int,
        ) -> int | None:
            raise RuntimeError(f"redis connection failed for {plaintext}")

    monkeypatch.setattr(settings, "redis_url", "redis://redis.example.test:6379/0")
    limiter = APIKeyRateLimiter(redis_backend_factory=lambda _: FailingBackend())
    monkeypatch.setattr(dependencies, "api_key_rate_limiter", limiter)
    app = _principal_app(test_db)
    from loguru import logger

    sink = StringIO()
    handler_id = logger.add(sink, level="ERROR", format="{message}")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/resource", headers={"X-API-Key": plaintext})
    finally:
        logger.remove(handler_id)

    assert response.status_code == 503
    assert plaintext not in response.text
    assert plaintext not in sink.getvalue()
    assert f"key_id={record.id}" in sink.getvalue()
    assert limiter._local_backend._windows == {}
