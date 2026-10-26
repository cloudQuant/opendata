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
from typing import TYPE_CHECKING, TypeAlias

from pydantic import JsonValue
from sqlalchemy.engine import Engine

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


class ProcessFetchHandler:
    """Adapt a fixed reviewed process operation to a run fetch handler.

    The builder and decoder execute in the parent process and are supplied by
    reviewed application code. Neither the process operation nor its import
    reference can be selected by data in the run request.
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
    only file-backed SQLite engines after verifying pool checkout and SQLite
    lock-wait bounds. MySQL and PostgreSQL require an independently verified
    driver and statement timeout contract before this facade can accept them.
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

        ``heartbeat_interval_seconds`` must match the actual interval on every
        registered :class:`ProcessFetchHandler`. The lease must cover one
        heartbeat interval, the verified SQLite pool/lock wait bound, and a
        safety margin. The I/O bound does not cover operating-system disk stalls.

        Args:
            engine: Synchronous engine for the durable warehouse.
            registry: Immutable catalog of explicitly reviewed jobs.
            worker_id: Stable identifier unique to this live worker instance.
            lease_ttl: Maximum time a claim remains owned without renewal.
            heartbeat_interval_seconds: Maximum executor heartbeat interval.
            database_io_timeout_seconds: Maximum verified SQLite pool/lock wait.

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
        _verify_sqlite_wait_bounds(engine, database_io_timeout)
        _verify_registry_heartbeat_intervals(registry, heartbeat_interval)

        self._worker_id = worker_id.strip()
        self._lease_ttl = lease_ttl
        self._ledger = RunLedger(engine, clock_policy="database")
        self._service = RunService(self._ledger, registry)

    @property
    def ledger(self) -> RunLedger:
        """Return the persistent ledger used by this worker."""
        return self._ledger

    def submit(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Persist one validated request using the reviewed run service."""
        return self._service.submit(job_id, version, request)

    def run_once(self) -> RunOutcome | None:
        """Claim at most one durable run and execute it to a fenced outcome."""
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


def _verify_sqlite_wait_bounds(engine: Engine, maximum_wait_seconds: float) -> None:
    """Require file-backed SQLite with bounded pool checkout and lock waits."""
    database = engine.url.database
    if not database or database == ":memory:" or "mode=memory" in database.lower():
        raise ValueError("persistent worker requires a file-backed SQLite database")
    try:
        timeout_method = getattr(engine.pool, "timeout", None)
        if not callable(timeout_method):
            raise ValueError("pool timeout is unavailable")
        pool_timeout = float(timeout_method())
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        raise ValueError("SQLite pool checkout timeout could not be verified") from None
    if not math.isfinite(pool_timeout) or pool_timeout < 0 or pool_timeout > maximum_wait_seconds:
        raise ValueError("SQLite pool checkout timeout exceeds the configured database I/O bound")
    try:
        with engine.connect() as connection:
            busy_timeout_ms = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    except Exception:
        raise ValueError("SQLite busy_timeout could not be verified") from None
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
        if isinstance(fetcher, ProcessFetchHandler) and not math.isclose(
            fetcher.heartbeat_interval_seconds,
            configured_interval,
            rel_tol=0.0,
            abs_tol=0.000_001,
        ):
            raise ValueError("heartbeat_interval_seconds must match every process executor")


__all__ = ["PersistentRunWorker", "ProcessFetchHandler"]
