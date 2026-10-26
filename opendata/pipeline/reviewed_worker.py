"""Run persisted reviewed jobs with parent-owned leases and killable fetches.

This module composes the durable ledger, controlled run service, and reviewed
JSON process executor. Database access, lease renewal, normalization, and
publication remain in the worker process. Only a fixed operation identifier
and plain JSON parameters cross into the provider child process.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, TypeAlias

from pydantic import JsonValue
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
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


class _SQLiteConnectionGuard:
    """Verify every engine checkout still targets the pinned SQLite file."""

    def __init__(
        self,
        database_path: Path,
        device: int,
        inode: int,
        maximum_wait_seconds: float,
    ) -> None:
        """Capture the physical database identity and connection wait limit."""
        self._database_path = database_path
        self._device = device
        self._inode = inode
        self._maximum_wait_seconds = maximum_wait_seconds

    def validate(self, connection: Connection) -> None:
        """Reject a checkout whose file identity or SQLite lock wait drifted."""
        had_transaction = connection.in_transaction()
        try:
            _verify_pool_wait_bound(connection.engine, self._maximum_wait_seconds)
            _verify_actual_sqlite_connection(
                connection,
                expected_path=self._database_path,
                expected_device=self._device,
                expected_inode=self._inode,
                maximum_wait_seconds=self._maximum_wait_seconds,
            )
        finally:
            if not had_transaction and connection.in_transaction():
                connection.rollback()

    def validate_commit(self, connection: Connection) -> None:
        """Reject a commit if the database pathname changed during its transaction."""
        _verify_actual_sqlite_connection(
            connection,
            expected_path=self._database_path,
            expected_device=self._device,
            expected_inode=self._inode,
            maximum_wait_seconds=self._maximum_wait_seconds,
        )


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
    only file-backed SQLite engines that use the standard SQLAlchemy
    :class:`QueuePool`. It pins the canonical path and physical file identity,
    then validates the SQLite filename and lock-wait bound on every engine
    checkout. MySQL and PostgreSQL require an independently verified driver
    and statement timeout contract before this facade can accept them.
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
        interval, the verified SQLite pool/lock wait bound, and a safety margin.
        Those checks do not bound operating-system disk stalls or SQL execution.

        Args:
            engine: Synchronous engine for the durable warehouse.
            registry: Immutable catalog of explicitly reviewed jobs.
            worker_id: Stable identifier unique to this live worker instance.
            lease_ttl: Maximum time a claim remains owned without renewal.
            heartbeat_interval_seconds: Maximum executor heartbeat interval.
            database_io_timeout_seconds: Maximum verified QueuePool checkout and
                SQLite lock wait.

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
        if engine.dialect.name != "sqlite":
            raise ValueError("persistent worker currently requires verified file-backed SQLite")
        _verify_registry_heartbeat_intervals(registry, heartbeat_interval)
        database_path, device, inode = _verify_sqlite_wait_bounds(engine, database_io_timeout)
        guard = _SQLiteConnectionGuard(
            database_path,
            device,
            inode,
            database_io_timeout,
        )
        event.listen(engine, "engine_connect", guard.validate)
        event.listen(engine, "commit", guard.validate_commit)

        self._worker_id = worker_id.strip()
        self._lease_ttl = lease_ttl
        self._database_io_timeout_seconds = database_io_timeout
        self._engine = engine
        self._ledger = RunLedger(engine, clock_policy="database")
        self._service = RunService(self._ledger, registry)
        self._connection_guard = guard

    @property
    def ledger(self) -> RunLedger:
        """Return the persistent ledger used by this worker."""
        return self._ledger

    def submit(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Persist one validated request using the reviewed run service."""
        _verify_pool_wait_bound(self._engine, self._database_io_timeout_seconds)
        return self._service.submit(job_id, version, request)

    def run_once(self) -> RunOutcome | None:
        """Claim at most one durable run and execute it to a fenced outcome."""
        _verify_pool_wait_bound(self._engine, self._database_io_timeout_seconds)
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


def _verify_sqlite_wait_bounds(
    engine: Engine, maximum_wait_seconds: float
) -> tuple[Path, int, int]:
    """Pin an absolute SQLite file and verify its first actual checkout."""
    database_path, expected_device, expected_inode = _canonical_database_identity(engine)
    _verify_pool_wait_bound(engine, maximum_wait_seconds)
    try:
        with engine.connect() as connection:
            _verify_actual_sqlite_connection(
                connection,
                expected_path=database_path,
                expected_device=expected_device,
                expected_inode=expected_inode,
                maximum_wait_seconds=maximum_wait_seconds,
            )
    except ValueError:
        raise
    except Exception:
        raise ValueError("SQLite database connection could not be verified") from None
    return database_path, expected_device, expected_inode


def _canonical_database_identity(engine: Engine) -> tuple[Path, int, int]:
    """Require a canonical absolute URL path and capture its regular-file inode."""
    database = engine.url.database
    uri_option = engine.url.query.get("uri", "false")
    uri_values = uri_option if isinstance(uri_option, tuple) else (uri_option,)
    uri_enabled = any(str(value).lower() in {"true", "1", "yes"} for value in uri_values)
    if (
        not database
        or database == ":memory:"
        or database.startswith("file:")
        or uri_enabled
        or not Path(database).is_absolute()
    ):
        raise ValueError("persistent worker requires a canonical absolute file-backed SQLite path")
    try:
        path = Path(database)
        resolved_path = path.resolve(strict=True)
        path_stat = path.stat(follow_symlinks=False)
    except OSError:
        raise ValueError("SQLite database path must identify an existing regular file") from None
    if resolved_path != path or not S_ISREG(path_stat.st_mode):
        raise ValueError("SQLite database path must be canonical and identify a regular file")
    return path, path_stat.st_dev, path_stat.st_ino


def _verify_pool_wait_bound(engine: Engine, maximum_wait_seconds: float) -> None:
    """Require the documented QueuePool implementation and a finite checkout wait."""
    if type(engine.pool) is not QueuePool:
        raise ValueError("persistent worker requires the standard bounded SQLite QueuePool")
    try:
        pool_timeout = float(engine.pool.timeout())
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        raise ValueError("SQLite QueuePool checkout timeout could not be verified") from None
    if not math.isfinite(pool_timeout) or pool_timeout < 0 or pool_timeout > maximum_wait_seconds:
        raise ValueError("SQLite pool checkout timeout exceeds the configured database I/O bound")


def _verify_actual_sqlite_connection(
    connection: Connection,
    *,
    expected_path: Path,
    expected_device: int,
    expected_inode: int,
    maximum_wait_seconds: float,
) -> None:
    """Verify the checked-out connection's SQLite file and per-connection wait."""
    try:
        databases = connection.exec_driver_sql("PRAGMA database_list").all()
        main_files = [str(row[2]) for row in databases if row[1] == "main"]
        busy_timeout_ms = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        current_stat = expected_path.stat(follow_symlinks=False)
        current_resolved_path = expected_path.resolve(strict=True)
    except Exception:
        raise ValueError("SQLite database connection could not be verified") from None
    if len(main_files) != 1 or main_files[0] != str(expected_path):
        raise ValueError("SQLite connection does not target the configured regular database file")
    if (
        current_resolved_path != expected_path
        or not S_ISREG(current_stat.st_mode)
        or current_stat.st_dev != expected_device
        or current_stat.st_ino != expected_inode
    ):
        raise ValueError("SQLite database file identity changed after worker verification")
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
        raise ValueError("SQLite busy_timeout has an invalid value")
    if busy_timeout_ms / 1_000 > maximum_wait_seconds + 0.001:
        raise ValueError("SQLite busy_timeout exceeds the configured database I/O bound")


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


__all__ = ["PersistentRunWorker", "ProcessFetchHandler"]
