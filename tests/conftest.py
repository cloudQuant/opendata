"""
Pytest configuration and fixtures.

Provides shared test fixtures for all test modules.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from functools import wraps

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# These must be set before importing any app module. Settings reads .env during
# import; empty Redis and Sentry URLs keep their singletons from doing I/O.
os.environ["TESTING"] = "true"
os.environ["APP_ENV"] = "testing"
os.environ["REDIS_URL"] = ""
os.environ["SENTRY_DSN"] = ""

# Live e2e legs are released only by the exact token used by the collection guard.
LIVE_E2E_OPT_IN_ENV = "OPENDATA_ALLOW_LIVE_E2E"
LIVE_E2E_OPT_IN_TOKEN = "allow-prod-and-upstream-writes"
LIVE_E2E_MARKER = "e2e"
OWNED_MYSQL_E2E_NODE = (
    "tests/test_script_download_backend_iter2.py::"
    "test_real_browser_download_chain_against_owned_mysql"
)


def live_e2e_allowed() -> bool:
    """Whether the operator explicitly released the live e2e legs."""
    return os.environ.get(LIVE_E2E_OPT_IN_ENV, "").strip() == LIVE_E2E_OPT_IN_TOKEN


from opendata.core.database import (  # noqa: E402  # environment guard must precede app imports
    Base,
    data_engine,
    engine,
    get_data_db,
    get_db,
)

_LIVE_DEFAULT_DB_ACCESS_NODE_ID: str | None = None


def _live_default_db_access_allowed() -> bool:
    """Allow default-engine access only during an explicitly released e2e item."""
    return _LIVE_DEFAULT_DB_ACCESS_NODE_ID is not None and live_e2e_allowed()


def _make_default_engine_connect_guard(engine_name: str) -> Callable[..., None]:
    """Reject new DBAPI connections through one of the app's default engines."""

    def guard_do_connect(*_args: object, **_kwargs: object) -> None:
        if _live_default_db_access_allowed():
            return
        raise RuntimeError(
            f"test blocked a DBAPI connection through the app's default {engine_name} engine "
            f"before driver connect; only an e2e item with exact {LIVE_E2E_OPT_IN_ENV} opt-in "
            "may use it"
        )

    return guard_do_connect


def _make_default_engine_checkout_guard(engine_name: str) -> Callable[..., None]:
    """Reject reuse of a pooled default connection by an unreleased test item."""

    def guard_checkout(*_args: object, **_kwargs: object) -> None:
        if _live_default_db_access_allowed():
            return
        raise RuntimeError(
            f"test blocked checkout of the app's default {engine_name} connection; "
            "the connection cannot be used by this test item"
        )

    return guard_checkout


def _make_default_engine_cursor_guard(engine_name: str) -> Callable[..., None]:
    """Reject SQLAlchemy cursor execution through a retained default connection."""

    def guard_before_cursor_execute(*_args: object, **_kwargs: object) -> None:
        if _live_default_db_access_allowed():
            return
        raise RuntimeError(
            f"test blocked SQL execution through the app's default {engine_name} engine "
            "before cursor execution"
        )

    return guard_before_cursor_execute


_GUARDED_DEFAULT_ENGINE_GUARDS: dict[
    int, tuple[object, tuple[tuple[str, Callable[..., None]], ...]]
] = {}


def _install_default_engine_guards(
    target: object, engine_name: str
) -> tuple[Callable[..., None], ...]:
    """Guard one known app-owned Engine object without affecting explicit fixture engines."""
    sync_target = getattr(target, "sync_engine", target)
    existing = _GUARDED_DEFAULT_ENGINE_GUARDS.get(id(sync_target))
    if existing is not None and existing[0] is sync_target:
        return tuple(listener for _, listener in existing[1])

    guards = (
        ("do_connect", _make_default_engine_connect_guard(engine_name)),
        ("checkout", _make_default_engine_checkout_guard(engine_name)),
        ("before_cursor_execute", _make_default_engine_cursor_guard(engine_name)),
    )
    for event_name, listener in guards:
        event.listen(sync_target, event_name, listener)
    _GUARDED_DEFAULT_ENGINE_GUARDS[id(sync_target)] = (sync_target, guards)
    return tuple(listener for _, listener in guards)


def _guard_default_engine_factory(
    factory: Callable[..., object], engine_name: str
) -> Callable[..., object]:
    """Wrap a known cached factory so every returned default Engine is guarded."""
    cache_info = getattr(factory, "cache_info", None)
    if callable(cache_info) and cache_info().currsize:
        # A plugin may have imported and primed this factory before conftest.
        # Attach guards to its cached Engine before test modules can retain it.
        _install_default_engine_guards(factory(), engine_name)

    @wraps(factory)
    def guarded_factory(*args: object, **kwargs: object) -> object:
        default_engine = factory(*args, **kwargs)
        _install_default_engine_guards(default_engine, engine_name)
        return default_engine

    for cache_method_name in ("cache_clear", "cache_info", "cache_parameters"):
        cache_method = getattr(factory, cache_method_name, None)
        if callable(cache_method):
            setattr(guarded_factory, cache_method_name, cache_method)
    return guarded_factory


_CONTROL_DB_GUARDS = _install_default_engine_guards(engine, "control")
_DATA_DB_GUARDS = _install_default_engine_guards(data_engine, "data")
_CONTROL_DB_CONNECT_GUARD, _CONTROL_DB_CHECKOUT_GUARD, _CONTROL_DB_CURSOR_GUARD = _CONTROL_DB_GUARDS
_DATA_DB_CONNECT_GUARD, _DATA_DB_CHECKOUT_GUARD, _DATA_DB_CURSOR_GUARD = _DATA_DB_GUARDS

# Install before importing ``opendata.main`` and before pytest imports any test
# modules. These four known default targets are guarded at DBAPI connect,
# checkout, and SQLAlchemy cursor execution. Direct DBAPI use is outside scope.
from opendata.pipeline import jobs as pipeline_jobs  # noqa: E402  # protect before app/test imports

pipeline_jobs.warehouse_engine = _guard_default_engine_factory(
    pipeline_jobs.warehouse_engine, "pipeline warehouse"
)
pipeline_jobs.control_engine = _guard_default_engine_factory(
    pipeline_jobs.control_engine, "pipeline control"
)

from opendata.main import app  # noqa: E402  # default-engine guards must be installed first

# Test database URL
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None):
    """Scope default-engine access to one exact-token, e2e-marked test item."""
    global _LIVE_DEFAULT_DB_ACCESS_NODE_ID

    _LIVE_DEFAULT_DB_ACCESS_NODE_ID = None
    if live_e2e_allowed() and item.get_closest_marker(LIVE_E2E_MARKER) is not None:
        _LIVE_DEFAULT_DB_ACCESS_NODE_ID = item.nodeid
    try:
        yield
    finally:
        _LIVE_DEFAULT_DB_ACCESS_NODE_ID = None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Deny live ``e2e`` items while allowing the exact owned-MySQL harness.

    Keyed on the same marker that ``make gate``'s ``-m "not e2e"`` deselects, so
    the two planes cannot disagree about the list: under the gate these items are
    removed before this hook's skip marker is ever reached, and outside the gate
    they skip with the reason instead of running.
    """
    if live_e2e_allowed():
        return
    skip_live = pytest.mark.skip(
        reason=(
            f"live e2e leg reaches the production warehouse / upstream APIs; run with "
            f'-m "not e2e" (as make gate does) or set {LIVE_E2E_OPT_IN_ENV}='
            f"{LIVE_E2E_OPT_IN_TOKEN} to execute it"
        )
    )
    for item in items:
        if item.nodeid == OWNED_MYSQL_E2E_NODE:
            from opendata.core.token_blacklist import _InMemoryBlacklist, token_blacklist

            # Global test cleanup must not invalidate sessions in a shared Redis backend.
            if isinstance(token_blacklist, _InMemoryBlacklist):
                continue
        if item.get_closest_marker(LIVE_E2E_MARKER) is not None:
            item.add_marker(skip_live)


@pytest.fixture(scope="session", autouse=True)
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Set event loop policy for the entire test session."""
    policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(policy)
    yield policy
    # Clean up
    asyncio.set_event_loop_policy(None)


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Create test database engine."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_db(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    async_session_maker = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session_maker() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def test_client(test_db):
    """Create test client with database override."""

    async def override_get_db():
        yield test_db

    async def override_get_data_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_data_db] = override_get_data_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


def _clear_blacklist(bl: object) -> None:
    """Clear only the process-local test blacklist, never a shared Redis backend."""
    from opendata.core.token_blacklist import _InMemoryBlacklist

    if isinstance(bl, _InMemoryBlacklist):
        with bl._lock:
            bl._blacklist.clear()


@pytest.fixture(autouse=True)
def _clear_test_state():
    """Clear shared state before each test."""
    from opendata.core.token_blacklist import token_blacklist
    from opendata.utils.cache import api_cache

    _clear_blacklist(token_blacklist)
    api_cache.clear()
    yield
    _clear_blacklist(token_blacklist)
    api_cache.clear()


@pytest.fixture
def test_user_data():
    """Provide test user data."""
    return {
        "email": "testuser@example.com",
        "password": "Password123!",
        "password_confirm": "Password123!",
    }


@pytest.fixture
def test_admin_data():
    """Provide test admin data."""
    return {
        "email": "admin@example.com",
        "password": "AdminPass123!",
        "password_confirm": "AdminPass123!",
    }


@pytest_asyncio.fixture
async def test_user(test_client, test_user_data):
    """Create a test user and return auth token."""
    # Register user
    await test_client.post("/api/auth/register", json=test_user_data)

    # Login to get token
    response = await test_client.post(
        "/api/auth/login",
        json={
            "email": test_user_data["email"],
            "password": test_user_data["password"],
        },
    )

    data = response.json()
    return data["data"]["access_token"]


# Alias for backward compatibility
test_user_token = test_user


async def _create_admin_and_login(test_client, test_db, email, password):
    """Helper: create admin user directly and return access token."""
    from opendata.core.security import hash_password
    from opendata.models.user import User, UserRole

    admin = User(
        username=email.split("@")[0],
        email=email,
        hashed_password=hash_password(password),
        role=UserRole.ADMIN,
        is_active=True,
    )
    test_db.add(admin)
    await test_db.commit()

    response = await test_client.post(
        "/api/auth/login",
        json={
            "email": email,
            "password": password,
        },
    )
    data = response.json()
    return data["data"]["access_token"]


@pytest_asyncio.fixture
async def test_admin(test_client, test_admin_data, test_db):
    """Create a test admin user and return auth token."""
    return await _create_admin_and_login(
        test_client, test_db, test_admin_data["email"], test_admin_data["password"]
    )


# Alias for backward compatibility
test_admin_token = test_admin


def get_auth_headers(token: str) -> dict:
    """Helper to get auth headers."""
    return {"Authorization": f"Bearer {token}"}
