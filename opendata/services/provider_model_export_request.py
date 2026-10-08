"""Bounded async ownership and request validation for native model exports."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from opendata.data import async_execution, domains
from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudgetError,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestOperation,
    check_scope_liveness,
    current_request_scopes,
    request_execution_scope,
    validate_scope_grants,
)
from opendata.services import provider_model_export, provider_model_store
from opendata.services.provider_model_read import _validate_filters, _validate_range
from opendata.services.provider_model_warehouse import (
    _annotation_contains_date,
    _canonical_date,
)
from opendata.services.provider_models import (
    ProviderModelIdentityError,
    ProviderModelNotFoundError,
    find_provider_model,
    validate_model_identity,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import date
    from threading import Event

    from sqlalchemy.engine import Engine

    from opendata.data.models.base import ContractModel
    from opendata.data.registry import ProviderRegistry
    from opendata.services.provider_model_export import NativeModelExportArtifact


_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_RECORDS = 200_000
_MAX_BYTES = 64 * 1024 * 1024
_MODEL_DOMAINS: dict[tuple[str, str], str] = {
    ("fred", "FredSeries"): "fred_series",
    ("bls", "BlsSeries"): "bls_series",
    ("fmp", "EquityHistorical"): "equity_historical",
}
_QUERY_FIELDS = frozenset({"filters", "start", "end", "max_records", "max_bytes"})
_POLL_INTERVAL_SECONDS = 0.01
_MAX_ACTIVE_EXPORT_SPOOLS = 8
_SPOOL_PERMIT_LOCK = threading.Lock()
_ACTIVE_EXPORT_SPOOLS = 0


class _DomainPrincipal(Protocol):
    def allows_domain(self, domain: str) -> bool: ...


class ProviderModelExportRequestError(RuntimeError):
    """Base class for fixed-status export request failures."""


class ProviderModelExportRequestIdentityError(ProviderModelExportRequestError):
    """A provider or model path component is malformed."""


class ProviderModelExportRequestNotFoundError(ProviderModelExportRequestError):
    """The exact provider/model identity is not registered for export."""


class ProviderModelExportRequestForbiddenError(ProviderModelExportRequestError):
    """The principal or request grant does not authorize export."""


class ProviderModelExportRequestValidationError(ProviderModelExportRequestError):
    """The bounded export request contains invalid fields."""


class ProviderModelExportRequestLimitError(ProviderModelExportRequestError):
    """An explicit row or byte limit exceeds the supported cap."""


class ProviderModelExportRequestTimeoutError(ProviderModelExportRequestError, TimeoutError):
    """The original request deadline expired."""


class ProviderModelExportRequestCancelledError(ProviderModelExportRequestError):
    """The export request was cancelled."""


class ProviderModelExportRequestUnavailableError(ProviderModelExportRequestError):
    """The reviewed native export is temporarily unavailable."""


class _CombinedCancellation:
    """Join the bounded worker signal with the trusted request cancellation."""

    def __init__(self, *signals: Event | _CombinedCancellation | None) -> None:
        self._signals = tuple(signal for signal in signals if signal is not None)
        self._local = threading.Event()

    def is_set(self) -> bool:
        return self._local.is_set() or any(signal.is_set() for signal in self._signals)

    def set(self) -> None:
        self._local.set()


class _SpoolPermit:
    """One process-local slot held for a worker or live response artifact."""

    def __init__(self) -> None:
        self._released = False

    def release(self) -> None:
        """Release this slot once after its worker and spool are terminal."""
        global _ACTIVE_EXPORT_SPOOLS
        with _SPOOL_PERMIT_LOCK:
            if self._released:
                return
            self._released = True
            _ACTIVE_EXPORT_SPOOLS -= 1


@dataclass(slots=True)
class NativeModelExportLease:
    """Transferred ownership of a completed export and its original liveness."""

    artifact: NativeModelExportArtifact
    scopes: tuple[RequestExecutionScope, ...]
    deadline: float | None
    _permit: _SpoolPermit = field(repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cleaned: bool = field(default=False, repr=False)

    def check_live(self) -> None:
        """Recheck original cancellation, deadline, and exact EXPORT grants."""
        check_scope_liveness(self.scopes)
        validate_scope_grants(self.scopes)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RequestExecutionDeadlineError("export request deadline expired")

    def cleanup(self) -> None:
        """Release the artifact's private spool exactly once."""
        with self._lock:
            if self._cleaned:
                return
            self._cleaned = True
        with suppress(Exception):
            self.artifact.cleanup()
        self._permit.release()


class _ExportArtifactOwner:
    """Close the race between worker completion, caller cancellation, and handoff."""

    def __init__(self, permit: _SpoolPermit) -> None:
        self._lock = threading.Lock()
        self._artifact: NativeModelExportArtifact | None = None
        self._permit: _SpoolPermit | None = permit
        self._abandoned = False
        self._transferred = False
        self._worker_done = False
        self._runner_task: asyncio.Task[Any] | None = None

    @property
    def abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    def register(self, artifact: NativeModelExportArtifact) -> bool:
        """Take worker ownership, or dispose a result that arrived too late."""
        with self._lock:
            accepted = (
                not self._abandoned
                and not self._worker_done
                and self._artifact is None
                and not self._transferred
            )
            if accepted:
                self._artifact = artifact
        if not accepted:
            with suppress(Exception):
                artifact.cleanup()
        return accepted

    def abandon(self) -> None:
        """Mark the request gone and clean an artifact already registered."""
        with self._lock:
            self._abandoned = True
            artifact = self._artifact
            self._artifact = None
            permit = self._permit if self._worker_done else None
            if permit is not None:
                self._permit = None
        if artifact is not None:
            with suppress(Exception):
                artifact.cleanup()
        if permit is not None:
            permit.release()

    def worker_terminal(self) -> None:
        """Release the permit after an abandoned or artifact-free worker ends."""
        with self._lock:
            if self._worker_done:
                return
            self._worker_done = True
            release = self._abandoned or (self._artifact is None and not self._transferred)
            permit = self._permit if release else None
            if permit is not None:
                self._permit = None
            artifact = self._artifact if self._abandoned else None
            if artifact is not None:
                self._artifact = None
        if artifact is not None:
            with suppress(Exception):
                artifact.cleanup()
        if permit is not None:
            permit.release()

    def retain_runner_task(self, task: asyncio.Task[Any]) -> None:
        """Keep an abandoned bounded coroutine alive through real worker exit."""
        with self._lock:
            self._runner_task = task

    def runner_terminal(self, _task: asyncio.Task[Any]) -> None:
        """Release a permit if the bounded runner failed before invoking work."""
        self.worker_terminal()
        with self._lock:
            self._runner_task = None

    def transfer(
        self,
        artifact: NativeModelExportArtifact,
        *,
        scopes: tuple[RequestExecutionScope, ...],
        deadline: float | None,
    ) -> NativeModelExportLease:
        """Atomically hand one successful worker result to the caller."""
        with self._lock:
            if (
                self._abandoned
                or not self._worker_done
                or self._transferred
                or self._artifact is not artifact
                or self._permit is None
            ):
                accepted = False
                permit = None
            else:
                accepted = True
                self._artifact = None
                self._transferred = True
                permit = self._permit
                self._permit = None
        if not accepted:
            with suppress(Exception):
                artifact.cleanup()
            raise ProviderModelExportRequestCancelledError from None
        if permit is None:
            raise ProviderModelExportRequestCancelledError from None
        return NativeModelExportLease(
            artifact=artifact,
            scopes=scopes,
            deadline=deadline,
            _permit=permit,
        )


async def _acquire_spool_permit(
    scopes: tuple[RequestExecutionScope, ...],
    deadline: float,
) -> _SpoolPermit:
    """Wait asynchronously for one process-local spool slot under request liveness."""
    global _ACTIVE_EXPORT_SPOOLS
    while True:
        check_scope_liveness(scopes)
        validate_scope_grants(scopes)
        with _SPOOL_PERMIT_LOCK:
            if _ACTIVE_EXPORT_SPOOLS < _MAX_ACTIVE_EXPORT_SPOOLS:
                _ACTIVE_EXPORT_SPOOLS += 1
                return _SpoolPermit()
        remaining = max(0.0, deadline - time.monotonic())
        if remaining == 0:
            raise RequestExecutionDeadlineError
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _trusted_context(ctx: FetchContext | None) -> FetchContext:
    try:
        return provider_model_export._trusted_export_context(ctx)
    except Exception:
        raise ProviderModelExportRequestValidationError from None


def _check_inherited_identity(
    scopes: tuple[RequestExecutionScope, ...], *, source: str, model: str
) -> None:
    for scope in scopes:
        if (
            scope.source != source
            or scope.canonical_model != model
            or scope.operation is not RequestOperation.EXPORT
        ):
            raise ProviderModelExportRequestForbiddenError from None


def _scope_deadline(
    context: FetchContext,
    admission_started: float,
    inherited: tuple[RequestExecutionScope, ...],
) -> float:
    try:
        deadline = provider_model_store._context_deadline(context, admission_started, inherited)
    except RequestExecutionDeadlineError:
        raise ProviderModelExportRequestTimeoutError from None
    except Exception:
        raise ProviderModelExportRequestValidationError from None
    if deadline is None or not math.isfinite(deadline):
        raise ProviderModelExportRequestValidationError from None
    return deadline


def _preflight_model(
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
) -> tuple[str, type[ContractModel]]:
    try:
        validate_model_identity(source, model)
    except (ProviderModelIdentityError, TypeError, ValueError):
        raise ProviderModelExportRequestIdentityError from None
    try:
        find_provider_model(registry, source, model)
    except ProviderModelNotFoundError:
        raise ProviderModelExportRequestNotFoundError from None
    except Exception:
        raise ProviderModelExportRequestUnavailableError from None
    domain = _MODEL_DOMAINS.get((source, model))
    if domain is None:
        raise ProviderModelExportRequestNotFoundError from None
    try:
        _, _, contract_type, _, _, _ = provider_model_export._reviewed_export_profile(
            registry=registry,
            source=source,
            model=model,
            principal=principal,
        )
    except provider_model_export.ProviderModelExportForbiddenError:
        raise ProviderModelExportRequestForbiddenError from None
    except provider_model_export.ProviderModelExportValidationError:
        raise ProviderModelExportRequestUnavailableError from None
    except Exception:
        raise ProviderModelExportRequestUnavailableError from None
    return domain, contract_type


def _query_limits(query: dict[str, object]) -> tuple[int, int]:
    raw_records = query.get("max_records", _MAX_RECORDS)
    raw_bytes = query.get("max_bytes", _MAX_BYTES)
    if type(raw_records) is not int or type(raw_bytes) is not int:
        raise ProviderModelExportRequestValidationError from None
    if not 1 <= raw_records <= _MAX_RECORDS or not 1 <= raw_bytes <= _MAX_BYTES:
        raise ProviderModelExportRequestLimitError from None
    return raw_records, raw_bytes


def _prepare_export_query(
    *,
    query: Mapping[str, object],
    domain: str,
    contract_type: type[ContractModel],
) -> tuple[dict[str, object], date | int | None, date | int | None, int, int]:
    if type(query) is not dict or any(type(key) is not str for key in query):
        raise ProviderModelExportRequestValidationError from None
    copied = dict(query)
    if set(copied) - _QUERY_FIELDS:
        raise ProviderModelExportRequestValidationError from None
    raw_filters = copied.get("filters", {})
    if type(raw_filters) is not dict or any(type(key) is not str for key in raw_filters):
        raise ProviderModelExportRequestValidationError from None
    filters: dict[str, object] = {}
    for field_name, value in raw_filters.items():
        if (
            field_name in contract_type.model_fields
            and _annotation_contains_date(
                contract_type.model_fields[field_name].rebuild_annotation()
            )
            and type(value) is str
        ):
            try:
                value = _canonical_date(value)
            except Exception:
                raise ProviderModelExportRequestValidationError from None
        filters[field_name] = value

    spec = domains.require_domain_semantics(domain)
    if spec.time_field == "date":
        try:
            start: date | int | None = _canonical_date(copied.get("start"))
            end: date | int | None = _canonical_date(copied.get("end"))
        except Exception:
            raise ProviderModelExportRequestValidationError from None
    elif spec.time_field == "year":
        start_value = copied.get("start")
        end_value = copied.get("end")
        if (start_value is not None and type(start_value) is not int) or (
            end_value is not None and type(end_value) is not int
        ):
            raise ProviderModelExportRequestValidationError from None
        start = start_value
        end = end_value
    else:
        raise ProviderModelExportRequestUnavailableError from None

    try:
        validated_filters = _validate_filters(domain, filters, spec, contract_type)
        validated_start, validated_end = _validate_range(
            domain,
            contract_type,
            start,
            end,
        )
    except Exception:
        raise ProviderModelExportRequestValidationError from None
    max_records, max_bytes = _query_limits(copied)
    return validated_filters, validated_start, validated_end, max_records, max_bytes


async def await_provider_model_export_request_content(
    read_content: Callable[[], Awaitable[bytes]],
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
    ctx: FetchContext | None,
    admission_started: float,
) -> bytes:
    """Read a bounded body under the same deadline, identity, ACL, and EXPORT grant."""
    context = _trusted_context(ctx)
    inherited = current_request_scopes()
    _check_inherited_identity(inherited, source=source, model=model)
    deadline = _scope_deadline(context, admission_started, inherited)
    try:
        with request_execution_scope(
            source=source,
            canonical_model=model,
            operation=RequestOperation.EXPORT,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
            inherited_scopes=inherited,
        ):
            scopes = current_request_scopes()
            _preflight_model(
                registry=registry,
                source=source,
                model=model,
                principal=principal,
            )
            validate_scope_grants(scopes)
            task = asyncio.ensure_future(read_content())

            async def cancel_and_drain() -> None:
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

            try:
                while not task.done():
                    check_scope_liveness(scopes)
                    validate_scope_grants(scopes)
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0:
                        raise RequestExecutionDeadlineError
                    await asyncio.wait((task,), timeout=min(_POLL_INTERVAL_SECONDS, remaining))
                result = await task
                check_scope_liveness(scopes)
                validate_scope_grants(scopes)
                return result
            except asyncio.CancelledError:
                await cancel_and_drain()
                raise
            except BaseException:
                await cancel_and_drain()
                raise
    except ProviderModelExportRequestError:
        raise
    except provider_model_export.ProviderModelExportForbiddenError:
        raise ProviderModelExportRequestForbiddenError from None
    except RequestExecutionDeadlineError:
        raise ProviderModelExportRequestTimeoutError from None
    except RequestExecutionCancelledError:
        raise ProviderModelExportRequestCancelledError from None
    except (RequestAuthorizationError, RequestBudgetScopeError, RequestBudgetError):
        raise ProviderModelExportRequestForbiddenError from None


async def _await_worker(
    *,
    owner: _ExportArtifactOwner,
    operation: Callable[[Event, float | None], NativeModelExportArtifact],
    deadline: float,
    scopes: tuple[RequestExecutionScope, ...],
    request_cancel: threading.Event,
) -> NativeModelExportArtifact:
    # Use no internal runner deadline: the caller owns deadline/cancellation
    # admission, while this task must remain attached until its sync worker
    # actually terminates so the spool permit cannot be released early.
    task = asyncio.create_task(async_execution.run_bounded(owner, operation, None))
    owner.retain_runner_task(task)
    task.add_done_callback(owner.runner_terminal)

    def consume_late_result(done_task: asyncio.Task[NativeModelExportArtifact]) -> None:
        with suppress(BaseException):
            done_task.exception()

    def abandon_worker() -> None:
        # Keep run_bounded alive: cancelling its waiter can detach a still-running
        # thread, and an operation queued for shared capacity must still enter,
        # observe request_cancel, and release this request's permit at termination.
        request_cancel.set()
        owner.abandon()
        task.add_done_callback(consume_late_result)

    try:
        while not task.done():
            check_scope_liveness(scopes)
            validate_scope_grants(scopes)
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise RequestExecutionDeadlineError
            await asyncio.wait((task,), timeout=min(_POLL_INTERVAL_SECONDS, remaining))
        return await task
    except asyncio.CancelledError:
        abandon_worker()
        raise
    except RequestExecutionDeadlineError:
        abandon_worker()
        raise
    except RequestExecutionCancelledError:
        abandon_worker()
        raise
    except (RequestAuthorizationError, RequestBudgetScopeError, RequestBudgetError):
        abandon_worker()
        raise
    except async_execution.AsyncExecutionTimeoutError:
        abandon_worker()
        raise RequestExecutionDeadlineError from None


async def export_provider_model_request(
    *,
    engine_factory: Callable[[], Engine],
    registry: ProviderRegistry,
    source: str,
    model: str,
    query: Mapping[str, object],
    principal: _DomainPrincipal,
    ctx: FetchContext | None = None,
    admission_started: float | None = None,
) -> NativeModelExportLease:
    """Return a completed export lease, running every sync operation in run_bounded."""
    started = time.monotonic() if admission_started is None else admission_started
    if (
        isinstance(started, bool)
        or not isinstance(started, (int, float))
        or not math.isfinite(float(started))
    ):
        raise ProviderModelExportRequestValidationError from None
    if not callable(engine_factory):
        raise ProviderModelExportRequestUnavailableError from None

    context = _trusted_context(ctx)
    inherited = current_request_scopes()
    _check_inherited_identity(inherited, source=source, model=model)
    deadline = _scope_deadline(context, float(started), inherited)
    owner: _ExportArtifactOwner | None = None
    transferred = False
    try:
        with request_execution_scope(
            source=source,
            canonical_model=model,
            operation=RequestOperation.EXPORT,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
            inherited_scopes=inherited,
        ):
            scopes = current_request_scopes()
            domain, contract_type = _preflight_model(
                registry=registry,
                source=source,
                model=model,
                principal=principal,
            )
            filters, start, end, max_records, max_bytes = _prepare_export_query(
                query=query,
                domain=domain,
                contract_type=contract_type,
            )
            check_scope_liveness(scopes)
            validate_scope_grants(scopes)
            permit = await _acquire_spool_permit(scopes, deadline)
            active_owner = _ExportArtifactOwner(permit)
            owner = active_owner
            request_cancel = threading.Event()

            def run_export(
                worker_cancel: Event,
                _worker_deadline: float | None,
            ) -> NativeModelExportArtifact:
                try:
                    joined_cancel = _CombinedCancellation(
                        worker_cancel,
                        context._thread_cancel_event,
                        request_cancel,
                    )
                    worker_context = FetchContext(
                        _deadline_monotonic=deadline,
                        _thread_cancel_event=cast("Event", joined_cancel),
                        request_budget=context.request_budget,
                        operation=RequestOperation.EXPORT,
                    )
                    with request_execution_scope(
                        source=source,
                        canonical_model=model,
                        operation=RequestOperation.EXPORT,
                        budget=worker_context.request_budget,
                        deadline=deadline,
                        cancellation=cast("Event", joined_cancel),
                        inherited_scopes=scopes,
                    ):
                        worker_scopes = current_request_scopes()
                        check_scope_liveness(worker_scopes)
                        validate_scope_grants(worker_scopes)
                        artifact: NativeModelExportArtifact | None = None
                        try:
                            artifact = provider_model_export.export_provider_model_snapshot(
                                engine_factory=engine_factory,
                                registry=registry,
                                source=source,
                                model=model,
                                principal=principal,
                                filters=filters,
                                start=start,
                                end=end,
                                ctx=worker_context,
                                max_records=max_records,
                                max_bytes=max_bytes,
                            )
                            check_scope_liveness(worker_scopes)
                            validate_scope_grants(worker_scopes)
                            if joined_cancel.is_set():
                                raise ProviderModelExportRequestCancelledError from None
                            if not active_owner.register(artifact):
                                artifact = None
                                raise ProviderModelExportRequestCancelledError from None
                            transferred_to_owner = artifact
                            artifact = None
                            return transferred_to_owner
                        except BaseException:
                            if artifact is not None:
                                with suppress(Exception):
                                    artifact.cleanup()
                            raise
                finally:
                    active_owner.worker_terminal()

            artifact = await _await_worker(
                owner=active_owner,
                operation=run_export,
                deadline=deadline,
                scopes=scopes,
                request_cancel=request_cancel,
            )
            check_scope_liveness(scopes)
            validate_scope_grants(scopes)
            lease = active_owner.transfer(artifact, scopes=scopes, deadline=deadline)
            transferred = True
            return lease
    except asyncio.CancelledError:
        if owner is not None:
            owner.abandon()
        raise
    except ProviderModelExportRequestError:
        if owner is not None:
            owner.abandon()
        raise
    except provider_model_export.ProviderModelExportForbiddenError:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestForbiddenError from None
    except provider_model_export.ProviderModelExportLimitError:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestLimitError from None
    except provider_model_export.ProviderModelExportValidationError:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestValidationError from None
    except (RequestExecutionDeadlineError, async_execution.AsyncExecutionTimeoutError):
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestTimeoutError from None
    except RequestExecutionCancelledError:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestCancelledError from None
    except (RequestAuthorizationError, RequestBudgetScopeError, RequestBudgetError):
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestForbiddenError from None
    except provider_model_export.ProviderModelExportError:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestUnavailableError from None
    except Exception:
        if owner is not None:
            owner.abandon()
        raise ProviderModelExportRequestUnavailableError from None
    finally:
        if not transferred and owner is not None:
            owner.abandon()


__all__ = [
    "NativeModelExportLease",
    "ProviderModelExportRequestCancelledError",
    "ProviderModelExportRequestError",
    "ProviderModelExportRequestForbiddenError",
    "ProviderModelExportRequestIdentityError",
    "ProviderModelExportRequestLimitError",
    "ProviderModelExportRequestNotFoundError",
    "ProviderModelExportRequestTimeoutError",
    "ProviderModelExportRequestUnavailableError",
    "ProviderModelExportRequestValidationError",
    "await_provider_model_export_request_content",
    "export_provider_model_request",
]
