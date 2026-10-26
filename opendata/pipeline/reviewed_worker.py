"""Run persisted reviewed jobs with parent-owned leases and killable fetches.

This module composes the durable ledger, controlled run service, and reviewed
JSON process executor. Database access, lease renewal, normalization, and
publication remain in the worker process. Only a fixed operation identifier
and plain JSON parameters cross into the provider child process.
"""

from __future__ import annotations

import math
import os
import re
import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISREG, S_ISSOCK
from threading import Lock
from typing import TYPE_CHECKING, Literal, TypeAlias
from uuid import UUID
from weakref import WeakKeyDictionary

from pydantic import JsonValue
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.pool import QueuePool

from opendata.pipeline.process_executor import ReviewedProcessExecutor
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_ledger import MAX_TTL, MIN_TTL, RunLedger
from opendata.pipeline.run_service import (
    ReviewedJobRegistry,
    RunExecutionContext,
    RunOutcome,
    RunService,
)

if TYPE_CHECKING:  # pragma: no cover
    from opendata.pipeline.run_ledger import RunRecord

JsonParameters: TypeAlias = Mapping[str, JsonValue]
ParameterBuilder: TypeAlias = Callable[[RunRequest], JsonParameters]
ResultDecoder: TypeAlias = Callable[[JsonValue], object]

_MIN_NUMBER = 0.01
_MAX_HEARTBEAT_INTERVAL_SECONDS = 60.0
_MAX_DATABASE_IO_TIMEOUT_SECONDS = 30.0
_MIN_LEASE_MARGIN_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class _SQLiteFileIdentity:
    """Canonical path and inode captured for one worker warehouse."""

    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _SQLiteWorkerEngineBinding:
    """Factory-only SQLite Engine identity and bounded connection configuration."""

    database: _SQLiteFileIdentity
    database_io_timeout_seconds: float
    pool_creator: object
    owner_process_id: int


@dataclass(frozen=True, slots=True)
class _MySQLServerSchemaIdentity:
    """Explicitly reviewed MySQL server and schema target."""

    server_uuid: str
    schema: str


@dataclass(frozen=True, slots=True)
class _MySQLDriverTimeouts:
    """Driver connect and socket I/O timeouts in seconds."""

    connect: float
    read: float
    write: float


@dataclass(frozen=True, slots=True)
class _UnixSocketIdentity:
    """Physical identity of a private MySQL socket and its parent directory."""

    path: Path
    device: int
    inode: int
    parent: Path
    parent_device: int
    parent_inode: int
    owner_user_id: int


@dataclass(frozen=True, slots=True)
class _MySQLTransportBinding:
    """One authenticated transport: verified TLS or a private local socket."""

    kind: Literal["tls", "unix_socket"]
    ssl_context: ssl.SSLContext | None
    unix_socket: _UnixSocketIdentity | None


@dataclass(frozen=True, slots=True)
class _MySQLWorkerEngineBinding:
    """Factory-only MySQL Engine target, driver limits, and process owner."""

    identity: _MySQLServerSchemaIdentity
    timeouts: _MySQLDriverTimeouts
    transport: _MySQLTransportBinding
    database_io_timeout_seconds: float
    pool_creator: object
    owner_process_id: int


_WorkerEngineBinding: TypeAlias = _SQLiteWorkerEngineBinding | _MySQLWorkerEngineBinding
_WORKER_ENGINE_BINDINGS: WeakKeyDictionary[Engine, _WorkerEngineBinding] = WeakKeyDictionary()
_WORKER_ENGINE_BINDINGS_LOCK = Lock()


class _SQLiteConnectionGuard:
    """Verify every engine checkout still targets the pinned SQLite file."""

    def __init__(
        self,
        database: _SQLiteFileIdentity,
        pool_creator: object,
        owner_process_id: int,
        maximum_wait_seconds: float,
    ) -> None:
        """Capture the physical database identity and connection wait limit."""
        self._database = database
        self._pool_creator = pool_creator
        self._owner_process_id = owner_process_id
        self._maximum_wait_seconds = maximum_wait_seconds

    def validate(self, connection: Connection) -> None:
        """Reject a checkout whose file identity or SQLite lock wait drifted."""
        _verify_process_owner(self._owner_process_id)
        had_transaction = connection.in_transaction()
        try:
            _verify_pool_wait_bound(
                connection.engine,
                self._maximum_wait_seconds,
                self._pool_creator,
            )
            _verify_actual_sqlite_connection(
                connection,
                expected_database=self._database,
                maximum_wait_seconds=self._maximum_wait_seconds,
            )
        finally:
            if not had_transaction and connection.in_transaction():
                connection.rollback()

    def validate_commit(self, connection: Connection) -> None:
        """Reject a commit if the database pathname changed during its transaction."""
        _verify_process_owner(self._owner_process_id)
        _verify_actual_sqlite_connection(
            connection,
            expected_database=self._database,
            maximum_wait_seconds=self._maximum_wait_seconds,
        )


class _MySQLConnectionGuard:
    """Verify every MySQL checkout and commit still targets the reviewed DB."""

    def __init__(
        self,
        identity: _MySQLServerSchemaIdentity,
        timeouts: _MySQLDriverTimeouts,
        transport: _MySQLTransportBinding,
        pool_creator: object,
        owner_process_id: int,
        database_io_timeout_seconds: float,
    ) -> None:
        """Capture immutable server, schema, driver, and process bindings."""
        self._identity = identity
        self._timeouts = timeouts
        self._transport = transport
        self._pool_creator = pool_creator
        self._owner_process_id = owner_process_id
        self._database_io_timeout_seconds = database_io_timeout_seconds

    def validate(self, connection: Connection) -> None:
        """Reject a checkout from a different server/schema or wider timeout."""
        _verify_process_owner(self._owner_process_id)
        had_transaction = connection.in_transaction()
        try:
            _verify_pool_wait_bound(
                connection.engine,
                self._database_io_timeout_seconds,
                self._pool_creator,
                backend="MySQL",
            )
            _verify_mysql_connection(connection, self._identity, self._timeouts)
            _verify_mysql_transport(connection, self._transport)
        finally:
            if not had_transaction and connection.in_transaction():
                connection.rollback()

    def validate_commit(self, connection: Connection) -> None:
        """Reject a commit unless the active transaction still targets the bound DB."""
        _verify_process_owner(self._owner_process_id)
        _verify_mysql_connection(connection, self._identity, self._timeouts)
        _verify_mysql_transport(connection, self._transport)


def create_worker_engine(
    database_path: str | Path,
    *,
    database_io_timeout_seconds: float,
) -> Engine:
    """Create an owned worker Engine pinned to an existing SQLite file.

    The factory accepts only a canonical absolute regular file with no symlink
    path components. It creates a one-connection standard :class:`QueuePool`
    using SQLAlchemy's built-in SQLite creator, and records the file inode,
    creator identity, timeout, and creating process ID in a weak registry. The
    Engine must be created in the process that will use it because SQLAlchemy
    pools and DBAPI connections cannot be inherited across ``fork``. A
    persistent worker accepts only an Engine registered here and rechecks that
    binding before each run. Direct Engines remain available to other services.

    Args:
        database_path: Existing absolute warehouse file path.
        database_io_timeout_seconds: SQLite lock and pool checkout bound.

    Returns:
        A SQLAlchemy Engine owned by the reviewed persistent worker path.

    Raises:
        TypeError: If the database path is not a string or :class:`Path`.
        ValueError: If the path or timeout is unsupported or not canonical.
    """
    if not isinstance(database_path, (str, Path)):
        raise TypeError("database_path must be a string or Path")
    maximum_wait = _bounded_number(
        database_io_timeout_seconds,
        "database_io_timeout_seconds",
        _MIN_NUMBER,
        _MAX_DATABASE_IO_TIMEOUT_SECONDS,
    )
    database = _canonical_file_identity(Path(database_path))
    engine = create_engine(
        URL.create("sqlite", database=str(database.path)),
        connect_args={"timeout": maximum_wait},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=maximum_wait,
    )
    creator = getattr(engine.pool, "_creator", None)
    if type(engine.pool) is not QueuePool or not callable(creator):
        engine.dispose()
        raise ValueError("worker Engine factory could not create its controlled SQLite QueuePool")
    try:
        # SQLAlchemy Engines are lazy. Warm and inspect the factory-created
        # connection before exposing/registering the Engine so a later path
        # replacement can never be mistaken for the originally pinned file.
        with engine.connect() as connection:
            _verify_actual_sqlite_connection(
                connection,
                expected_database=database,
                maximum_wait_seconds=maximum_wait,
            )
        if _canonical_file_identity(database.path) != database:
            raise ValueError("SQLite database file identity changed during worker Engine creation")
    except Exception:
        engine.dispose()
        raise
    binding = _SQLiteWorkerEngineBinding(database, maximum_wait, creator, os.getpid())
    with _WORKER_ENGINE_BINDINGS_LOCK:
        _WORKER_ENGINE_BINDINGS[engine] = binding
    return engine


def create_mysql_worker_engine(
    database_url: str | URL,
    *,
    expected_server_uuid: str,
    expected_schema: str,
    ssl_context: ssl.SSLContext | None = None,
    unix_socket_path: str | Path | None = None,
    database_io_timeout_seconds: float,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
    write_timeout_seconds: float,
) -> Engine:
    """Create an owned PyMySQL Engine pinned to a reviewed server and schema.

    The caller must explicitly provide the approved ``@@server_uuid`` and
    schema name. The URL's driver and selected schema must match the reviewed
    target, and URL query options are disallowed. TCP requires an explicitly
    supplied :class:`ssl.SSLContext` with certificate and hostname verification
    enabled. Alternatively, a local ``localhost`` URL may use an explicit
    Unix socket inside a private directory owned by the current user. The two
    transport options are mutually exclusive. The factory uses a standard
    one-connection :class:`QueuePool` and PyMySQL's connect, read, and write
    timeouts. Before registering the Engine it warms one connection and
    verifies the transport, server identity, selected database, and driver
    timeout state. It neither creates a schema nor reads settings or environment.

    Each driver timeout and the pool checkout timeout must be no larger than
    ``database_io_timeout_seconds``. The worker lease must in turn exceed its
    heartbeat interval plus twice that bound and a safety margin. These client
    limits bound connect/socket/pool waits; they do not impose a server-side SQL
    execution deadline or bound operating-system stalls. Create this Engine in
    the process that will use it; SQLAlchemy pools cannot cross ``fork``.

    Args:
        database_url: Explicit ``mysql+pymysql`` URL containing the target schema.
        expected_server_uuid: Reviewed canonical MySQL server UUID.
        expected_schema: Reviewed database name, matching the URL schema.
        ssl_context: Required for TCP; must require a trusted certificate and
            hostname validation. Mutually exclusive with ``unix_socket_path``.
        unix_socket_path: Explicit private local socket path, for a ``localhost``
            URL only. Mutually exclusive with ``ssl_context``.
        database_io_timeout_seconds: Upper bound used by the worker lease budget.
        connect_timeout_seconds: PyMySQL TCP connection timeout.
        read_timeout_seconds: PyMySQL socket read timeout.
        write_timeout_seconds: PyMySQL socket write timeout.

    Returns:
        A preflighted SQLAlchemy Engine owned by :class:`PersistentRunWorker`.

    Raises:
        TypeError: If the URL has an unsupported type.
        ValueError: If identity, driver, or timeout requirements are not met,
            if transport security is not verified, or the live server does not
            match the approved target.
    """
    if not isinstance(database_url, (str, URL)):
        raise TypeError("database_url must be a string or SQLAlchemy URL")
    identity = _validated_mysql_identity(expected_server_uuid, expected_schema)
    maximum_wait = _bounded_number(
        database_io_timeout_seconds,
        "database_io_timeout_seconds",
        _MIN_NUMBER,
        _MAX_DATABASE_IO_TIMEOUT_SECONDS,
    )
    timeouts = _MySQLDriverTimeouts(
        connect=_bounded_number(
            connect_timeout_seconds,
            "connect_timeout_seconds",
            _MIN_NUMBER,
            _MAX_DATABASE_IO_TIMEOUT_SECONDS,
        ),
        read=_bounded_number(
            read_timeout_seconds,
            "read_timeout_seconds",
            _MIN_NUMBER,
            _MAX_DATABASE_IO_TIMEOUT_SECONDS,
        ),
        write=_bounded_number(
            write_timeout_seconds,
            "write_timeout_seconds",
            _MIN_NUMBER,
            _MAX_DATABASE_IO_TIMEOUT_SECONDS,
        ),
    )
    if max(timeouts.connect, timeouts.read, timeouts.write) > maximum_wait:
        raise ValueError("MySQL driver timeouts exceed database_io_timeout_seconds")
    try:
        url = make_url(database_url)
    except Exception:
        raise ValueError("MySQL worker URL is invalid") from None
    if (
        url.drivername != "mysql+pymysql"
        or url.database != identity.schema
        or url.query
        or not url.host
        or not url.username
        or (url.port is not None and (not isinstance(url.port, int) or not 1 <= url.port <= 65_535))
    ):
        raise ValueError("MySQL worker URL must select the reviewed PyMySQL server schema")

    transport = _validated_mysql_transport(url, ssl_context, unix_socket_path)
    connect_args: dict[str, object] = {
        "connect_timeout": timeouts.connect,
        "read_timeout": timeouts.read,
        "write_timeout": timeouts.write,
    }
    if transport.kind == "tls":
        if transport.ssl_context is None:  # pragma: no cover - closed union
            raise ValueError("MySQL TLS transport is missing its verified context")
        connect_args["ssl"] = transport.ssl_context
    else:
        if transport.unix_socket is None:  # pragma: no cover - closed union
            raise ValueError("MySQL Unix socket transport is missing its verified path")
        connect_args["unix_socket"] = str(transport.unix_socket.path)

    try:
        engine = create_engine(
            url,
            connect_args=connect_args,
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=maximum_wait,
        )
    except Exception as exc:
        raise ValueError(f"MySQL worker Engine creation failed ({type(exc).__name__})") from None
    creator = getattr(engine.pool, "_creator", None)
    if (
        engine.dialect.name != "mysql"
        or engine.dialect.driver != "pymysql"
        or type(engine.pool) is not QueuePool
        or not callable(creator)
    ):
        engine.dispose()
        raise ValueError("MySQL worker factory could not create its controlled PyMySQL QueuePool")
    guarded_creator = _guard_mysql_pool_creator(creator, transport)
    # SQLAlchemy exposes no public hook to guard a physical connection before authentication.
    # The targeted lint exemption keeps the guard limited to this owned QueuePool slot.
    setattr(engine.pool, "_creator", guarded_creator)  # noqa: B010
    try:
        with engine.connect() as connection:
            _verify_mysql_connection(connection, identity, timeouts)
            _verify_mysql_transport(connection, transport)
        _verify_pool_wait_bound(engine, maximum_wait, guarded_creator, backend="MySQL")
    except ValueError:
        engine.dispose()
        raise
    except Exception as exc:
        engine.dispose()
        raise ValueError(f"MySQL worker Engine preflight failed ({type(exc).__name__})") from None
    binding = _MySQLWorkerEngineBinding(
        identity,
        timeouts,
        transport,
        maximum_wait,
        guarded_creator,
        os.getpid(),
    )
    with _WORKER_ENGINE_BINDINGS_LOCK:
        _WORKER_ENGINE_BINDINGS[engine] = binding
    return engine


class ProcessFetchHandler:
    """Adapt a fixed reviewed process operation to a run fetch handler.

    The builder and decoder execute in the parent process and are supplied by
    reviewed application code. Neither the process operation nor its import
    reference can be selected by data in the run request. The builder and
    decoder must remain bounded local conversions; only the registered
    provider operation runs in the killable child process.
    """

    def __init__(
        self,
        executor: ReviewedProcessExecutor,
        operation_id: str,
        parameters_builder: ParameterBuilder,
        result_decoder: ResultDecoder,
        *,
        timeout_seconds: float,
    ) -> None:
        """Bind one operation and explicit parent-side JSON conversions.

        Args:
            executor: Process executor with the same operation registered.
            operation_id: Stable operation key from the reviewed registry.
            parameters_builder: Converts a validated request to JSON data.
            result_decoder: Converts returned JSON into the normalizer input.
            timeout_seconds: Total process startup and execution deadline.

        Raises:
            TypeError: If a required dependency is not the expected type.
            ValueError: If the operation ID or deadline is invalid.
        """
        if not isinstance(executor, ReviewedProcessExecutor):
            raise TypeError("executor must be a ReviewedProcessExecutor")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id must be a non-empty registered identifier")
        if not callable(parameters_builder):
            raise TypeError("parameters_builder must be callable")
        if not callable(result_decoder):
            raise TypeError("result_decoder must be callable")
        self._timeout_seconds = _bounded_number(
            timeout_seconds, "timeout_seconds", _MIN_NUMBER, 3_600.0
        )
        executor_interval = getattr(executor, "_checkpoint_interval", None)
        self._heartbeat_interval_seconds = _bounded_number(
            executor_interval,
            "executor checkpoint interval",
            _MIN_NUMBER,
            _MAX_HEARTBEAT_INTERVAL_SECONDS,
        )
        self._executor = executor
        self._operation_id = operation_id
        self._parameters_builder = parameters_builder
        self._result_decoder = result_decoder

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        """Run the fixed operation while renewing its parent-held lease."""
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a validated RunRequest")
        if not isinstance(context, RunExecutionContext):
            raise TypeError("context must be a RunExecutionContext")
        context.checkpoint()
        parameters = self._parameters_builder(request)
        if not isinstance(parameters, Mapping):
            raise TypeError("parameters_builder must return a JSON object")
        result = self._executor.run(
            self._operation_id,
            parameters,
            timeout_seconds=self._timeout_seconds,
            checkpoint=context.checkpoint,
        )
        return self._result_decoder(result.value)

    @property
    def heartbeat_interval_seconds(self) -> float:
        """Return the actual checkpoint interval configured on the executor."""
        return self._heartbeat_interval_seconds


class PersistentRunWorker:
    """Compose an explicit reviewed catalog with a database-clock run ledger.

    The supplied engine must point to the same durable warehouse that stores
    facts and must already contain the run-ledger table. This version accepts
    only Engines created by :func:`create_worker_engine` or
    :func:`create_mysql_worker_engine`. SQLite Engines are pinned to an
    existing canonical regular file; MySQL Engines are pinned to an explicitly
    reviewed server UUID and schema. Both use process-owned standard
    :class:`QueuePool` connections. PostgreSQL remains unsupported.
    """

    def __init__(
        self,
        engine: Engine,
        registry: ReviewedJobRegistry,
        *,
        worker_id: str,
        lease_ttl: timedelta,
        heartbeat_interval_seconds: float,
        database_io_timeout_seconds: float,
    ) -> None:
        """Bind durable storage, reviewed jobs, and a validated timing budget.

        Every registered fetch handler must be a
        :class:`ProcessFetchHandler`; a normal callable cannot be stopped when
        its lease expires. ``heartbeat_interval_seconds`` must match the actual
        interval on every process handler. The lease must cover one heartbeat
        interval, twice the largest configured database I/O wait, and a safety
        margin. SQLite's busy timeout bounds lock waits; MySQL uses PyMySQL
        connect/read/write and pool timeouts. Neither contract imposes a
        server-side SQL deadline or bounds operating-system stalls.

        Args:
            engine: Owned engine from either reviewed worker Engine factory.
            registry: Immutable catalog of explicitly reviewed jobs.
            worker_id: Stable identifier unique to this live worker instance.
            lease_ttl: Maximum time a claim remains owned without renewal.
            heartbeat_interval_seconds: Maximum executor heartbeat interval.
            database_io_timeout_seconds: Maximum factory-verified pool, SQLite
                lock, or MySQL driver timeout used in the lease budget.

        Raises:
            TypeError: If storage or registry dependencies have the wrong type.
            ValueError: If worker identity or timing settings are invalid.
        """
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if not isinstance(registry, ReviewedJobRegistry):
            raise TypeError("registry must be a ReviewedJobRegistry")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be a non-empty string")
        ttl_seconds = _validate_ttl(lease_ttl)
        heartbeat_interval = _bounded_number(
            heartbeat_interval_seconds,
            "heartbeat_interval_seconds",
            _MIN_NUMBER,
            _MAX_HEARTBEAT_INTERVAL_SECONDS,
        )
        database_io_timeout = _bounded_number(
            database_io_timeout_seconds,
            "database_io_timeout_seconds",
            _MIN_NUMBER,
            _MAX_DATABASE_IO_TIMEOUT_SECONDS,
        )
        minimum_ttl = heartbeat_interval + 2 * database_io_timeout + _MIN_LEASE_MARGIN_SECONDS
        if ttl_seconds <= minimum_ttl:
            raise ValueError("lease_ttl is too short for the configured heartbeat and database I/O")
        _verify_registry_heartbeat_intervals(registry, heartbeat_interval)
        binding = _require_worker_engine_binding(engine, database_io_timeout)
        if isinstance(binding, _SQLiteWorkerEngineBinding):
            if engine.dialect.name != "sqlite":
                raise ValueError("SQLite worker Engine dialect changed after factory creation")
            _verify_sqlite_wait_bounds(engine, binding)
            guard: _SQLiteConnectionGuard | _MySQLConnectionGuard = _SQLiteConnectionGuard(
                binding.database,
                binding.pool_creator,
                binding.owner_process_id,
                database_io_timeout,
            )
        elif isinstance(binding, _MySQLWorkerEngineBinding):
            if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
                raise ValueError("MySQL worker Engine dialect changed after factory creation")
            _verify_mysql_wait_bounds(engine, binding)
            guard = _MySQLConnectionGuard(
                binding.identity,
                binding.timeouts,
                binding.transport,
                binding.pool_creator,
                binding.owner_process_id,
                database_io_timeout,
            )
        else:  # pragma: no cover - closed union
            raise TypeError("worker Engine binding type is unsupported")
        event.listen(engine, "engine_connect", guard.validate)
        event.listen(engine, "commit", guard.validate_commit)

        self._worker_id = worker_id.strip()
        self._lease_ttl = lease_ttl
        self._database_io_timeout_seconds = database_io_timeout
        self._engine = engine
        self._engine_binding = binding
        self._ledger = RunLedger(engine, clock_policy="database")
        self._service = RunService(self._ledger, registry)
        self._connection_guard = guard

    @property
    def ledger(self) -> RunLedger:
        """Return the persistent ledger used by this worker."""
        return self._ledger

    def submit(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Persist one validated request using the reviewed run service."""
        _verify_process_owner(self._engine_binding.owner_process_id)
        backend: Literal["MySQL", "SQLite"] = (
            "MySQL" if isinstance(self._engine_binding, _MySQLWorkerEngineBinding) else "SQLite"
        )
        _verify_pool_wait_bound(
            self._engine,
            self._database_io_timeout_seconds,
            self._engine_binding.pool_creator,
            backend=backend,
        )
        return self._service.submit(job_id, version, request)

    def run_once(self) -> RunOutcome | None:
        """Claim at most one durable run and execute it to a fenced outcome."""
        _verify_process_owner(self._engine_binding.owner_process_id)
        backend: Literal["MySQL", "SQLite"] = (
            "MySQL" if isinstance(self._engine_binding, _MySQLWorkerEngineBinding) else "SQLite"
        )
        _verify_pool_wait_bound(
            self._engine,
            self._database_io_timeout_seconds,
            self._engine_binding.pool_creator,
            backend=backend,
        )
        return self._service.run_once(self._worker_id, self._lease_ttl)


def _bounded_number(value: object, name: str, minimum: float, maximum: float) -> float:
    """Validate a finite numeric timing value without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


def _validate_ttl(value: timedelta) -> float:
    """Check the same hard lease bounds enforced by the persistent ledger."""
    if not isinstance(value, timedelta):
        raise TypeError("lease_ttl must be a timedelta")
    seconds = value.total_seconds()
    if not math.isfinite(seconds) or seconds < MIN_TTL.total_seconds():
        raise ValueError("lease_ttl is below the ledger minimum")
    if seconds > MAX_TTL.total_seconds():
        raise ValueError("lease_ttl exceeds the ledger maximum")
    return seconds


def _canonical_file_identity(database_path: Path) -> _SQLiteFileIdentity:
    """Require an existing canonical absolute regular file and capture its inode."""
    if not database_path.is_absolute():
        raise ValueError("worker database path must be absolute")
    try:
        resolved_path = database_path.resolve(strict=True)
        path_stat = database_path.stat(follow_symlinks=False)
    except OSError:
        raise ValueError("SQLite database path must identify an existing regular file") from None
    if resolved_path != database_path or not S_ISREG(path_stat.st_mode):
        raise ValueError("SQLite database path must be canonical and identify a regular file")
    return _SQLiteFileIdentity(database_path, path_stat.st_dev, path_stat.st_ino)


def _verify_process_owner(owner_process_id: int) -> None:
    """Reject an Engine inherited by a process other than its creator."""
    if os.getpid() != owner_process_id:
        raise ValueError("worker Engine cannot be reused across process boundaries")


def _require_worker_engine_binding(
    engine: Engine, maximum_wait_seconds: float
) -> _WorkerEngineBinding:
    """Require the correct process-local factory binding and unchanged target."""
    with _WORKER_ENGINE_BINDINGS_LOCK:
        binding = _WORKER_ENGINE_BINDINGS.get(engine)
    if binding is None:
        if engine.dialect.name == "mysql":
            raise ValueError(
                "persistent worker requires verified file-backed SQLite or a MySQL Engine "
                "from create_mysql_worker_engine"
            )
        raise ValueError("persistent worker Engine must come from create_worker_engine")
    _verify_process_owner(binding.owner_process_id)
    if not math.isclose(
        binding.database_io_timeout_seconds,
        maximum_wait_seconds,
        rel_tol=0.0,
        abs_tol=0.000_001,
    ):
        raise ValueError("worker database I/O timeout must match its factory Engine")
    if isinstance(binding, _SQLiteWorkerEngineBinding):
        if engine.dialect.name != "sqlite":
            raise ValueError("SQLite worker Engine dialect changed after factory creation")
        if engine.url.database != str(binding.database.path) or engine.url.query:
            raise ValueError("worker Engine URL changed after factory registration")
        _verify_pool_wait_bound(engine, maximum_wait_seconds, binding.pool_creator)
        current_database = _canonical_file_identity(binding.database.path)
        if current_database != binding.database:
            raise ValueError("SQLite database file identity changed after worker Engine creation")
    elif isinstance(binding, _MySQLWorkerEngineBinding):
        if engine.dialect.name != "mysql" or engine.dialect.driver != "pymysql":
            raise ValueError("MySQL worker Engine dialect changed after factory creation")
        if engine.url.drivername != "mysql+pymysql" or engine.url.query:
            raise ValueError("MySQL worker Engine URL changed after factory registration")
        if engine.url.database != binding.identity.schema:
            raise ValueError("MySQL worker Engine schema changed after factory registration")
        _verify_pool_wait_bound(
            engine,
            maximum_wait_seconds,
            binding.pool_creator,
            backend="MySQL",
        )
    else:  # pragma: no cover - closed union
        raise TypeError("worker Engine binding type is unsupported")
    return binding


def _verify_sqlite_wait_bounds(engine: Engine, binding: _SQLiteWorkerEngineBinding) -> None:
    """Verify the factory-owned Engine's first actual connection and lock wait."""
    with engine.connect() as connection:
        _verify_actual_sqlite_connection(
            connection,
            expected_database=binding.database,
            maximum_wait_seconds=binding.database_io_timeout_seconds,
        )


def _verify_mysql_wait_bounds(engine: Engine, binding: _MySQLWorkerEngineBinding) -> None:
    """Verify the factory-owned MySQL Engine's first checkout and identity."""
    _verify_pool_wait_bound(
        engine,
        binding.database_io_timeout_seconds,
        binding.pool_creator,
        backend="MySQL",
    )
    with engine.connect() as connection:
        _verify_mysql_connection(connection, binding.identity, binding.timeouts)
        _verify_mysql_transport(connection, binding.transport)


def _verify_pool_wait_bound(
    engine: Engine,
    maximum_wait_seconds: float,
    expected_creator: object | None = None,
    *,
    backend: Literal["MySQL", "SQLite"] = "SQLite",
) -> None:
    """Require the factory's one-slot QueuePool and a finite checkout wait."""
    if type(engine.pool) is not QueuePool:
        raise ValueError(f"persistent worker requires the standard bounded {backend} QueuePool")
    if (
        expected_creator is not None
        and getattr(engine.pool, "_creator", None) is not expected_creator
    ):
        raise ValueError(f"worker Engine {backend} creator changed after factory registration")
    try:
        pool_timeout = float(engine.pool.timeout())
        pool_size = engine.pool.size()
        max_overflow = getattr(engine.pool, "_max_overflow", None)
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        raise ValueError(f"{backend} QueuePool configuration could not be verified") from None
    if pool_size != 1 or max_overflow != 0:
        raise ValueError(f"persistent worker requires a one-slot {backend} QueuePool")
    if not math.isfinite(pool_timeout) or pool_timeout < 0 or pool_timeout > maximum_wait_seconds:
        raise ValueError(
            f"{backend} pool checkout timeout exceeds the configured database I/O bound"
        )


def _verify_actual_sqlite_connection(
    connection: Connection,
    *,
    expected_database: _SQLiteFileIdentity,
    maximum_wait_seconds: float,
) -> None:
    """Verify the checked-out connection's SQLite file and per-connection wait."""
    try:
        databases = connection.exec_driver_sql("PRAGMA database_list").all()
        main_files = [str(row[2]) for row in databases if row[1] == "main"]
        busy_timeout_ms = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        current_stat = expected_database.path.stat(follow_symlinks=False)
        current_resolved_path = expected_database.path.resolve(strict=True)
    except Exception:
        raise ValueError("SQLite database connection could not be verified") from None
    if len(main_files) != 1 or main_files[0] != str(expected_database.path):
        raise ValueError("SQLite connection does not target the configured regular database file")
    if (
        current_resolved_path != expected_database.path
        or not S_ISREG(current_stat.st_mode)
        or current_stat.st_dev != expected_database.device
        or current_stat.st_ino != expected_database.inode
    ):
        raise ValueError("SQLite database file identity changed after worker verification")
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
        raise ValueError("SQLite busy_timeout has an invalid value")
    if busy_timeout_ms / 1_000 > maximum_wait_seconds + 0.001:
        raise ValueError("SQLite busy_timeout exceeds the configured database I/O bound")


def _validated_mysql_identity(
    expected_server_uuid: str, expected_schema: str
) -> _MySQLServerSchemaIdentity:
    """Validate reviewed target identifiers before any network connection."""
    if not isinstance(expected_server_uuid, str):
        raise TypeError("expected_server_uuid must be a string")
    if not isinstance(expected_schema, str):
        raise TypeError("expected_schema must be a string")
    try:
        normalized_uuid = str(UUID(expected_server_uuid))
    except (ValueError, AttributeError):
        raise ValueError("expected_server_uuid must be a canonical UUID") from None
    if expected_server_uuid.lower() != normalized_uuid:
        raise ValueError("expected_server_uuid must be a canonical UUID")
    if not re.fullmatch(r"[A-Za-z0-9_$-]{1,64}", expected_schema):
        raise ValueError("expected_schema must be a bounded MySQL database identifier")
    return _MySQLServerSchemaIdentity(normalized_uuid, expected_schema)


def _validated_mysql_transport(
    url: URL,
    ssl_context: ssl.SSLContext | None,
    unix_socket_path: str | Path | None,
) -> _MySQLTransportBinding:
    """Require verified TLS for TCP or a pinned private localhost socket."""
    if ssl_context is not None and unix_socket_path is not None:
        raise ValueError("MySQL TLS and Unix socket transports are mutually exclusive")
    if ssl_context is not None:
        if not isinstance(ssl_context, ssl.SSLContext):
            raise TypeError("ssl_context must be an ssl.SSLContext")
        if ssl_context.verify_mode != ssl.CERT_REQUIRED or ssl_context.check_hostname is not True:
            raise ValueError("MySQL TCP transport requires certificate and hostname verification")
        return _MySQLTransportBinding("tls", ssl_context, None)
    if unix_socket_path is None:
        raise ValueError(
            "plain MySQL TCP is disabled; provide verified TLS or a private Unix socket"
        )
    if not isinstance(unix_socket_path, (str, Path)):
        raise TypeError("unix_socket_path must be a string or Path")
    if url.host != "localhost" or url.port is not None:
        raise ValueError("MySQL Unix socket transport requires a localhost URL without a port")
    identity = _canonical_private_unix_socket_identity(Path(unix_socket_path))
    return _MySQLTransportBinding("unix_socket", None, identity)


def _canonical_private_unix_socket_identity(path: Path) -> _UnixSocketIdentity:
    """Require an absolute socket in a canonical, owner-controlled directory."""
    if not path.is_absolute():
        raise ValueError("MySQL Unix socket path must be absolute")
    try:
        resolved_path = path.resolve(strict=True)
        socket_stat = path.stat(follow_symlinks=False)
        resolved_parent = path.parent.resolve(strict=True)
        parent_stat = path.parent.stat(follow_symlinks=False)
    except OSError:
        raise ValueError("MySQL Unix socket must exist inside a private directory") from None
    if (
        resolved_path != path
        or resolved_parent != path.parent
        or not S_ISSOCK(socket_stat.st_mode)
        or not S_ISDIR(parent_stat.st_mode)
    ):
        raise ValueError("MySQL Unix socket path must be canonical and identify a socket")
    if (
        parent_stat.st_uid != os.getuid()
        or S_IMODE(parent_stat.st_mode) & 0o027
        or socket_stat.st_uid != os.getuid()
    ):
        raise ValueError("MySQL Unix socket directory must be private and owned by this user")
    return _UnixSocketIdentity(
        path,
        socket_stat.st_dev,
        socket_stat.st_ino,
        path.parent,
        parent_stat.st_dev,
        parent_stat.st_ino,
        os.getuid(),
    )


def _verify_private_unix_socket_identity(expected: _UnixSocketIdentity) -> None:
    """Reject replaced sockets or parent directories after factory preflight."""
    current = _canonical_private_unix_socket_identity(expected.path)
    if current != expected:
        raise ValueError("MySQL Unix socket identity changed after worker Engine creation")


def _verify_mysql_transport_configuration(transport: _MySQLTransportBinding) -> None:
    """Recheck mutable TLS policy or socket identity before opening a connection."""
    if transport.kind == "tls":
        context = transport.ssl_context
        if (
            context is None
            or context.verify_mode != ssl.CERT_REQUIRED
            or not context.check_hostname
        ):
            raise ValueError("MySQL TLS verification policy changed after Engine creation")
    elif transport.kind == "unix_socket":
        expected = transport.unix_socket
        if expected is None:  # pragma: no cover - closed union
            raise ValueError("MySQL Unix socket binding is incomplete")
        _verify_private_unix_socket_identity(expected)
    else:  # pragma: no cover - closed union
        raise ValueError("MySQL worker transport is unsupported")


def _guard_mysql_pool_creator(
    creator: Callable[[], object], transport: _MySQLTransportBinding
) -> Callable[[], object]:
    """Check transport security immediately before PyMySQL receives credentials."""
    if not callable(creator):
        raise TypeError("MySQL QueuePool creator must be callable")

    def create_connection() -> object:
        _verify_mysql_transport_configuration(transport)
        return creator()

    return create_connection


def _verify_mysql_transport(connection: Connection, transport: _MySQLTransportBinding) -> None:
    """Verify the active DBAPI connection uses the registered secure transport."""
    try:
        _verify_mysql_transport_configuration(transport)
        dbapi_connection = connection.connection.driver_connection
        active_socket = getattr(dbapi_connection, "_sock", None)
        if transport.kind == "tls":
            context = transport.ssl_context
            if context is None:  # pragma: no cover - checked above
                raise ValueError("MySQL TLS context binding is incomplete")
            if (
                not isinstance(active_socket, ssl.SSLSocket)
                or active_socket.context is not context
                or getattr(dbapi_connection, "_secure", False) is not True
            ):
                raise ValueError(
                    "MySQL connection is not using its registered verified TLS context"
                )
        elif transport.kind == "unix_socket":
            expected = transport.unix_socket
            if expected is None:  # pragma: no cover - closed union
                raise ValueError("MySQL Unix socket binding is incomplete")
            _verify_private_unix_socket_identity(expected)
            if (
                getattr(dbapi_connection, "unix_socket", None) != str(expected.path)
                or getattr(active_socket, "family", None) != socket.AF_UNIX
            ):
                raise ValueError("MySQL connection is not using its registered private Unix socket")
        else:  # pragma: no cover - closed union
            raise ValueError("MySQL worker transport is unsupported")
    except ValueError:
        raise
    except Exception:
        raise ValueError("MySQL transport could not be verified") from None


def _verify_mysql_connection(
    connection: Connection,
    identity: _MySQLServerSchemaIdentity,
    timeouts: _MySQLDriverTimeouts,
) -> None:
    """Read-only verify server/schema identity and actual PyMySQL timeouts."""
    try:
        row = connection.exec_driver_sql("SELECT @@server_uuid, DATABASE()").one()
        dbapi_connection = connection.connection.driver_connection
        connect_timeout = getattr(dbapi_connection, "connect_timeout", None)
        read_timeout = getattr(dbapi_connection, "_read_timeout", None)
        write_timeout = getattr(dbapi_connection, "_write_timeout", None)
        socket = getattr(dbapi_connection, "_sock", None)
        socket_timeout = socket.gettimeout() if socket is not None else None
    except Exception:
        raise ValueError("MySQL worker connection could not be verified") from None
    if str(row[0]).lower() != identity.server_uuid or row[1] != identity.schema:
        raise ValueError("MySQL connection does not match the reviewed server/schema identity")
    observed_timeouts = (connect_timeout, read_timeout, write_timeout, socket_timeout)
    expected_timeouts = (timeouts.connect, timeouts.read, timeouts.write, timeouts.read)
    for observed, expected in zip(observed_timeouts, expected_timeouts, strict=True):
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isfinite(float(observed))
            or not math.isclose(float(observed), expected, rel_tol=0.0, abs_tol=0.001)
        ):
            raise ValueError("MySQL driver timeout changed after factory registration")


def _verify_registry_heartbeat_intervals(
    registry: ReviewedJobRegistry, configured_interval: float
) -> None:
    """Fail closed if any process-backed fetcher checkpoints too slowly."""
    jobs = getattr(registry, "_jobs", None)
    if not isinstance(jobs, Mapping):
        raise TypeError("reviewed job registry does not expose its frozen job catalog")
    for job in jobs.values():
        fetcher = getattr(job, "fetch", None)
        if not isinstance(fetcher, ProcessFetchHandler):
            raise ValueError("persistent worker requires process-bounded reviewed fetch handlers")
        if not math.isclose(
            fetcher.heartbeat_interval_seconds,
            configured_interval,
            rel_tol=0.0,
            abs_tol=0.000_001,
        ):
            raise ValueError("heartbeat_interval_seconds must match every process executor")


__all__ = [
    "PersistentRunWorker",
    "ProcessFetchHandler",
    "create_mysql_worker_engine",
    "create_worker_engine",
]
