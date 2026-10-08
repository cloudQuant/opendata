"""Governed, bounded reads from reviewed native provider-model tables."""

from __future__ import annotations

import asyncio
import math
import re
import time
import types
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Annotated, Protocol, TypeVar, Union, cast, get_args, get_origin

from opendata.data import async_execution, domains
from opendata.data.async_execution import AsyncExecutionTimeoutError
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
from opendata.pipeline import provider_model_codec
from opendata.services import provider_model_read, provider_model_store
from opendata.services.provider_model_query import (
    ProviderModelQueryUnavailableError,
    _reviewed_query_contract,
    _serialize_contract_row,
)
from opendata.services.provider_models import (
    ProviderModelIdentityError,
    ProviderModelNotFoundError,
    find_provider_model,
    validate_model_identity,
)

if TYPE_CHECKING:
    from threading import Event

    from sqlalchemy.engine import Engine

    from opendata.data.models.base import ContractModel
    from opendata.data.registry import ProviderRegistry


_DEFAULT_TIMEOUT_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.01
_QUERY_FIELDS = frozenset({"filters", "start", "end", "limit", "offset"})
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_RequestContent = TypeVar("_RequestContent")


class _DomainPrincipal(Protocol):
    """The authorization surface required by a local warehouse read."""

    def allows_domain(self, domain: str) -> bool: ...


class ProviderModelWarehouseIdentityError(ValueError):
    """The request does not identify one supported provider model."""


class ProviderModelWarehouseNotFoundError(LookupError):
    """The exact supported model is not registered in the supplied registry."""


class ProviderModelWarehouseForbiddenError(PermissionError):
    """The authenticated principal cannot read this domain."""


class ProviderModelWarehouseAuthorizationError(PermissionError):
    """No current exact QUERY grant authorizes this warehouse read."""


class ProviderModelWarehouseValidationError(ValueError):
    """The strict bounded page request is invalid."""


class ProviderModelWarehouseConfigurationError(RuntimeError):
    """Registry or reviewed domain configuration cannot serve the read."""


class ProviderModelWarehouseTimeoutError(TimeoutError):
    """The bounded warehouse read exceeded its admission deadline."""


class ProviderModelWarehouseCancelledError(RuntimeError):
    """The bounded warehouse read was cancelled before completion."""


class ProviderModelWarehouseUnavailableError(RuntimeError):
    """The reviewed native warehouse page could not be read safely."""


async def await_warehouse_request_content(
    read_content: Callable[[], Awaitable[_RequestContent]],
    *,
    ctx: FetchContext | None,
    admission_started: float,
) -> _RequestContent:
    """Bound pre-service async request reads by the same admission liveness.

    ASGI body receive happens before the main service call, but it still
    consumes the request's original timeout and inherited deadline/cancel
    signals. The content task is cancelled and drained on every early exit.
    """
    context = _trusted_context(ctx)
    inherited = current_request_scopes()
    try:
        deadline = _effective_deadline(context, admission_started, inherited)
    except RequestExecutionDeadlineError:
        raise ProviderModelWarehouseTimeoutError from None

    def check_liveness() -> None:
        check_scope_liveness(inherited)
        if context._thread_cancel_event is not None and context._thread_cancel_event.is_set():
            raise RequestExecutionCancelledError("warehouse request was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise RequestExecutionDeadlineError("warehouse request deadline expired")

    task = asyncio.ensure_future(read_content())

    async def cancel_and_drain() -> None:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    try:
        while not task.done():
            check_liveness()
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                raise RequestExecutionDeadlineError("warehouse request deadline expired")
            wait_for = (
                _POLL_INTERVAL_SECONDS
                if remaining is None
                else min(_POLL_INTERVAL_SECONDS, remaining)
            )
            await asyncio.wait((task,), timeout=wait_for)
        check_liveness()
        return task.result()
    except RequestExecutionCancelledError:
        await cancel_and_drain()
        raise ProviderModelWarehouseCancelledError from None
    except RequestExecutionDeadlineError:
        await cancel_and_drain()
        raise ProviderModelWarehouseTimeoutError from None
    except asyncio.CancelledError:
        await cancel_and_drain()
        raise
    except BaseException:
        await cancel_and_drain()
        raise


class _WarehouseReadOwner:
    """A distinct run_bounded owner per call, avoiding global read serialization."""


class _CombinedCancellation:
    """Expose multiple trusted cancellation signals as one Event-like view."""

    def __init__(self, *events: Event | None) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


def _trusted_context(ctx: FetchContext | None) -> FetchContext:
    """Use a finite production timeout and accept only internal QUERY contexts."""
    if ctx is None:
        return FetchContext(timeout=_DEFAULT_TIMEOUT_SECONDS, operation=RequestOperation.QUERY)
    if (
        not isinstance(ctx, FetchContext)
        or ctx.operation is not RequestOperation.QUERY
        or isinstance(ctx.timeout, bool)
        or (
            ctx.timeout is not None
            and (
                not isinstance(ctx.timeout, (int, float))
                or not math.isfinite(float(ctx.timeout))
                or ctx.timeout <= 0
            )
        )
    ):
        raise ProviderModelWarehouseValidationError from None
    if ctx.timeout is None:
        return FetchContext(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            _deadline_monotonic=ctx._deadline_monotonic,
            _thread_cancel_event=ctx._thread_cancel_event,
            request_budget=ctx.request_budget,
            operation=RequestOperation.QUERY,
        )
    return ctx


def _resolve_reviewed_model(
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
) -> tuple[str, bool, type[ContractModel]]:
    """Require one of the three reviewed identities and complete DWD semantics."""
    try:
        validate_model_identity(source, model)
    except ProviderModelIdentityError:
        raise ProviderModelWarehouseIdentityError from None

    if (source, model) not in provider_model_store._MODEL_DOMAINS:
        raise ProviderModelWarehouseNotFoundError from None

    try:
        domain = provider_model_store._registered_domain(registry, source, model)
        descriptor = find_provider_model(registry, source, model)
    except ProviderModelNotFoundError:
        raise ProviderModelWarehouseNotFoundError from None
    except Exception:
        raise ProviderModelWarehouseConfigurationError from None

    expected_identity = provider_model_store._MODEL_DOMAINS[(source, model)][1]
    if (
        descriptor.source != source
        or descriptor.model != model
        or descriptor.domain != domain
        or descriptor.full_capability_identity != expected_identity
        or type(descriptor.verified) is not bool
    ):
        raise ProviderModelWarehouseConfigurationError from None

    try:
        allowed = principal.allows_domain(domain)
    except Exception:
        raise ProviderModelWarehouseConfigurationError from None
    if not allowed:
        raise ProviderModelWarehouseForbiddenError from None

    try:
        spec, contract_type = _reviewed_query_contract(domain)
        provider_model_store._expected_columns(domain)
        provider_model_codec.physical_model_key(domain)
    except ProviderModelQueryUnavailableError:
        raise ProviderModelWarehouseConfigurationError from None
    except Exception:
        raise ProviderModelWarehouseConfigurationError from None

    if spec.storage_mode != "upsert" or "query" not in spec.permissions:
        raise ProviderModelWarehouseConfigurationError from None
    if spec.contract != contract_type.__name__:
        raise ProviderModelWarehouseConfigurationError from None
    return domain, descriptor.verified, contract_type


def _canonical_date(value: object) -> date | None:
    """Convert only an exact canonical YYYY-MM-DD string for HTTP date bounds."""
    if value is None:
        return None
    if type(value) is not str or _ISO_DATE.fullmatch(value) is None:
        raise ProviderModelWarehouseValidationError from None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ProviderModelWarehouseValidationError from None
    if parsed.isoformat() != value:
        raise ProviderModelWarehouseValidationError from None
    return parsed


def _annotation_contains_date(annotation: object) -> bool:
    """Find date-valued fields through Annotated and nullable unions."""
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return bool(args) and _annotation_contains_date(args[0])
    if origin in (Union, types.UnionType):
        return any(_annotation_contains_date(argument) for argument in args)
    return annotation is date


def _prepare_query(
    *,
    query: Mapping[str, object],
    domain: str,
    contract_type: type[ContractModel],
) -> tuple[dict[str, object], date | int | None, date | int | None, int, int]:
    """Validate all caller-controlled page fields before warehouse admission."""
    if not isinstance(query, Mapping) or any(type(key) is not str for key in query):
        raise ProviderModelWarehouseValidationError from None
    try:
        copied = dict(query)
    except Exception:
        raise ProviderModelWarehouseValidationError from None
    if set(copied) - _QUERY_FIELDS:
        raise ProviderModelWarehouseValidationError from None

    filters = copied.get("filters", {})
    if type(filters) is not dict:
        raise ProviderModelWarehouseValidationError from None
    try:
        spec = domains.require_domain_semantics(domain)
        normalized_filters: dict[str, object] = {}
        for field_name, value in filters.items():
            if (
                type(field_name) is str
                and field_name in contract_type.model_fields
                and _annotation_contains_date(
                    contract_type.model_fields[field_name].rebuild_annotation()
                )
                and type(value) is str
            ):
                value = _canonical_date(value)
            normalized_filters[field_name] = value
        validated_filters = provider_model_read._validate_filters(
            domain,
            normalized_filters,
            spec,
            contract_type,
        )
        raw_start = copied.get("start")
        raw_end = copied.get("end")
        if spec.time_field == "date":
            start: date | int | None = _canonical_date(raw_start)
            end: date | int | None = _canonical_date(raw_end)
        elif spec.time_field == "year":
            if raw_start is not None and type(raw_start) is not int:
                raise ProviderModelWarehouseValidationError
            if raw_end is not None and type(raw_end) is not int:
                raise ProviderModelWarehouseValidationError
            start = raw_start
            end = raw_end
        else:
            raise ProviderModelWarehouseConfigurationError
        validated_start, validated_end = provider_model_read._validate_range(
            domain,
            contract_type,
            start,
            end,
        )
        raw_limit = copied.get("limit", 1_000)
        raw_offset = copied.get("offset", 0)
        if type(raw_limit) is not int or type(raw_offset) is not int:
            raise ProviderModelWarehouseValidationError
        limit, offset = provider_model_read._validate_page_bounds(raw_limit, raw_offset)
    except ProviderModelWarehouseValidationError:
        raise
    except ProviderModelWarehouseConfigurationError:
        raise
    except Exception:
        raise ProviderModelWarehouseValidationError from None
    return validated_filters, validated_start, validated_end, limit, offset


def _effective_deadline(
    context: FetchContext,
    admission_started: float,
    inherited: tuple[RequestExecutionScope, ...],
) -> float | None:
    try:
        return provider_model_store._context_deadline(context, admission_started, inherited)
    except RequestExecutionDeadlineError:
        raise
    except Exception:
        raise ProviderModelWarehouseValidationError from None


async def _await_worker(
    *,
    owner: _WarehouseReadOwner,
    operation: Callable[[Event, float | None], tuple[ContractModel, ...]],
    deadline: float | None,
    scopes: tuple[RequestExecutionScope, ...],
) -> tuple[ContractModel, ...]:
    """Await shared bounded work while preserving slots after caller exit."""
    task = asyncio.create_task(async_execution.run_bounded(owner, operation, deadline))

    async def cancel_worker_wait() -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        while not task.done():
            try:
                check_scope_liveness(scopes)
            except RequestExecutionCancelledError:
                await cancel_worker_wait()
                raise
            except RequestExecutionDeadlineError:
                await cancel_worker_wait()
                raise

            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0:
                await cancel_worker_wait()
                raise RequestExecutionDeadlineError("warehouse query deadline expired")
            wait_for = (
                _POLL_INTERVAL_SECONDS
                if remaining is None
                else min(
                    _POLL_INTERVAL_SECONDS,
                    remaining,
                )
            )
            try:
                await asyncio.wait((task,), timeout=wait_for)
            except asyncio.CancelledError:
                await cancel_worker_wait()
                raise
        return await task
    except asyncio.CancelledError:
        if not task.done():
            await cancel_worker_wait()
        raise


async def query_provider_model_warehouse(
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    query: Mapping[str, object],
    principal: _DomainPrincipal,
    engine_factory: Callable[[], Engine],
    ctx: FetchContext | None = None,
    _admission_started: float | None = None,
) -> dict[str, object]:
    """Read one typed page without calling a source fetcher or writing data.

    The engine factory runs only inside the shared bounded worker and only
    after exact identity, domain ACL, request-grant, schema, and query checks.
    The default context is finite and has no caller grant; a trusted internal
    dependency may inject an explicit local QUERY grant for tests.
    """
    admission_started = time.monotonic() if _admission_started is None else _admission_started
    if (
        isinstance(admission_started, bool)
        or not isinstance(admission_started, (int, float))
        or not math.isfinite(float(admission_started))
    ):
        raise ProviderModelWarehouseValidationError from None
    if not callable(engine_factory):
        raise ProviderModelWarehouseConfigurationError from None

    context = _trusted_context(ctx)
    inherited = current_request_scopes()
    try:
        deadline = _effective_deadline(context, float(admission_started), inherited)
    except RequestExecutionDeadlineError:
        raise ProviderModelWarehouseTimeoutError from None

    try:
        with request_execution_scope(
            source=source,
            canonical_model=model,
            operation=RequestOperation.QUERY,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
            inherited_scopes=inherited,
        ):
            scopes = current_request_scopes()
            validate_scope_grants(scopes)
            domain, verified, contract_type = _resolve_reviewed_model(
                registry=registry,
                source=source,
                model=model,
                principal=principal,
            )
            filters, start, end, limit, offset = _prepare_query(
                query=query,
                domain=domain,
                contract_type=contract_type,
            )
            validate_scope_grants(scopes)

            def read_page(
                worker_cancel: Event,
                worker_deadline: float | None,
            ) -> tuple[ContractModel, ...]:
                joined_cancel = _CombinedCancellation(
                    worker_cancel,
                    context._thread_cancel_event,
                )
                reader_context = FetchContext(
                    _deadline_monotonic=worker_deadline,
                    _thread_cancel_event=cast("Event", joined_cancel),
                    request_budget=context.request_budget,
                    operation=RequestOperation.QUERY,
                )
                with request_execution_scope(
                    source=source,
                    canonical_model=model,
                    operation=RequestOperation.QUERY,
                    budget=context.request_budget,
                    deadline=worker_deadline,
                    cancellation=cast("Event", joined_cancel),
                    inherited_scopes=scopes,
                ):
                    worker_scopes = current_request_scopes()
                    validate_scope_grants(worker_scopes)
                    engine = engine_factory()
                    validate_scope_grants(worker_scopes)
                    rows = provider_model_read.read_provider_model_rows(
                        engine=engine,
                        registry=registry,
                        source=source,
                        model=model,
                        filters=filters,
                        start=start,
                        end=end,
                        limit=limit,
                        offset=offset,
                        ctx=reader_context,
                    )
                    validate_scope_grants(worker_scopes)
                    return rows

            owner = _WarehouseReadOwner()
            rows = await _await_worker(
                owner=owner,
                operation=read_page,
                deadline=deadline,
                scopes=scopes,
            )
            validate_scope_grants(scopes)
            serialized = [_serialize_contract_row(row, contract_type) for row in rows]
            validate_scope_grants(scopes)
            return {
                "source": source,
                "model": model,
                "domain": domain,
                "verified": verified,
                "read_at": datetime.now(timezone.utc).isoformat(),
                "completeness": "NOT_ASSESSED",
                "results": serialized,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(serialized),
                    "total": None,
                },
                "snapshot_scope": "single_read_transaction",
            }
    except (ProviderModelWarehouseIdentityError, ProviderModelWarehouseNotFoundError):
        raise
    except ProviderModelWarehouseForbiddenError:
        raise
    except ProviderModelWarehouseAuthorizationError:
        raise
    except ProviderModelWarehouseValidationError:
        raise
    except ProviderModelWarehouseConfigurationError:
        raise
    except ProviderModelWarehouseTimeoutError:
        raise
    except ProviderModelWarehouseCancelledError:
        raise
    except asyncio.CancelledError:
        raise
    except AsyncExecutionTimeoutError:
        raise ProviderModelWarehouseTimeoutError from None
    except RequestExecutionDeadlineError:
        raise ProviderModelWarehouseTimeoutError from None
    except RequestExecutionCancelledError:
        raise ProviderModelWarehouseCancelledError from None
    except (RequestAuthorizationError, RequestBudgetScopeError):
        raise ProviderModelWarehouseAuthorizationError from None
    except RequestBudgetError:
        raise ProviderModelWarehouseAuthorizationError from None
    except ProviderModelWarehouseUnavailableError:
        raise
    except Exception:
        raise ProviderModelWarehouseUnavailableError from None
    except BaseException:
        raise


__all__ = [
    "ProviderModelWarehouseAuthorizationError",
    "ProviderModelWarehouseCancelledError",
    "ProviderModelWarehouseConfigurationError",
    "ProviderModelWarehouseForbiddenError",
    "ProviderModelWarehouseIdentityError",
    "ProviderModelWarehouseNotFoundError",
    "ProviderModelWarehouseTimeoutError",
    "ProviderModelWarehouseUnavailableError",
    "ProviderModelWarehouseValidationError",
    "query_provider_model_warehouse",
]
