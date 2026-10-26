"""Behavioral tests for guards on app-owned default database engines."""

import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event

from opendata.api import data_query as api_data_query
from opendata.api import minute_data as api_minute_data
from opendata.api import published_data as api_published_data
from opendata.core import config, database
from opendata.core.token_blacklist import _InMemoryBlacklist, _RedisBlacklist, token_blacklist
from opendata.data import minute_archive
from opendata.pipeline import jobs as pipeline_jobs
from tests import conftest


@pytest.mark.parametrize(
    ("target", "connect_guard", "checkout_guard", "cursor_guard"),
    [
        (
            database.engine.sync_engine,
            conftest._CONTROL_DB_CONNECT_GUARD,
            conftest._CONTROL_DB_CHECKOUT_GUARD,
            conftest._CONTROL_DB_CURSOR_GUARD,
        ),
        (
            database.data_engine.sync_engine,
            conftest._DATA_DB_CONNECT_GUARD,
            conftest._DATA_DB_CHECKOUT_GUARD,
            conftest._DATA_DB_CURSOR_GUARD,
        ),
    ],
)
def test_core_default_engines_have_connect_checkout_and_cursor_guards(
    target, connect_guard, checkout_guard, cursor_guard
) -> None:
    """Both core default engines block connection and SQL paths outside opt-in."""
    assert event.contains(target, "do_connect", connect_guard)
    assert event.contains(target, "checkout", checkout_guard)
    assert event.contains(target, "before_cursor_execute", cursor_guard)


def test_explicit_fixture_engine_does_not_inherit_app_default_guards() -> None:
    """An independently created in-memory fixture engine remains unguarded."""
    fixture_engine = create_engine("sqlite://")
    try:
        for event_name, listener in (
            ("do_connect", conftest._CONTROL_DB_CONNECT_GUARD),
            ("checkout", conftest._CONTROL_DB_CHECKOUT_GUARD),
            ("before_cursor_execute", conftest._CONTROL_DB_CURSOR_GUARD),
        ):
            assert not event.contains(fixture_engine, event_name, listener)
        with fixture_engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        fixture_engine.dispose()


def test_default_connect_guard_rejects_before_dbapi_connect(monkeypatch) -> None:
    """The connect guard fires before the DBAPI connect/authentication call."""
    monkeypatch.setattr(conftest, "_LIVE_DEFAULT_DB_ACCESS_NODE_ID", None)
    fixture_engine = create_engine("sqlite://")
    dbapi_connect_calls: list[object] = []

    def record_dbapi_connect(*args: object, **kwargs: object) -> None:
        dbapi_connect_calls.append((args, kwargs))
        raise AssertionError("DBAPI connect must be unreachable behind the guard")

    guard = conftest._make_default_engine_connect_guard("probe")
    monkeypatch.setattr(fixture_engine.dialect.dbapi, "connect", record_dbapi_connect)
    event.listen(fixture_engine, "do_connect", guard)
    try:
        with pytest.raises(RuntimeError, match="before driver connect"):
            fixture_engine.raw_connection()
        assert dbapi_connect_calls == []
    finally:
        fixture_engine.dispose()


def test_checkout_guard_rejects_a_pooled_in_memory_connection() -> None:
    """A pooled test-local SQLite handle is stopped before checkout returns it."""
    fixture_engine = create_engine("sqlite://")
    with fixture_engine.connect():
        pass
    guard = conftest._make_default_engine_checkout_guard("probe")
    event.listen(fixture_engine, "checkout", guard)
    try:
        with pytest.raises(RuntimeError, match="blocked checkout"):
            fixture_engine.connect()
    finally:
        fixture_engine.dispose()


def test_cursor_guard_rejects_sql_on_an_already_checked_out_sqlite_connection(
    monkeypatch,
) -> None:
    """A retained Connection cannot execute SQL after live authorization ends."""
    executed_statements: list[str] = []

    class RecordingCursor(sqlite3.Cursor):
        """Collect SQL that reaches the SQLite DBAPI cursor."""

        def execute(self, statement, parameters=()):
            """Record SQL before delegating to the SQLite cursor."""
            executed_statements.append(statement)
            return super().execute(statement, parameters)

    class RecordingConnection(sqlite3.Connection):
        """Route SQLite cursors through the test-local recorder."""

        def cursor(self, *args, **kwargs):
            """Return a cursor that records statements passed to the DBAPI."""
            kwargs["factory"] = RecordingCursor
            return super().cursor(*args, **kwargs)

    monkeypatch.setenv(conftest.LIVE_E2E_OPT_IN_ENV, conftest.LIVE_E2E_OPT_IN_TOKEN)
    monkeypatch.setattr(conftest, "_LIVE_DEFAULT_DB_ACCESS_NODE_ID", None)
    fixture_engine = create_engine("sqlite://", connect_args={"factory": RecordingConnection})
    connection = fixture_engine.connect()
    executed_statements.clear()
    guard = conftest._make_default_engine_cursor_guard("probe")
    event.listen(fixture_engine, "before_cursor_execute", guard)
    try:
        with pytest.raises(RuntimeError, match="before cursor execution"):
            connection.exec_driver_sql("SELECT 1")
        assert executed_statements == []
    finally:
        event.remove(fixture_engine, "before_cursor_execute", guard)
        connection.close()
        fixture_engine.dispose()


@pytest.mark.parametrize(
    ("factory", "engine_name", "settings_values"),
    [
        (
            pipeline_jobs.warehouse_engine,
            "pipeline warehouse",
            {"data_database_url": "sqlite://"},
        ),
        (
            pipeline_jobs.control_engine,
            "pipeline control",
            {"database_url_sync": "sqlite://"},
        ),
        (
            api_data_query.get_warehouse_engine,
            "API warehouse",
            {"data_database_url": "sqlite://"},
        ),
        (
            minute_archive.get_minute_archive_engine,
            "minute archive index",
            {"database_url_sync": "sqlite://"},
        ),
    ],
)
def test_known_default_factories_reject_before_dbapi_connect(
    factory, engine_name: str, settings_values: dict[str, str], monkeypatch
) -> None:
    """Each known cached factory guards a local Engine before driver access."""
    assert callable(factory.cache_clear)
    assert callable(factory.cache_info)
    assert callable(factory.cache_parameters)
    assert factory.cache_info().maxsize == 1
    assert factory.cache_parameters()["maxsize"] == 1

    monkeypatch.setattr(config, "settings", SimpleNamespace(**settings_values))
    factory.cache_clear()
    default_engine = None
    try:
        if factory is api_data_query.get_warehouse_engine:
            assert factory in _route_dependency_calls(api_data_query.router)
        default_engine = factory()
        registered_guards = conftest._GUARDED_DEFAULT_ENGINE_GUARDS[id(default_engine)][1]
        for event_name, listener in registered_guards:
            assert event.contains(default_engine, event_name, listener)

        dbapi_connect_calls: list[object] = []

        def record_dbapi_connect(*args: object, **kwargs: object) -> None:
            dbapi_connect_calls.append((args, kwargs))
            raise AssertionError("DBAPI connect must be unreachable behind the guard")

        monkeypatch.setattr(default_engine.dialect.dbapi, "connect", record_dbapi_connect)
        with pytest.raises(RuntimeError, match="before driver connect"):
            default_engine.raw_connection()
        assert dbapi_connect_calls == []
    finally:
        if default_engine is not None:
            default_engine.dispose()
        factory.cache_clear()


def _route_dependency_calls(router: object) -> list[object]:
    """Collect function identities from a router's existing dependency tree."""
    pending = [
        dependant
        for route in getattr(router, "routes", ())
        if (dependant := getattr(route, "dependant", None)) is not None
    ]
    visited: set[int] = set()
    calls: list[object] = []
    while pending:
        dependant = pending.pop()
        if id(dependant) in visited:
            continue
        visited.add(id(dependant))
        calls.append(getattr(dependant, "call", None))
        pending.extend(getattr(dependant, "dependencies", ()))
    return calls


def test_api_routes_keep_the_original_guarded_factory_identities() -> None:
    """Existing Depends nodes retain both cached getter objects after guard setup."""
    api_factory = api_data_query.get_warehouse_engine
    minute_factory = minute_archive.get_minute_archive_engine
    assert api_factory in _route_dependency_calls(api_data_query.router)
    assert minute_factory in _route_dependency_calls(api_minute_data.router)
    for factory in (api_factory, minute_factory):
        assert callable(factory.cache_clear)
        assert callable(factory.cache_info)
        assert callable(factory.cache_parameters)


def test_published_data_constructor_guards_before_dbapi_connect(monkeypatch) -> None:
    """The configured publication Engine constructor guards its returned Engine."""
    fixture_engine = api_published_data.create_engine("sqlite://", pool_pre_ping=True)
    dbapi_connect_calls: list[object] = []

    def record_dbapi_connect(*args: object, **kwargs: object) -> None:
        dbapi_connect_calls.append((args, kwargs))
        raise AssertionError("DBAPI connect must be unreachable behind the guard")

    monkeypatch.setattr(fixture_engine.dialect.dbapi, "connect", record_dbapi_connect)
    try:
        registered_guards = conftest._GUARDED_DEFAULT_ENGINE_GUARDS[id(fixture_engine)][1]
        for event_name, listener in registered_guards:
            assert event.contains(fixture_engine, event_name, listener)
        ping_registration = conftest._GUARDED_DEFAULT_ENGINE_PING_GUARDS[id(fixture_engine)]
        assert ping_registration[0] is fixture_engine
        assert fixture_engine.dialect.do_ping is ping_registration[1]

        with pytest.raises(RuntimeError, match="before driver connect"):
            fixture_engine.raw_connection()
        assert dbapi_connect_calls == []
    finally:
        fixture_engine.dispose()


def test_default_pre_ping_guard_rejects_before_dbapi_ping() -> None:
    """A default Engine's pre-ping hook rejects before its fake DBAPI cursor runs."""
    fixture_engine = api_published_data.create_engine("sqlite://", pool_pre_ping=True)
    ping_calls: list[str] = []

    class RecordingCursor:
        """Record calls made by SQLAlchemy's dialect ping implementation."""

        def execute(self, statement: str) -> None:
            ping_calls.append(statement)

        def close(self) -> None:
            ping_calls.append("close")

    class RecordingConnection:
        """Provide a fake DBAPI connection for the dialect-only pre-ping path."""

        def cursor(self) -> RecordingCursor:
            ping_calls.append("cursor")
            return RecordingCursor()

    try:
        with pytest.raises(RuntimeError, match="before DBAPI ping"):
            fixture_engine.dialect._do_ping_w_event(RecordingConnection())
        assert ping_calls == []
    finally:
        fixture_engine.dispose()


class _ProtocolItem:
    """Small item stub for directly exercising the protocol authorization hook."""

    def __init__(self, nodeid: str, *, e2e: bool) -> None:
        self.nodeid = nodeid
        self.e2e = e2e

    def get_closest_marker(self, name: str) -> object | None:
        """Return a marker-shaped value only for e2e items."""
        return object() if name == conftest.LIVE_E2E_MARKER and self.e2e else None


class _ProtocolError(Exception):
    """Synthetic failure used to confirm hook cleanup through finally."""


def test_exact_live_opt_in_covers_the_full_e2e_protocol_then_revokes(monkeypatch) -> None:
    """The exact token and marker authorize setup, call, and teardown as one item."""
    monkeypatch.setenv(conftest.LIVE_E2E_OPT_IN_ENV, conftest.LIVE_E2E_OPT_IN_TOKEN)
    item = _ProtocolItem("tests/test_probe.py::test_live_path", e2e=True)
    protocol = conftest.pytest_runtest_protocol(item, None)

    assert next(protocol) is None
    assert item.nodeid == conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID
    for phase in ("setup", "call", "teardown"):
        assert item.nodeid == conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID, phase
        conftest._CONTROL_DB_CONNECT_GUARD()
        conftest._CONTROL_DB_CHECKOUT_GUARD()
        conftest._CONTROL_DB_CURSOR_GUARD()

    with pytest.raises(StopIteration):
        next(protocol)
    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None
    with pytest.raises(RuntimeError, match="before driver connect"):
        conftest._CONTROL_DB_CONNECT_GUARD()


def test_live_protocol_exception_still_revokes_authorization(monkeypatch) -> None:
    """A failed live e2e item loses access in the protocol hook's finally block."""
    monkeypatch.setenv(conftest.LIVE_E2E_OPT_IN_ENV, conftest.LIVE_E2E_OPT_IN_TOKEN)
    item = _ProtocolItem("tests/test_probe.py::test_live_error", e2e=True)
    protocol = conftest.pytest_runtest_protocol(item, None)

    assert next(protocol) is None
    assert item.nodeid == conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID
    with pytest.raises(_ProtocolError, match="downstream failure"):
        protocol.throw(_ProtocolError("downstream failure"))

    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None
    with pytest.raises(RuntimeError, match="before driver connect"):
        conftest._CONTROL_DB_CONNECT_GUARD()


def test_default_pre_ping_delegates_during_exact_live_e2e_protocol(monkeypatch) -> None:
    """The exact live release delegates the default dialect ping to its DBAPI handle."""
    fixture_engine = api_published_data.create_engine("sqlite://", pool_pre_ping=True)
    ping_calls: list[str] = []

    class RecordingCursor:
        """Record the statement sent by the dialect's original ping method."""

        def execute(self, statement: str) -> None:
            ping_calls.append(statement)

        def close(self) -> None:
            ping_calls.append("close")

    class RecordingConnection:
        """Provide a fake DBAPI connection without opening a real database handle."""

        def cursor(self) -> RecordingCursor:
            ping_calls.append("cursor")
            return RecordingCursor()

    monkeypatch.setenv(conftest.LIVE_E2E_OPT_IN_ENV, conftest.LIVE_E2E_OPT_IN_TOKEN)
    item = _ProtocolItem("tests/test_probe.py::test_authorized_ping", e2e=True)
    protocol = conftest.pytest_runtest_protocol(item, None)

    assert next(protocol) is None
    try:
        assert fixture_engine.dialect._do_ping_w_event(RecordingConnection()) is True
        assert ping_calls == [
            "cursor",
            fixture_engine.dialect._dialect_specific_select_one,
            "close",
        ]
    finally:
        with pytest.raises(StopIteration):
            next(protocol)
        fixture_engine.dispose()
    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None


def test_exact_token_without_e2e_marker_does_not_authorize_access(monkeypatch) -> None:
    """A correctly spelled token cannot release an ordinary test item."""
    monkeypatch.setenv(conftest.LIVE_E2E_OPT_IN_ENV, conftest.LIVE_E2E_OPT_IN_TOKEN)
    item = _ProtocolItem("tests/test_probe.py::test_ordinary", e2e=False)
    protocol = conftest.pytest_runtest_protocol(item, None)

    assert next(protocol) is None
    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None
    with pytest.raises(RuntimeError, match="before driver connect"):
        conftest._CONTROL_DB_CONNECT_GUARD()
    with pytest.raises(StopIteration):
        next(protocol)
    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None


def test_e2e_marker_without_exact_token_does_not_authorize_access(monkeypatch) -> None:
    """An e2e marker without the exact release token remains blocked."""
    monkeypatch.delenv(conftest.LIVE_E2E_OPT_IN_ENV, raising=False)
    item = _ProtocolItem("tests/test_probe.py::test_marked", e2e=True)
    protocol = conftest.pytest_runtest_protocol(item, None)

    assert next(protocol) is None
    assert conftest._LIVE_DEFAULT_DB_ACCESS_NODE_ID is None
    with pytest.raises(RuntimeError, match="before driver connect"):
        conftest._CONTROL_DB_CONNECT_GUARD()
    with pytest.raises(StopIteration):
        next(protocol)


def test_global_test_blacklist_is_process_local() -> None:
    """Test imports have no Redis or Sentry endpoint configured."""
    assert os.environ["REDIS_URL"] == ""
    assert os.environ["SENTRY_DSN"] == ""
    assert isinstance(token_blacklist, _InMemoryBlacklist)


def test_blacklist_cleanup_never_scans_or_deletes_redis_keys() -> None:
    """Fixture cleanup leaves any shared Redis namespace untouched."""
    redis_backend = object.__new__(_RedisBlacklist)
    redis_client = Mock()
    redis_backend._redis = redis_client

    conftest._clear_blacklist(redis_backend)

    assert redis_client.mock_calls == []
