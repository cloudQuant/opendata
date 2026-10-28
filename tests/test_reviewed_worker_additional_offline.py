"""Cover reviewed-worker safety boundaries without opening database connections."""

from __future__ import annotations

import socket
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool, QueuePool

from opendata.pipeline import reviewed_worker as reviewed_worker_module
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    create_mysql_worker_engine,
)
from opendata.pipeline.run_service import ReviewedJobRegistry

_SERVER_UUID = "11111111-2222-3333-4444-555555555555"
_SCHEMA = "od204_offline_worker"
_DATABASE_IO_TIMEOUT = 1.0
_DRIVER_TIMEOUT = 0.5

if TYPE_CHECKING:
    from pathlib import Path


class _FakeMysqlResult:
    """Expose the read-only identity row returned by MySQL preflight."""

    def one(self) -> tuple[str, str]:
        """Return a stable synthetic server and schema identity."""
        return _SERVER_UUID, _SCHEMA


class _FakeMysqlConnection:
    """Model only the DBAPI attributes inspected by the worker guard."""

    def __init__(self, engine: Engine, unix_socket_path: Path) -> None:
        """Bind synthetic identity and timeout state to one Engine."""
        self.engine = engine
        self.transaction_open = False
        self.rollback_count = 0
        self.connection = SimpleNamespace(
            driver_connection=SimpleNamespace(
                unix_socket=str(unix_socket_path),
                connect_timeout=_DRIVER_TIMEOUT,
                _read_timeout=_DRIVER_TIMEOUT,
                _write_timeout=_DRIVER_TIMEOUT,
                _sock=SimpleNamespace(
                    family=socket.AF_UNIX,
                    gettimeout=lambda: _DRIVER_TIMEOUT,
                ),
            )
        )

    def __enter__(self) -> _FakeMysqlConnection:
        """Support the Engine checkout context used by the factory."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Release the fake checkout without external resources."""

    def in_transaction(self) -> bool:
        """Report whether the synthetic read has opened a transaction."""
        return self.transaction_open

    def exec_driver_sql(self, statement: str) -> _FakeMysqlResult:
        """Model MySQL's identity read and its transaction side effect."""
        assert statement == "SELECT @@server_uuid, DATABASE()"
        self.transaction_open = True
        return _FakeMysqlResult()

    def rollback(self) -> None:
        """Record a rollback and close the synthetic transaction."""
        self.rollback_count += 1
        self.transaction_open = False


def _private_socket(tmp_path: Path) -> tuple[Path, socket.socket]:
    """Create an owner-only directory containing a bound local socket."""
    socket_directory = tmp_path / "mysql-socket"
    socket_directory.mkdir(mode=0o700)
    socket_directory.chmod(0o700)
    socket_path = socket_directory / "mysql.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    return socket_path, listener


def _install_synthetic_mysql_engine(
    monkeypatch: pytest.MonkeyPatch,
    socket_path: Path,
) -> tuple[
    list[Engine],
    list[_FakeMysqlConnection],
    list[dict[str, Any]],
    list[Engine],
]:
    """Replace SQLAlchemy checkouts with a fake while keeping a real lazy Engine."""
    engines: list[Engine] = []
    connections: list[_FakeMysqlConnection] = []
    arguments_seen: list[dict[str, Any]] = []
    disposed_engines: list[Engine] = []

    def create_engine_without_connect(database_url: str | URL, **kwargs: Any) -> Engine:
        engine = sqlalchemy_create_engine(database_url, **kwargs)
        connection = _FakeMysqlConnection(engine, socket_path)
        original_dispose = engine.dispose

        def tracked_dispose(close: bool = True) -> None:
            disposed_engines.append(engine)
            original_dispose(close=close)

        def fake_connect() -> _FakeMysqlConnection:
            connections.append(connection)
            return connection

        engine.connect = fake_connect  # type: ignore[method-assign]
        engine.dispose = tracked_dispose  # type: ignore[method-assign]
        engines.append(engine)
        arguments_seen.append(dict(kwargs))
        return engine

    monkeypatch.setattr(reviewed_worker_module, "create_engine", create_engine_without_connect)
    return engines, connections, arguments_seen, disposed_engines


def _mysql_url() -> URL:
    """Return an offline-only URL; the test substitutes every physical checkout."""
    return URL.create(
        "mysql+pymysql",
        username="fixture-worker",
        password="fixture-password",
        host="localhost",
        database=_SCHEMA,
    )


def _factory_options(socket_path: Path) -> dict[str, object]:
    """Return bounded worker options for a synthetic local-socket target."""
    return {
        "expected_server_uuid": _SERVER_UUID,
        "expected_schema": _SCHEMA,
        "unix_socket_path": socket_path,
        "database_io_timeout_seconds": _DATABASE_IO_TIMEOUT,
        "connect_timeout_seconds": _DRIVER_TIMEOUT,
        "read_timeout_seconds": _DRIVER_TIMEOUT,
        "write_timeout_seconds": _DRIVER_TIMEOUT,
    }


def test_mysql_factory_binds_synthetic_local_socket_and_worker_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin factory, worker, and transaction checks without connecting to MySQL."""
    socket_path, listener = _private_socket(tmp_path)
    engines, connections, arguments_seen, disposed_engines = _install_synthetic_mysql_engine(
        monkeypatch, socket_path
    )
    try:
        engine = create_mysql_worker_engine(_mysql_url(), **_factory_options(socket_path))

        assert engines == [engine]
        assert arguments_seen[0]["connect_args"]["unix_socket"] == str(socket_path)
        binding = reviewed_worker_module._require_worker_engine_binding(
            engine, _DATABASE_IO_TIMEOUT
        )
        assert isinstance(binding, reviewed_worker_module._MySQLWorkerEngineBinding)
        assert binding.identity.server_uuid == _SERVER_UUID
        assert binding.identity.schema == _SCHEMA

        worker = PersistentRunWorker(
            engine,
            ReviewedJobRegistry(),
            worker_id="offline-worker",
            lease_ttl=timedelta(seconds=5),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
        )
        assert isinstance(worker._connection_guard, reviewed_worker_module._MySQLConnectionGuard)
        assert connections
        assert disposed_engines == []

        checkout = _FakeMysqlConnection(engine, socket_path)
        worker._connection_guard.validate(checkout)  # type: ignore[arg-type]
        assert checkout.rollback_count == 1
        assert checkout.transaction_open is False

        commit = _FakeMysqlConnection(engine, socket_path)
        commit.transaction_open = True
        worker._connection_guard.validate_commit(commit)  # type: ignore[arg-type]
        assert commit.rollback_count == 0
        assert commit.transaction_open is True
    finally:
        for engine in engines:
            engine.dispose()
        listener.close()


def test_mysql_factory_disposes_synthetic_engine_after_preflight_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed identity preflight disposes its pool and keeps only the safe error."""
    socket_path, listener = _private_socket(tmp_path)
    engines, _connections, _arguments_seen, disposed_engines = _install_synthetic_mysql_engine(
        monkeypatch, socket_path
    )

    def reject_identity(*_args: object, **_kwargs: object) -> None:
        raise ValueError("reviewed server/schema identity mismatch")

    monkeypatch.setattr(reviewed_worker_module, "_verify_mysql_connection", reject_identity)
    try:
        with pytest.raises(ValueError, match="reviewed server/schema identity mismatch"):
            create_mysql_worker_engine(_mysql_url(), **_factory_options(socket_path))

        engine = engines[0]
        assert disposed_engines == [engine]
        assert engine.pool.checkedout() == 0
    finally:
        for engine in engines:
            engine.dispose()
        listener.close()


def test_mysql_factory_rejects_non_url_before_creating_an_engine(tmp_path: Path) -> None:
    """Reject unsupported URL input before transport or pool setup."""
    socket_path, listener = _private_socket(tmp_path)
    try:
        with pytest.raises(TypeError, match="string or SQLAlchemy URL"):
            create_mysql_worker_engine(object(), **_factory_options(socket_path))  # type: ignore[arg-type]
    finally:
        listener.close()


@pytest.mark.parametrize(
    ("main_file", "busy_timeout", "expected_error"),
    [
        ("/different/warehouse.sqlite", 100, "does not target the configured"),
        ("__expected_main_file__", True, "busy_timeout has an invalid value"),
    ],
)
def test_sqlite_checkout_guard_rejects_wrong_target_and_noninteger_timeout(
    tmp_path: Path,
    main_file: str,
    busy_timeout: object,
    expected_error: str,
) -> None:
    """Reject a disguised SQLite target and boolean timeout values."""
    database_path = tmp_path / "owned.sqlite"
    database_path.touch()
    file_stat = database_path.stat()
    expected = reviewed_worker_module._SQLiteFileIdentity(
        database_path,
        file_stat.st_dev,
        file_stat.st_ino,
    )

    class _Result:
        def all(self) -> list[tuple[int, str, str]]:
            observed_file = (
                str(database_path) if main_file == "__expected_main_file__" else main_file
            )
            return [(0, "main", observed_file)]

        def scalar_one(self) -> object:
            return busy_timeout

    class _Connection:
        def exec_driver_sql(self, statement: str) -> _Result:
            if statement not in {"PRAGMA database_list", "PRAGMA busy_timeout"}:
                raise AssertionError("unexpected SQLite probe")
            return _Result()

    with pytest.raises(ValueError, match=expected_error):
        reviewed_worker_module._verify_actual_sqlite_connection(
            _Connection(),  # type: ignore[arg-type]
            expected_database=expected,
            maximum_wait_seconds=1.0,
        )


def test_sqlite_checkout_guard_sanitizes_driver_probe_errors(tmp_path: Path) -> None:
    """Do not expose driver details when the physical-file probe fails."""
    database_path = tmp_path / "owned.sqlite"
    database_path.touch()
    file_stat = database_path.stat()
    expected = reviewed_worker_module._SQLiteFileIdentity(
        database_path,
        file_stat.st_dev,
        file_stat.st_ino,
    )

    class _BrokenConnection:
        def exec_driver_sql(self, _statement: str) -> object:
            raise RuntimeError("private driver detail")

    with pytest.raises(ValueError, match="SQLite database connection could not be verified") as exc:
        reviewed_worker_module._verify_actual_sqlite_connection(
            _BrokenConnection(),  # type: ignore[arg-type]
            expected_database=expected,
            maximum_wait_seconds=1.0,
        )
    assert "private driver detail" not in str(exc.value)


def test_worker_pool_guard_rejects_unbounded_pool_and_creator_drift() -> None:
    """Require the worker's exact bounded QueuePool configuration."""
    null_pool_engine = sqlalchemy_create_engine("sqlite://", poolclass=NullPool)
    queue_pool_engine = sqlalchemy_create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=1,
        pool_timeout=0.5,
    )
    try:
        with pytest.raises(ValueError, match="standard bounded SQLite QueuePool"):
            reviewed_worker_module._verify_pool_wait_bound(null_pool_engine, 1.0)
        with pytest.raises(ValueError, match="creator changed"):
            reviewed_worker_module._verify_pool_wait_bound(
                queue_pool_engine,
                1.0,
                expected_creator=object(),
            )
        with pytest.raises(ValueError, match="one-slot SQLite QueuePool"):
            reviewed_worker_module._verify_pool_wait_bound(queue_pool_engine, 1.0)
    finally:
        null_pool_engine.dispose()
        queue_pool_engine.dispose()


def test_worker_process_owner_and_heartbeat_catalog_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject inherited ownership and a registry whose private catalog is malformed."""
    monkeypatch.setattr(reviewed_worker_module.os, "getpid", lambda: 4242)
    with pytest.raises(ValueError, match="cannot be reused across process boundaries"):
        reviewed_worker_module._verify_process_owner(3131)

    registry = ReviewedJobRegistry()
    monkeypatch.setattr(registry, "_jobs", None)
    with pytest.raises(TypeError, match="frozen job catalog"):
        reviewed_worker_module._verify_registry_heartbeat_intervals(registry, 0.05)
