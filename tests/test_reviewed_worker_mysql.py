"""Owned-MySQL acceptance and offline validation for the reviewed worker."""

from __future__ import annotations

import os
import socket
import ssl
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import URL

from opendata.pipeline import reviewed_worker as reviewed_worker_module
from opendata.pipeline.process_executor import (
    ReviewedHandler,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    _guard_mysql_pool_creator,
    _MySQLDriverTimeouts,
    _MySQLServerSchemaIdentity,
    _validated_mysql_identity,
    _validated_mysql_transport,
    _verify_mysql_connection,
    _verify_mysql_transport,
    create_mysql_worker_engine,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

pytest_plugins = ("tests.test_afi204_owned_mysql",)

_SERVER_UUID = "11111111-2222-3333-4444-555555555555"
_SCHEMA = "od204_test_fixture"
_DATABASE_IO_TIMEOUT = 1.0
_TIMEOUTS = _MySQLDriverTimeouts(connect=0.5, read=0.5, write=0.5)


class _FakeMysqlResult:
    """Return one deterministic identity row for driver-boundary tests."""

    def __init__(self, row: tuple[str, str]) -> None:
        self._row = row

    def one(self) -> tuple[str, str]:
        """Return the configured database identity row."""
        return self._row


class _FakeMysqlSocket:
    """Expose the current socket timeout as PyMySQL does."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout

    def gettimeout(self) -> float:
        """Return the current socket timeout."""
        return self._timeout


class _FakeMysqlConnection:
    """Minimal PyMySQL connection surface used by identity guard tests."""

    def __init__(self, *, read_timeout: float = 0.5) -> None:
        self.connection = SimpleNamespace(
            driver_connection=SimpleNamespace(
                connect_timeout=0.5,
                _read_timeout=read_timeout,
                _write_timeout=0.5,
                _sock=_FakeMysqlSocket(read_timeout),
            )
        )
        self.statements: list[str] = []
        self.row = (_SERVER_UUID, _SCHEMA)

    def exec_driver_sql(self, statement: str) -> _FakeMysqlResult:
        """Record SQL and return the synthetic SELECT identity row."""
        self.statements.append(statement)
        return _FakeMysqlResult(self.row)


def _offline_url(
    *,
    schema: str = _SCHEMA,
    query: dict[str, str] | None = None,
    host: str = "mysql.invalid",
    port: int | None = 3306,
) -> URL:
    """Build a non-routable explicit URL without consulting the environment."""
    return URL.create(
        "mysql+pymysql",
        username="worker",
        password="canary-password-value",
        host=host,
        port=port,
        database=schema,
        query=query or {},
    )


def _assert_factory_rejects(url: str | URL, **overrides: object) -> ValueError:
    """Run URL/timeout validation that must fail before opening a connection."""
    parameters: dict[str, object] = {
        "expected_server_uuid": _SERVER_UUID,
        "expected_schema": _SCHEMA,
        "database_io_timeout_seconds": _DATABASE_IO_TIMEOUT,
        "connect_timeout_seconds": _TIMEOUTS.connect,
        "read_timeout_seconds": _TIMEOUTS.read,
        "write_timeout_seconds": _TIMEOUTS.write,
    }
    parameters.update(overrides)
    with pytest.raises(ValueError) as raised:
        create_mysql_worker_engine(url, **parameters)  # type: ignore[arg-type]
    assert "canary-password-value" not in str(raised.value)
    return raised.value


def _owned_mysql_fetch(_parameters: Mapping[str, JsonValue]) -> JsonValue:
    """Return deterministic JSON for the real MySQL worker acceptance path."""
    return {"ok": True}


def _owned_mysql_fetch_with_secret_failure(_parameters: Mapping[str, JsonValue]) -> JsonValue:
    """Raise a credential-bearing provider failure to verify ledger redaction."""
    raise RuntimeError(
        "mysql+pymysql://worker:credential-canary@secret-host/db?token=secret-canary"
    )


def _normalize_mysql_fixture(
    raw: object,
    _request: RunRequest,
    _context: RunExecutionContext,
) -> NormalizedBatch:
    """Validate child JSON and produce a zero-row publication batch."""
    if raw != {"ok": True}:
        raise ValueError("mysql_fixture_payload_invalid")
    return NormalizedBatch(
        records=(),
        schema_version="1",
        semantic_version="mysql-worker-fixture-v1",
        unit="none",
        adjustment_basis="not_applicable",
        quality={"rejected_rows": 0},
        provenance={"source": "owned-mysql-fixture"},
    )


def _publish_mysql_fixture(
    _connection: Connection,
    task: TaskDefinition,
    _request: RunRequest,
    batch: NormalizedBatch,
    _idempotency_key: str,
) -> PublicationReceipt:
    """Return a receipt after the ledger's real fenced MySQL commit."""
    return PublicationReceipt("owned-mysql-fixture", task.dataset_id, len(batch.records))


def _mysql_job(
    *,
    operation_id: str = "owned-mysql-worker-fixture",
    handler: ReviewedHandler = _owned_mysql_fetch,
) -> ReviewedJob:
    """Build a fixed process-backed job for the owned database acceptance test."""
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({operation_id: handler}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.05,
    )
    fetch = ProcessFetchHandler(
        executor,
        operation_id,
        lambda request: dict(request.query),
        lambda value: value,
        timeout_seconds=5,
    )
    task = TaskDefinition(
        job_id=operation_id,
        version="v1",
        dataset_id=f"{operation_id}-dataset",
        provider="fixture",
        model="fixture-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        enabled=True,
    )
    return ReviewedJob(
        task,
        fetch,
        _normalize_mysql_fixture,
        _publish_mysql_fixture,
        reviewed=True,
    )


def _reviewed_mysql_engine(owned_mysql: Engine) -> Engine:
    """Bind the existing randomly owned schema to its observed server UUID."""
    with owned_mysql.connect() as connection:
        server_uuid, schema = connection.exec_driver_sql("SELECT @@server_uuid, DATABASE()").one()
    return create_mysql_worker_engine(
        owned_mysql.url.set(query={}),
        expected_server_uuid=str(server_uuid),
        expected_schema=str(schema),
        database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
        connect_timeout_seconds=_TIMEOUTS.connect,
        read_timeout_seconds=_TIMEOUTS.read,
        write_timeout_seconds=_TIMEOUTS.write,
        unix_socket_path=os.environ["OD204_MYSQL_SOCKET"],
    )


def _record_owned_mysql_schema(owned_mysql: Engine) -> None:
    """Append the random test-owned schema for cleanup evidence when requested."""
    receipt_path = os.environ.get("OD204_MYSQL_SCHEMA_LOG")
    if receipt_path is not None:
        with Path(receipt_path).open("a", encoding="utf-8") as receipt:
            receipt.write(f"{owned_mysql.url.database}\n")


def test_mysql_identity_validator_requires_canonical_server_and_schema() -> None:
    """Reviewed identity values are strict, bounded, and normalized pre-connect."""
    identity = _validated_mysql_identity(_SERVER_UUID.upper(), _SCHEMA)
    assert identity == _MySQLServerSchemaIdentity(_SERVER_UUID, _SCHEMA)
    for bad_uuid in ("", "mysql.invalid", "1" * 37, f"{{{_SERVER_UUID}}}"):
        with pytest.raises(ValueError, match="canonical UUID"):
            _validated_mysql_identity(bad_uuid, _SCHEMA)
    for bad_schema in ("", "has space", "schema;DROP", "x" * 65):
        with pytest.raises(ValueError, match="database identifier"):
            _validated_mysql_identity(_SERVER_UUID, bad_schema)
    with pytest.raises(TypeError, match="expected_server_uuid"):
        _validated_mysql_identity(object(), _SCHEMA)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected_schema"):
        _validated_mysql_identity(_SERVER_UUID, object())  # type: ignore[arg-type]


def test_mysql_transport_rejects_plain_tcp_and_unverified_tls_before_connect(
    tmp_path: Path,
) -> None:
    """Only validated TLS or a pinned private local socket may reach Engine setup."""
    remote_url = _offline_url()
    with pytest.raises(ValueError, match="plain MySQL TCP is disabled"):
        _validated_mysql_transport(remote_url, None, None)

    unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="certificate and hostname verification"):
        _validated_mysql_transport(remote_url, unverified, None)

    verified = ssl.create_default_context()
    tls_binding = _validated_mysql_transport(remote_url, verified, None)
    assert tls_binding.kind == "tls" and tls_binding.ssl_context is verified
    with pytest.raises(TypeError, match="ssl_context"):
        _validated_mysql_transport(remote_url, object(), None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="mutually exclusive"):
        _validated_mysql_transport(remote_url, verified, tmp_path / "mysql.sock")

    private_dir = tmp_path / "mysql-private"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    socket_path = private_dir / "mysql.sock"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(socket_path))
        local_url = _offline_url(host="localhost", port=None)
        binding = _validated_mysql_transport(local_url, None, socket_path)
        assert binding.kind == "unix_socket"
        assert binding.unix_socket is not None
        assert binding.unix_socket.path == socket_path
        with pytest.raises(TypeError, match="unix_socket_path"):
            _validated_mysql_transport(local_url, None, object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must be absolute"):
            _validated_mysql_transport(local_url, None, Path("relative.sock"))
        with pytest.raises(ValueError, match="localhost URL"):
            _validated_mysql_transport(remote_url, None, socket_path)
        with pytest.raises(ValueError, match="without a port"):
            _validated_mysql_transport(local_url.set(port=3306), None, socket_path)
        symlink = private_dir / "alias.sock"
        symlink.symlink_to(socket_path)
        with pytest.raises(ValueError, match="canonical and identify a socket"):
            _validated_mysql_transport(local_url, None, symlink)
        private_dir.chmod(0o755)
        with pytest.raises(ValueError, match="private and owned"):
            _validated_mysql_transport(local_url, None, socket_path)
    finally:
        listener.close()


def test_mysql_unix_transport_guard_checks_active_socket_and_identity(tmp_path: Path) -> None:
    """The guard rejects a changed socket path or a non-Unix active connection."""
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    private_dir.chmod(0o700)
    socket_path = private_dir / "mysql.sock"
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(str(socket_path))
        binding = _validated_mysql_transport(
            _offline_url(host="localhost", port=None), None, socket_path
        )
        fake = _FakeMysqlConnection()
        driver_connection = fake.connection.driver_connection
        driver_connection.unix_socket = str(socket_path)
        driver_connection._sock.family = socket.AF_UNIX
        _verify_mysql_transport(fake, binding)  # type: ignore[arg-type]

        driver_connection._sock.family = socket.AF_INET
        with pytest.raises(ValueError, match="private Unix socket"):
            _verify_mysql_transport(fake, binding)  # type: ignore[arg-type]
        driver_connection._sock.family = socket.AF_UNIX
        socket_path.unlink()
        replacement = private_dir / "replacement.sock"
        replacement_listener = socket.socket(socket.AF_UNIX)
        try:
            replacement_listener.bind(str(replacement))
            replacement.rename(socket_path)
            with pytest.raises(ValueError, match="identity changed"):
                _verify_mysql_transport(fake, binding)  # type: ignore[arg-type]
        finally:
            replacement_listener.close()
    finally:
        listener.close()


def test_mysql_transport_guard_rejects_tls_policy_drift_and_unreadable_objects() -> None:
    """TLS trust cannot be disabled after review; broken connections fail closed."""
    context = ssl.create_default_context()
    binding = _validated_mysql_transport(_offline_url(), context, None)
    connection_attempts: list[bool] = []
    creator = _guard_mysql_pool_creator(lambda: connection_attempts.append(True), binding)
    context.check_hostname = False
    with pytest.raises(ValueError, match="verification policy changed"):
        creator()
    assert connection_attempts == []
    with pytest.raises(ValueError, match="verification policy changed"):
        _verify_mysql_transport(_FakeMysqlConnection(), binding)  # type: ignore[arg-type]

    secure_context = ssl.create_default_context()
    secure_binding = _validated_mysql_transport(_offline_url(), secure_context, None)

    class BrokenConnection:
        @property
        def connection(self) -> object:
            raise RuntimeError("secret database detail")

    with pytest.raises(ValueError, match="transport could not be verified") as raised:
        _verify_mysql_transport(BrokenConnection(), secure_binding)  # type: ignore[arg-type]
    assert "secret database detail" not in str(raised.value)


def test_mysql_factory_rejects_unsafe_url_target_and_timeout_before_connect() -> None:
    """Malformed or mismatched engine settings fail before any DNS/network I/O."""
    assert "reviewed PyMySQL server schema" in str(
        _assert_factory_rejects(_offline_url(schema="other-schema"))
    )
    assert "reviewed PyMySQL server schema" in str(
        _assert_factory_rejects(_offline_url(query={"charset": "utf8mb4"}))
    )
    assert "reviewed PyMySQL server schema" in str(
        _assert_factory_rejects(URL.create("mysql+mysqldb", host="mysql.invalid", database=_SCHEMA))
    )
    assert "reviewed PyMySQL server schema" in str(
        _assert_factory_rejects(URL.create("sqlite", database=":memory:"))
    )
    assert "reviewed PyMySQL server schema" in str(
        _assert_factory_rejects(URL.create("mysql+pymysql", host="mysql.invalid", database=_SCHEMA))
    )
    assert "exceed database_io_timeout_seconds" in str(
        _assert_factory_rejects(_offline_url(), read_timeout_seconds=2)
    )
    assert "database_io_timeout_seconds" in str(
        _assert_factory_rejects(_offline_url(), database_io_timeout_seconds=float("inf"))
    )
    with pytest.raises(ValueError, match="plain MySQL TCP is disabled"):
        create_mysql_worker_engine(
            _offline_url(),
            expected_server_uuid=_SERVER_UUID,
            expected_schema=_SCHEMA,
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
            connect_timeout_seconds=_TIMEOUTS.connect,
            read_timeout_seconds=_TIMEOUTS.read,
            write_timeout_seconds=_TIMEOUTS.write,
        )


def test_mysql_factory_sanitizes_invalid_urls_and_engine_creation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed URLs and adapter setup errors never expose credentials."""
    arguments = {
        "expected_server_uuid": _SERVER_UUID,
        "expected_schema": _SCHEMA,
        "ssl_context": ssl.create_default_context(),
        "database_io_timeout_seconds": _DATABASE_IO_TIMEOUT,
        "connect_timeout_seconds": _TIMEOUTS.connect,
        "read_timeout_seconds": _TIMEOUTS.read,
        "write_timeout_seconds": _TIMEOUTS.write,
    }
    with pytest.raises(ValueError, match="reviewed PyMySQL server schema"):
        create_mysql_worker_engine("mysql+pymysql://worker:canary@[", **arguments)

    make_url = reviewed_worker_module.make_url

    def fail_parse(_url: str | URL) -> URL:
        raise RuntimeError("canary-url-secret")

    monkeypatch.setattr(reviewed_worker_module, "make_url", fail_parse)
    with pytest.raises(ValueError, match="MySQL worker URL is invalid") as parse_error:
        create_mysql_worker_engine(_offline_url(), **arguments)
    assert "canary-url-secret" not in str(parse_error.value)
    monkeypatch.setattr(reviewed_worker_module, "make_url", make_url)

    def fail_creation(*_args: object, **_kwargs: object) -> Engine:
        raise RuntimeError("canary-engine-secret")

    monkeypatch.setattr(reviewed_worker_module, "create_engine", fail_creation)
    with pytest.raises(ValueError, match=r"Engine creation failed \(RuntimeError\)") as raised:
        create_mysql_worker_engine(_offline_url(), **arguments)
    assert "canary-engine-secret" not in str(raised.value)


def test_mysql_factory_sanitizes_connection_preflight_and_rejects_wrong_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed live preflight is sanitized and uncontrolled dialects are disposed."""
    from sqlalchemy.pool import NullPool

    arguments = {
        "expected_server_uuid": _SERVER_UUID,
        "expected_schema": _SCHEMA,
        "ssl_context": ssl.create_default_context(),
        "database_io_timeout_seconds": _DATABASE_IO_TIMEOUT,
        "connect_timeout_seconds": _TIMEOUTS.connect,
        "read_timeout_seconds": _TIMEOUTS.read,
        "write_timeout_seconds": _TIMEOUTS.write,
    }
    real_engine = create_engine(_offline_url())

    def fail_connect() -> Engine:
        raise RuntimeError("canary-connect-secret")

    real_engine.connect = fail_connect  # type: ignore[method-assign]
    monkeypatch.setattr(
        reviewed_worker_module, "create_engine", lambda *_args, **_kwargs: real_engine
    )
    with pytest.raises(ValueError, match=r"preflight failed \(RuntimeError\)") as raised:
        create_mysql_worker_engine(_offline_url(), **arguments)
    assert "canary-connect-secret" not in str(raised.value)
    real_engine.dispose()

    wrong_pool = create_engine(_offline_url(), poolclass=NullPool)
    monkeypatch.setattr(
        reviewed_worker_module, "create_engine", lambda *_args, **_kwargs: wrong_pool
    )
    with pytest.raises(ValueError, match="controlled PyMySQL QueuePool"):
        create_mysql_worker_engine(_offline_url(), **arguments)


def test_mysql_connection_guard_uses_read_only_identity_select_and_timeout_state() -> None:
    """Guard checks the real identity query and every configured client timeout."""
    connection = _FakeMysqlConnection()

    _verify_mysql_connection(
        connection,  # type: ignore[arg-type]
        _MySQLServerSchemaIdentity(_SERVER_UUID, _SCHEMA),
        _TIMEOUTS,
    )

    assert connection.statements == ["SELECT @@server_uuid, DATABASE()"]


def test_mysql_connection_guard_rejects_target_and_driver_timeout_drift() -> None:
    """Wrong server/schema or widened socket timeout cannot pass preflight."""
    wrong_target = _FakeMysqlConnection()
    wrong_target.row = (_SERVER_UUID, "different-schema")
    with pytest.raises(ValueError, match="reviewed server/schema identity"):
        _verify_mysql_connection(
            wrong_target,  # type: ignore[arg-type]
            _MySQLServerSchemaIdentity(_SERVER_UUID, _SCHEMA),
            _TIMEOUTS,
        )

    wrong_timeout = _FakeMysqlConnection(read_timeout=2)
    with pytest.raises(ValueError, match="driver timeout changed"):
        _verify_mysql_connection(
            wrong_timeout,  # type: ignore[arg-type]
            _MySQLServerSchemaIdentity(_SERVER_UUID, _SCHEMA),
            _TIMEOUTS,
        )


def test_unregistered_mysql_engine_cannot_be_adopted_without_network() -> None:
    """A regular SQLAlchemy MySQL Engine cannot bypass reviewed factory binding."""
    engine = create_engine(_offline_url())
    try:
        with pytest.raises(ValueError, match="create_mysql_worker_engine"):
            PersistentRunWorker(
                engine,
                ReviewedJobRegistry(),
                worker_id="unregistered-mysql",
                lease_ttl=timedelta(seconds=4),
                heartbeat_interval_seconds=0.05,
                database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
            )
    finally:
        engine.dispose()


def test_run_ledger_migration_compiles_as_expected_mysql_ddl() -> None:
    """The frozen warehouse migration emits key collations and claim index offline."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.dialects import mysql

    from tests.test_afi204_owned_mysql import _run_ledger_revision

    output = StringIO()
    context = MigrationContext.configure(
        dialect=mysql.dialect(),
        opts={"as_sql": True, "output_buffer": output},
    )
    revision = _run_ledger_revision()
    with Operations.context(context):
        revision.upgrade()

    ddl = output.getvalue()
    assert "CREATE TABLE od_run_ledger" in ddl
    assert "job_snapshot_json MEDIUMTEXT NOT NULL" in ddl
    assert "run_key VARCHAR(255) COLLATE utf8mb4_0900_bin NOT NULL" in ddl
    assert "ix_od_run_ledger_claim ON od_run_ledger" in ddl
    assert "CREATE DATABASE" not in ddl


def test_owned_mysql_factory_is_read_only_and_worker_commits_through_database_clock(
    owned_mysql: Engine,
) -> None:
    """Only the owned random schema is migrated; a reviewed worker completes a run."""
    _record_owned_mysql_schema(owned_mysql)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.test_afi204_owned_mysql import _run_ledger_revision

    assert inspect(owned_mysql).get_table_names() == []
    engine = _reviewed_mysql_engine(owned_mysql)
    try:
        assert inspect(engine).get_table_names() == []
        task = _mysql_job()
        worker = PersistentRunWorker(
            engine,
            ReviewedJobRegistry([task]),
            worker_id="owned-mysql-worker",
            lease_ttl=timedelta(seconds=5),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
        )
        assert inspect(engine).get_table_names() == []

        revision = _run_ledger_revision()
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            revision.upgrade()
        queued = worker.submit(
            task.task.job_id,
            task.task.version,
            RunRequest(
                run_type="manual",
                query={"fixture": "owned-mysql"},
                idempotency_key="owned-mysql-worker-run",
            ),
        )
        with pytest.MonkeyPatch.context() as child_environment:
            child_environment.delenv("OD204_MYSQL_ADMIN_URL", raising=False)
            outcome = worker.run_once()

        assert queued.status == "queued"
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "succeeded"
        assert outcome.record.created_at.year >= 2026
        assert outcome.publication == PublicationReceipt(
            "owned-mysql-fixture", task.task.dataset_id, 0
        )

        failing_task = _mysql_job(
            operation_id="owned-mysql-secret-failure",
            handler=_owned_mysql_fetch_with_secret_failure,
        )
        failure_worker = PersistentRunWorker(
            engine,
            ReviewedJobRegistry([failing_task]),
            worker_id="owned-mysql-failure-worker",
            lease_ttl=timedelta(seconds=5),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
        )
        failure_request = RunRequest(
            run_type="manual",
            query={"fixture": "owned-mysql-failure"},
            idempotency_key="owned-mysql-worker-failure",
        )
        failure_worker.submit(failing_task.task.job_id, failing_task.task.version, failure_request)
        with pytest.MonkeyPatch.context() as child_environment:
            child_environment.delenv("OD204_MYSQL_ADMIN_URL", raising=False)
            failure = failure_worker.run_once()
        assert failure is not None and failure.record is not None
        assert failure.record.status == "failed"
        assert failure.record.error_text == "execution_failed:ProcessExecutionHandlerError"
        assert "credential-canary" not in failure.record.error_text
        assert "secret-canary" not in failure.record.error_text
        assert "mysql+pymysql://" not in failure.record.error_text
        assert failure_worker.ledger.get(failure.record.run_key) == failure.record
        assert inspect(engine).get_table_names() == ["od_run_ledger"]
    finally:
        engine.dispose()


def test_owned_mysql_factory_rejects_wrong_reviewed_server_uuid(owned_mysql: Engine) -> None:
    """A real but wrong UUID is rejected without changing the owned schema."""
    _record_owned_mysql_schema(owned_mysql)
    with owned_mysql.connect() as connection:
        actual_uuid, schema = connection.exec_driver_sql("SELECT @@server_uuid, DATABASE()").one()
    wrong_uuid = str(uuid4())
    assert wrong_uuid != actual_uuid
    with pytest.raises(ValueError, match="reviewed server/schema identity"):
        create_mysql_worker_engine(
            owned_mysql.url.set(query={}),
            expected_server_uuid=wrong_uuid,
            expected_schema=str(schema),
            unix_socket_path=os.environ["OD204_MYSQL_SOCKET"],
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
            connect_timeout_seconds=_TIMEOUTS.connect,
            read_timeout_seconds=_TIMEOUTS.read,
            write_timeout_seconds=_TIMEOUTS.write,
        )
    assert inspect(owned_mysql).get_table_names() == []


def test_owned_mysql_rechecks_driver_socket_timeout_on_every_checkout(
    owned_mysql: Engine,
) -> None:
    """A checkout listener cannot widen PyMySQL read timeout after construction."""
    _record_owned_mysql_schema(owned_mysql)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from tests.test_afi204_owned_mysql import _run_ledger_revision

    engine = _reviewed_mysql_engine(owned_mysql)
    try:
        revision = _run_ledger_revision()
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            revision.upgrade()
        worker = PersistentRunWorker(
            engine,
            ReviewedJobRegistry(),
            worker_id="owned-mysql-timeout-worker",
            lease_ttl=timedelta(seconds=5),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=_DATABASE_IO_TIMEOUT,
        )

        def widen_driver_timeout(dbapi_connection: object, _record: object, _proxy: object) -> None:
            connection = dbapi_connection
            connection._read_timeout = 2  # type: ignore[attr-defined]
            connection._sock.settimeout(2)  # type: ignore[attr-defined]

        event.listen(engine.pool, "checkout", widen_driver_timeout)
        with pytest.raises(ValueError, match="driver timeout changed"):
            worker.run_once()
        event.remove(engine.pool, "checkout", widen_driver_timeout)
    finally:
        engine.dispose()
