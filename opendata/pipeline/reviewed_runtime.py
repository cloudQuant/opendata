"""Compose explicit reviewed run dependencies without loading application settings.

The runtime factory accepts a caller-owned SQLAlchemy Engine, static reviewed
task catalog, and validated process-role plan. It does not read URLs, settings,
environment variables, create schema, import job paths, or start a scheduler.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Literal

from sqlalchemy.engine import Engine

from opendata.pipeline.reviewed_worker import PersistentRunWorker
from opendata.pipeline.run_dispatch import (
    APSchedulerSubmissionCallback,
    DispatchReceipt,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_ledger import MAX_TTL, MIN_TTL, RunLedger
from opendata.pipeline.runtime_roles import RuntimeRolePlan, WorkerOnceSPI
from opendata.reviewed_cli import ReviewedCLIApplication

if TYPE_CHECKING:  # pragma: no cover
    from _thread import LockType
    from collections.abc import Mapping

    from opendata.pipeline.run_service import RunOutcome

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,254}$")
_MIN_WORKER_INTERVAL_SECONDS = 0.01
_MAX_HEARTBEAT_INTERVAL_SECONDS = 60.0
_MAX_DATABASE_IO_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class _WorkerBinding:
    """One persistent worker bound to its caller-owned Engine and fixed limits."""

    engine: Engine
    worker_id: str
    lease_ttl: timedelta
    heartbeat_interval_seconds: float
    database_io_timeout_seconds: float
    worker: PersistentRunWorker


@dataclass(slots=True)
class _RuntimeLifecycle:
    """Mutable lifecycle state kept outside the frozen public runtime fields."""

    metadata_engine: Engine
    runtime_plan: RuntimeRolePlan
    lock: LockType = field(default_factory=Lock)
    worker: _WorkerBinding | None = None
    closed: bool = False


@dataclass(frozen=True, slots=True)
class _WorkerOnceAdapter:
    """Expose one fixed persistent worker through the CLI worker SPI."""

    lifecycle: _RuntimeLifecycle
    binding: _WorkerBinding

    def __call__(self, worker: str, ttl: timedelta) -> RunOutcome | None:
        """Run at most one job using the exact settings bound at composition."""
        with self.lifecycle.lock:
            if self.lifecycle.worker is not self.binding:
                raise ValueError("reviewed runtime worker is not active")
            if self.lifecycle.closed:
                raise ValueError("reviewed runtime is closed")
            if worker != self.binding.worker_id or ttl != self.binding.lease_ttl:
                raise ValueError("worker request differs from fixed runtime settings")
            return self.binding.worker.run_once()


class _RuntimeRunDispatcher(RunDispatcher):
    """Keep cached API, scheduler, and CLI dispatchers inactive after close."""

    def __init__(
        self,
        ledger: RunLedger,
        catalog: ReviewedTaskCatalog,
        lifecycle: _RuntimeLifecycle,
    ) -> None:
        """Bind dispatcher operations to the runtime's close state."""
        super().__init__(ledger, catalog)
        self._runtime_lifecycle = lifecycle

    def dispatch_scheduled(
        self,
        job_id: str,
        version: str,
        *,
        occurrence: datetime,
        payload: Mapping[str, object] | None = None,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Enqueue one scheduled run while the owning runtime remains open."""
        with self._runtime_lifecycle.lock:
            self._require_role("scheduler")
            return super().dispatch_scheduled(
                job_id,
                version,
                occurrence=occurrence,
                payload=payload,
                dry_run=dry_run,
            )

    def dispatch_manual(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Enqueue one manual run while the owning runtime remains open."""
        with self._runtime_lifecycle.lock:
            self._require_role("api")
            return super().dispatch_manual(job_id, version, payload, dry_run=dry_run)

    def dispatch_backfill(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Enqueue one backfill while the owning runtime remains open."""
        with self._runtime_lifecycle.lock:
            self._require_role("api")
            return super().dispatch_backfill(job_id, version, payload, dry_run=dry_run)

    def apscheduler_callback(
        self,
        job_id: str,
        version: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> APSchedulerSubmissionCallback:
        """Create a callback that dispatches only while the runtime is open."""
        with self._runtime_lifecycle.lock:
            self._require_role("scheduler")
            return super().apscheduler_callback(job_id, version, payload=payload)

    def _require_role(self, role: Literal["api", "scheduler"]) -> None:
        """Reject dispatch when the runtime is closed or lacks its role."""
        if self._runtime_lifecycle.closed:
            raise RuntimeError("reviewed runtime is closed")
        if not self._runtime_lifecycle.runtime_plan.enables(role):
            raise ValueError(f"reviewed runtime role {role!r} is not enabled")


@dataclass(frozen=True, slots=True, eq=False)
class ReviewedRuntime:
    """Immutable public dependencies for one explicitly reviewed runtime.

    ``ledger`` and ``dispatcher`` borrow the Engine supplied to
    :func:`build_reviewed_runtime`; this object does not dispose that Engine. A
    worker Engine must be created and owned by the caller through an existing
    verified worker-engine factory. Persistent worker listeners remain
    attached to that Engine for its lifetime because the worker has no public
    listener-removal API; this runtime binds at most one worker per instance.
    """

    runtime_plan: RuntimeRolePlan
    catalog: ReviewedTaskCatalog
    dispatcher: RunDispatcher
    ledger: RunLedger
    _lifecycle: _RuntimeLifecycle = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Reject manually assembled runtimes with inconsistent public types."""
        if not isinstance(self.runtime_plan, RuntimeRolePlan):
            raise TypeError("runtime_plan must be a RuntimeRolePlan")
        if self.runtime_plan.profile != "reviewed":
            raise ValueError("reviewed runtime requires an explicit reviewed role plan")
        if not isinstance(self._lifecycle, _RuntimeLifecycle):
            raise TypeError("runtime lifecycle state is invalid")
        if self._lifecycle.runtime_plan is not self.runtime_plan:
            raise ValueError("runtime lifecycle plan must match the reviewed runtime")
        if not isinstance(self.catalog, ReviewedTaskCatalog):
            raise TypeError("catalog must be a ReviewedTaskCatalog")
        if not isinstance(self.dispatcher, _RuntimeRunDispatcher):
            raise TypeError("dispatcher must be a RunDispatcher")
        if self.dispatcher._runtime_lifecycle is not self._lifecycle:
            raise ValueError("dispatcher lifecycle must match the reviewed runtime")
        if not isinstance(self.ledger, RunLedger):
            raise TypeError("ledger must be a RunLedger")

    def to_cli_application(
        self,
        *,
        worker_engine: Engine | None = None,
        worker_id: str | None = None,
        lease_ttl: timedelta | None = None,
        heartbeat_interval_seconds: float | None = None,
        database_io_timeout_seconds: float | None = None,
    ) -> ReviewedCLIApplication:
        """Adapt this runtime to the strict reviewed CLI without starting roles.

        Scheduler plans receive the shared dispatcher but never create or start
        APScheduler. Worker plans require an existing Engine registered by a
        verified worker-engine factory and fixed worker timing values. The
        worker is created at most once for this runtime and reused by later
        adapters, preventing duplicate SQLAlchemy guard listeners.

        Args:
            worker_engine: Caller-owned Engine already verified by
                ``create_worker_engine`` or the verified MySQL worker factory.
                It must be the same Engine object supplied as ``metadata_engine``
                to the runtime factory, so queue and worker cannot target
                different stores.
            worker_id: Fixed worker identity used for every worker-once request.
            lease_ttl: Fixed, whole-second lease passed to the persistent worker.
            heartbeat_interval_seconds: Actual child checkpoint interval.
            database_io_timeout_seconds: Factory-matched maximum database wait.

        Returns:
            A strict CLI application containing only the interfaces enabled by
            ``runtime_plan``.

        Raises:
            TypeError: If an injected Engine has the wrong type.
            ValueError: If the enabled worker role lacks bounded fixed settings,
                or a previously bound worker is reconfigured.
            RuntimeError: If this runtime has already been closed.
        """
        with self._lifecycle.lock:
            if self._lifecycle.closed:
                raise RuntimeError("reviewed runtime is closed")

            worker_options = (
                worker_engine,
                worker_id,
                lease_ttl,
                heartbeat_interval_seconds,
                database_io_timeout_seconds,
            )
            worker_enabled = self.runtime_plan.enables("worker")
            if not worker_enabled and any(value is not None for value in worker_options):
                raise ValueError("worker options require an enabled worker role")

            worker_once: WorkerOnceSPI | None = None
            if worker_enabled:
                if any(value is None for value in worker_options):
                    raise ValueError("enabled worker role requires all fixed worker options")
                if not isinstance(worker_engine, Engine):
                    raise TypeError("worker_engine must be a verified SQLAlchemy Engine")
                if worker_engine is not self._lifecycle.metadata_engine:
                    raise ValueError(
                        "worker_engine must be the exact Engine bound to the run ledger"
                    )
                if not isinstance(worker_id, str) or not _WORKER_ID.fullmatch(worker_id):
                    raise ValueError("worker_id must be a bounded CLI-safe identifier")
                if not isinstance(lease_ttl, timedelta):
                    raise TypeError("lease_ttl must be a timedelta")
                lease_seconds = lease_ttl.total_seconds()
                if (
                    not math.isfinite(lease_seconds)
                    or lease_seconds < MIN_TTL.total_seconds()
                    or lease_seconds > MAX_TTL.total_seconds()
                    or not lease_seconds.is_integer()
                ):
                    raise ValueError(
                        "lease_ttl must be a whole number of seconds within ledger bounds"
                    )
                heartbeat_interval = _bounded_seconds(
                    heartbeat_interval_seconds,
                    "heartbeat_interval_seconds",
                    _MIN_WORKER_INTERVAL_SECONDS,
                    _MAX_HEARTBEAT_INTERVAL_SECONDS,
                )
                database_io_timeout = _bounded_seconds(
                    database_io_timeout_seconds,
                    "database_io_timeout_seconds",
                    _MIN_WORKER_INTERVAL_SECONDS,
                    _MAX_DATABASE_IO_TIMEOUT_SECONDS,
                )
                worker = self._bind_worker(
                    worker_engine,
                    worker_id,
                    lease_ttl,
                    heartbeat_interval,
                    database_io_timeout,
                )
                worker_once = self._worker_once(worker)

            dispatch_enabled = self.runtime_plan.enables("api") or self.runtime_plan.enables(
                "scheduler"
            )
            return ReviewedCLIApplication(
                runtime_plan=self.runtime_plan,
                dispatcher=self.dispatcher if dispatch_enabled else None,
                worker_once=worker_once,
            )

    @property
    def closed(self) -> bool:
        """Return whether this runtime has deactivated its public adapters."""
        with self._lifecycle.lock:
            return self._lifecycle.closed

    def close(self) -> None:
        """Deactivate runtime adapters without disposing caller-owned Engines.

        Worker connection guards remain attached to their Engine for the
        Engine's lifetime because ``PersistentRunWorker`` exposes no public
        listener-removal operation. Engine creation and disposal remain the
        caller's responsibility.
        """
        with self._lifecycle.lock:
            self._lifecycle.closed = True

    def _bind_worker(
        self,
        engine: Engine,
        worker_id: str,
        lease_ttl: timedelta,
        heartbeat_interval_seconds: float,
        database_io_timeout_seconds: float,
    ) -> _WorkerBinding:
        """Create one persistent worker or require an identical prior binding."""
        binding = self._lifecycle.worker
        if binding is not None:
            if (
                binding.engine is not engine
                or binding.worker_id != worker_id
                or binding.lease_ttl != lease_ttl
                or binding.heartbeat_interval_seconds != heartbeat_interval_seconds
                or binding.database_io_timeout_seconds != database_io_timeout_seconds
            ):
                raise ValueError("reviewed runtime worker is already bound to different settings")
            return binding

        worker = PersistentRunWorker(
            engine,
            self.catalog.registry,
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            database_io_timeout_seconds=database_io_timeout_seconds,
        )
        binding = _WorkerBinding(
            engine=engine,
            worker_id=worker_id,
            lease_ttl=lease_ttl,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            database_io_timeout_seconds=database_io_timeout_seconds,
            worker=worker,
        )
        self._lifecycle.worker = binding
        return binding

    def _worker_once(self, binding: _WorkerBinding) -> WorkerOnceSPI:
        """Return a fixed-identity one-run adapter for the strict CLI SPI."""
        return _WorkerOnceAdapter(self._lifecycle, binding)


def build_reviewed_runtime(
    *,
    metadata_engine: Engine,
    catalog: ReviewedTaskCatalog,
    runtime_plan: RuntimeRolePlan,
) -> ReviewedRuntime:
    """Build a reviewed runtime from explicit dependencies and no app settings.

    The process-role plan is validated before the injected Engine or catalog is
    inspected. The returned ledger and dispatcher borrow ``metadata_engine``;
    the caller remains responsible for its lifetime and for creating the
    ledger schema through an explicit migration/bootstrap operation.

    Args:
        metadata_engine: Existing synchronous Engine for the reviewed run
            ledger. It is not disposed and no schema is created here. Worker
            runtimes must use an Engine returned by a verified worker-engine
            factory and pass that same object to ``to_cli_application``.
        catalog: Static immutable catalog containing reviewed job handlers.
        runtime_plan: Explicit reviewed role plan. Worker-only and
            scheduler-only plans are valid; API hosts must separately require
            the API role before serving their routes.

    Returns:
        A frozen runtime containing its role plan, catalog, ledger, and
        dispatcher.

    Raises:
        TypeError: If an injected dependency has an unsupported type.
        ValueError: If the plan is not an explicit reviewed plan.
    """
    if not isinstance(runtime_plan, RuntimeRolePlan):
        raise TypeError("runtime_plan must be a RuntimeRolePlan")
    if runtime_plan.profile != "reviewed":
        raise ValueError("reviewed runtime requires an explicit reviewed role plan")

    if not isinstance(metadata_engine, Engine):
        raise TypeError("metadata_engine must be a synchronous SQLAlchemy Engine")
    if not isinstance(catalog, ReviewedTaskCatalog):
        raise TypeError("catalog must be a ReviewedTaskCatalog")

    ledger = RunLedger(metadata_engine, clock_policy="database")
    lifecycle = _RuntimeLifecycle(
        metadata_engine=metadata_engine,
        runtime_plan=runtime_plan,
    )
    dispatcher = _RuntimeRunDispatcher(ledger, catalog, lifecycle)
    return ReviewedRuntime(
        runtime_plan=runtime_plan,
        catalog=catalog,
        dispatcher=dispatcher,
        ledger=ledger,
        _lifecycle=lifecycle,
    )


def _bounded_seconds(
    value: float | None,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Validate finite non-boolean worker time bounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        normalized = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return normalized


__all__ = ["ReviewedRuntime", "build_reviewed_runtime"]
