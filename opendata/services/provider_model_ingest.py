"""Governed capture-and-store orchestration for native provider models."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from opendata.data import async_execution, domains
from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.protocol import CapturedFetch, FetchContext, Fetcher, QueryParams
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
from opendata.pipeline import provider_model_codec, provider_model_raw
from opendata.pipeline.provider_model_raw import NativeRawCapture
from opendata.services import provider_model_query, provider_model_store
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
_MAX_INGEST_ROWS = 10_000
_POLL_INTERVAL_SECONDS = 0.01
_T = TypeVar("_T")


class _DomainPrincipal(Protocol):
    """Authorization interface for local domain ingestion."""

    def allows_domain(self, domain: str) -> bool: ...


class ProviderModelIngestIdentityError(ValueError):
    """The provider/model path is malformed or conflicts with the query."""


class ProviderModelIngestNotFoundError(LookupError):
    """The exact model is unsupported or absent from the supplied registry."""


class ProviderModelIngestForbiddenError(PermissionError):
    """The principal is not allowed to ingest this domain."""


class ProviderModelIngestAuthorizationError(PermissionError):
    """No exact, current STORE grant authorizes this ingestion."""


class ProviderModelIngestValidationError(ValueError):
    """The request or normalized provider output fails its strict contract."""


class ProviderModelIngestConfigurationError(RuntimeError):
    """The registry, domain schema, or ingest integration is not ready."""


class ProviderModelIngestTimeoutError(TimeoutError):
    """Ingestion exceeded the deadline measured from handler admission."""


class ProviderModelIngestCancelledError(RuntimeError):
    """Ingestion was cancelled by its trusted context or enclosing scope."""


class ProviderModelIngestUnavailableError(RuntimeError):
    """Source capture or the single ODS/DWD write failed safely."""


class _IngestWriterOwner:
    """A per-call weak-referenceable owner for the shared bounded executor."""


class _CombinedCancellation:
    """A worker cancellation view over local, caller, and inherited events."""

    def __init__(self, *events: Event | None) -> None:
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


async def await_ingest_request_content(
    read_content: Callable[[], Awaitable[_T]],
    *,
    ctx: FetchContext | None,
    admission_started: float,
) -> _T:
    """Bound the ASGI body read by the same trusted STORE admission deadline."""
    context = _trusted_store_context(ctx)
    inherited = current_request_scopes()
    deadline = _context_deadline(context, admission_started, inherited)

    def check_liveness() -> None:
        check_scope_liveness(inherited)
        if context._thread_cancel_event is not None and context._thread_cancel_event.is_set():
            raise RequestExecutionCancelledError("ingest request was cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise RequestExecutionDeadlineError("ingest request deadline expired")

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
                raise RequestExecutionDeadlineError("ingest request deadline expired")
            interval = (
                _POLL_INTERVAL_SECONDS
                if remaining is None
                else min(_POLL_INTERVAL_SECONDS, remaining)
            )
            await asyncio.wait((task,), timeout=interval)
        check_liveness()
        return task.result()
    except RequestExecutionCancelledError:
        await cancel_and_drain()
        raise ProviderModelIngestCancelledError from None
    except RequestExecutionDeadlineError:
        await cancel_and_drain()
        raise ProviderModelIngestTimeoutError from None
    except asyncio.CancelledError:
        await cancel_and_drain()
        raise
    except BaseException:
        await cancel_and_drain()
        raise


def _trusted_store_context(ctx: FetchContext | None) -> FetchContext:
    """Default to a finite STORE context and never promote another operation."""
    if ctx is None:
        from opendata.data.request_budget import RequestBudget

        return FetchContext(
            timeout=_DEFAULT_TIMEOUT_SECONDS,
            request_budget=RequestBudget(),
            operation=RequestOperation.STORE,
        )
    if (
        not isinstance(ctx, FetchContext)
        or ctx.operation is not RequestOperation.STORE
        or ctx.timeout is None
        or isinstance(ctx.timeout, bool)
        or not isinstance(ctx.timeout, (int, float))
        or not math.isfinite(float(ctx.timeout))
        or ctx.timeout <= 0
    ):
        raise ProviderModelIngestValidationError from None
    return ctx


def _context_deadline(
    context: FetchContext,
    admission_started: float,
    inherited: tuple[RequestExecutionScope, ...],
) -> float | None:
    try:
        return provider_model_store._context_deadline(context, admission_started, inherited)
    except RequestExecutionDeadlineError:
        raise
    except Exception:
        raise ProviderModelIngestValidationError from None


def _resolve_ingest_model(
    *,
    registry: ProviderRegistry,
    source: str,
    model: str,
    principal: _DomainPrincipal,
) -> tuple[str, bool, type[ContractModel], Fetcher[Any, Any], Any]:
    """Require an exact registered Fetcher and reviewed store-enabled domain."""
    try:
        validate_model_identity(source, model)
    except ProviderModelIdentityError:
        raise ProviderModelIngestIdentityError from None
    except Exception:
        raise ProviderModelIngestIdentityError from None

    expected = provider_model_store._MODEL_DOMAINS.get((source, model))
    if expected is None:
        raise ProviderModelIngestNotFoundError from None
    expected_domain, expected_identity = expected

    try:
        descriptor = find_provider_model(registry, source, model)
        fetcher = registry.resolve_model(source, model)
    except ProviderModelNotFoundError:
        raise ProviderModelIngestNotFoundError from None
    except Exception:
        raise ProviderModelIngestConfigurationError from None

    capability = getattr(fetcher, "capability", None)
    capability_identity = tuple(
        getattr(capability, name, None)
        for name in ("asset_class", "domain", "period", "market", "source")
    )
    capability_verified = getattr(capability, "verified", None)
    if (
        not isinstance(fetcher, Fetcher)
        or descriptor.source != source
        or descriptor.model != model
        or descriptor.domain != expected_domain
        or descriptor.full_capability_identity != expected_identity
        or getattr(fetcher, "canonical_model", None) != model
        or capability_identity != expected_identity
        or type(descriptor.verified) is not bool
        or type(capability_verified) is not bool
        or capability_verified != descriptor.verified
        or getattr(fetcher, "async_mode", None) != "bounded_thread"
        or not callable(getattr(fetcher, "fetch_captured_async", None))
    ):
        raise ProviderModelIngestConfigurationError from None

    try:
        spec = domains.require_domain_semantics(expected_domain)
        contract_type = domains.contract_model(expected_domain)
        provider_model_store._expected_columns(expected_domain)
        provider_model_codec.physical_model_key(expected_domain)
        if (
            spec.contract != contract_type.__name__
            or spec.storage_mode != "upsert"
            or "store" not in spec.permissions
            or not spec.natural_key
        ):
            raise ValueError
    except Exception:
        raise ProviderModelIngestConfigurationError from None

    try:
        permitted = principal.allows_domain(expected_domain)
    except Exception:
        raise ProviderModelIngestConfigurationError from None
    if not permitted:
        raise ProviderModelIngestForbiddenError from None
    return expected_domain, descriptor.verified, contract_type, fetcher, spec


def _prepare_fetch_query(
    *,
    query: Mapping[str, object],
    source: str,
    model: str,
    market: str,
) -> dict[str, object]:
    """Normalize routing identity, enforce ceilings, and pure-validate the query."""
    try:
        normalized: dict[str, object] = provider_model_query._prepare_query(
            query,
            source=source,
            market=market,
        )
    except provider_model_query.ProviderModelQueryIdentityError:
        raise ProviderModelIngestIdentityError from None
    except provider_model_query.ProviderModelQueryValidationError:
        raise ProviderModelIngestValidationError from None
    except Exception:
        raise ProviderModelIngestValidationError from None

    if source in {"fred", "fmp"}:
        if "max_records" in normalized:
            maximum = normalized["max_records"]
            if type(maximum) is not int or maximum < 1 or maximum > _MAX_INGEST_ROWS:
                raise ProviderModelIngestValidationError from None
        else:
            normalized["max_records"] = _MAX_INGEST_ROWS
        if source == "fred" and "page_size" not in normalized:
            normalized["page_size"] = _MAX_INGEST_ROWS

    query_model = _reviewed_query_model(source, model)
    try:
        params = query_model.model_validate(normalized)
        values = params.model_dump(mode="python", by_alias=False)
        if not isinstance(values, dict) or any(type(key) is not str for key in values):
            raise TypeError
    except ValidationError:
        raise ProviderModelIngestValidationError from None
    except ProviderModelIngestValidationError:
        raise
    except Exception:
        raise ProviderModelIngestValidationError from None
    return cast("dict[str, object]", values)


def _reviewed_query_model(source: str, model: str) -> type[BaseModel]:
    """Return the exact Pydantic query type for the three persistent pairs."""
    from opendata.data.providers.bls.models.series import BlsSeriesQuery
    from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
    from opendata.data.providers.fred.models.series import FredSeriesQuery

    query_models: dict[tuple[str, str], type[BaseModel]] = {
        ("fred", "FredSeries"): FredSeriesQuery,
        ("bls", "BlsSeries"): BlsSeriesQuery,
        ("fmp", "EquityHistorical"): EquityHistoricalQuery,
    }
    query_model = query_models.get((source, model))
    if query_model is None:
        raise ProviderModelIngestNotFoundError from None
    return query_model


def _validate_contract_rows(
    value: object,
    contract_type: type[ContractModel],
) -> tuple[ContractModel, ...]:
    """Require a bounded sequence of exact, strictly revalidated contract rows."""
    if (
        isinstance(value, (str, bytes, bytearray, Mapping))
        or not isinstance(value, Sequence)
        or isinstance(value, BaseModel)
        or len(value) > _MAX_INGEST_ROWS
    ):
        raise ProviderModelIngestUnavailableError from None

    validated_rows: list[ContractModel] = []
    declared_fields = set(contract_type.model_fields)
    try:
        for row in value:
            if type(row) is not contract_type:
                raise ValueError
            row_state = vars(row)
            if set(row_state) - declared_fields or getattr(row, "__pydantic_extra__", None):
                raise ValueError
            raw_data = row.model_dump(mode="python", by_alias=False)
            if not isinstance(raw_data, dict) or set(raw_data) - declared_fields:
                raise ValueError
            validated = contract_type.model_validate(raw_data, strict=True)
            if type(validated) is not contract_type:
                raise ValueError
            validated_rows.append(validated)
    except Exception:
        raise ProviderModelIngestUnavailableError from None
    return tuple(validated_rows)


async def _await_writer_worker(
    *,
    owner: _IngestWriterOwner,
    operation: Callable[[Event, float | None], _T],
    deadline: float,
    scopes: tuple[RequestExecutionScope, ...],
) -> _T:
    """Run SQL in the shared bounded pool and keep its slot until it exits."""
    task = asyncio.create_task(async_execution.run_bounded(owner, operation, deadline))

    async def cancel_waiter() -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        while not task.done():
            try:
                check_scope_liveness(scopes)
            except (RequestExecutionCancelledError, RequestExecutionDeadlineError):
                await cancel_waiter()
                raise
            remaining = max(0.0, deadline - time.monotonic())
            if remaining == 0:
                await cancel_waiter()
                raise RequestExecutionDeadlineError("ingest deadline expired")
            await asyncio.wait((task,), timeout=min(_POLL_INTERVAL_SECONDS, remaining))
        return await task
    except asyncio.CancelledError:
        if not task.done():
            await cancel_waiter()
        raise


async def ingest_provider_model(
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
    """Capture once through the registered Fetcher, then commit ODS and DWD."""
    admission_started = time.monotonic() if _admission_started is None else _admission_started
    if (
        isinstance(admission_started, bool)
        or not isinstance(admission_started, (int, float))
        or not math.isfinite(float(admission_started))
    ):
        raise ProviderModelIngestValidationError from None
    if not callable(engine_factory):
        raise ProviderModelIngestConfigurationError from None

    try:
        validate_model_identity(source, model)
    except Exception:
        raise ProviderModelIngestIdentityError from None
    if (source, model) not in provider_model_store._MODEL_DOMAINS:
        raise ProviderModelIngestNotFoundError from None

    context = _trusted_store_context(ctx)
    inherited = current_request_scopes()
    try:
        deadline = _context_deadline(context, float(admission_started), inherited)
    except RequestExecutionDeadlineError:
        raise ProviderModelIngestTimeoutError from None
    if deadline is None:
        raise ProviderModelIngestConfigurationError from None

    try:
        with request_execution_scope(
            source=source,
            canonical_model=model,
            operation=RequestOperation.STORE,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
            inherited_scopes=inherited,
        ):
            scopes = current_request_scopes()
            check_scope_liveness(scopes)
            domain, verified, contract_type, fetcher, _spec = _resolve_ingest_model(
                registry=registry,
                source=source,
                model=model,
                principal=principal,
            )
            fetch_kwargs = _prepare_fetch_query(
                query=query,
                source=source,
                model=model,
                market=fetcher.capability.market,
            )

            validate_scope_grants(scopes)
            fetch_context = replace(context, _deadline_monotonic=deadline)

            def make_capture(raw: object, params: object) -> NativeRawCapture:
                return provider_model_raw.make_provider_model_capture(
                    source=source,
                    model=model,
                    raw=cast("list[dict[str, object]]", raw),
                    params=cast("QueryParams", params),
                )

            try:
                captured = await fetcher.fetch_captured_async(
                    capture=make_capture,
                    ctx=fetch_context,
                    **fetch_kwargs,
                )
            except asyncio.CancelledError:
                raise
            except (AsyncExecutionTimeoutError, RequestExecutionDeadlineError, TimeoutError):
                raise ProviderModelIngestTimeoutError from None
            except RequestExecutionCancelledError:
                raise ProviderModelIngestCancelledError from None
            except (RequestAuthorizationError, RequestBudgetScopeError):
                raise ProviderModelIngestAuthorizationError from None
            except RequestBudgetError:
                raise ProviderModelIngestAuthorizationError from None
            except ValidationError:
                raise ProviderModelIngestValidationError from None
            except Exception:
                raise ProviderModelIngestUnavailableError from None

            check_scope_liveness(scopes)
            validate_scope_grants(scopes)
            if type(captured) is not CapturedFetch:
                raise ProviderModelIngestUnavailableError from None
            try:
                capture = provider_model_raw.validate_provider_model_capture(
                    source=source,
                    model=model,
                    capture=captured.capture,
                )
            except Exception:
                raise ProviderModelIngestUnavailableError from None
            if type(capture) is not NativeRawCapture:
                raise ProviderModelIngestUnavailableError from None
            if (
                capture.source != source
                or capture.model != model
                or capture.raw_scope != "extract_data_output"
                or type(capture.raw_row_count) is not int
                or capture.raw_row_count < 0
            ):
                raise ProviderModelIngestUnavailableError from None
            rows = _validate_contract_rows(captured.results, contract_type)
            check_scope_liveness(scopes)
            validate_scope_grants(scopes)

            # This records local capture/validation time. It is not a source
            # event time, publication time, or point-in-time observation.
            observed_at = datetime.now(timezone.utc)

            batch_id = str(uuid.uuid4())
            if str(uuid.UUID(batch_id, version=4)) != batch_id:
                raise ProviderModelIngestConfigurationError from None

            def write_batch(
                worker_cancel: Event,
                worker_deadline: float | None,
            ) -> int:
                cancellation = _CombinedCancellation(
                    worker_cancel,
                    context._thread_cancel_event,
                    *(scope.cancellation for scope in scopes),
                )
                worker_context = replace(
                    fetch_context,
                    _deadline_monotonic=worker_deadline,
                    _thread_cancel_event=cast("Event", cancellation),
                )
                with request_execution_scope(
                    source=source,
                    canonical_model=model,
                    operation=RequestOperation.STORE,
                    budget=context.request_budget,
                    deadline=worker_deadline,
                    cancellation=cast("Event", cancellation),
                    inherited_scopes=scopes,
                ):
                    worker_scopes = current_request_scopes()
                    validate_scope_grants(worker_scopes)
                    engine = engine_factory()
                    validate_scope_grants(worker_scopes)
                    stored_rows = provider_model_store.write_provider_model_batch(
                        engine=engine,
                        registry=registry,
                        source=source,
                        model=model,
                        capture=capture,
                        rows=rows,
                        observed_at=observed_at,
                        batch_id=batch_id,
                        ctx=worker_context,
                    )
                    validate_scope_grants(worker_scopes)
                    if type(stored_rows) is not int or stored_rows != len(rows):
                        raise ProviderModelIngestUnavailableError
                    return stored_rows

            try:
                stored_rows = await _await_writer_worker(
                    owner=_IngestWriterOwner(),
                    operation=write_batch,
                    deadline=deadline,
                    scopes=scopes,
                )
            except asyncio.CancelledError:
                raise
            except AsyncExecutionTimeoutError:
                raise ProviderModelIngestTimeoutError from None
            except RequestExecutionDeadlineError:
                raise ProviderModelIngestTimeoutError from None
            except RequestExecutionCancelledError:
                raise ProviderModelIngestCancelledError from None
            except (RequestAuthorizationError, RequestBudgetScopeError, RequestBudgetError):
                raise ProviderModelIngestAuthorizationError from None
            except ProviderModelIngestUnavailableError:
                raise
            except Exception:
                raise ProviderModelIngestUnavailableError from None

            check_scope_liveness(scopes)
            validate_scope_grants(scopes)
            return {
                "source": source,
                "model": model,
                "domain": domain,
                "verified": verified,
                "batch_id": batch_id,
                "observed_at": observed_at.isoformat(),
                "raw_rows": capture.raw_row_count,
                "stored_rows": stored_rows,
                "raw_scope": capture.raw_scope,
                "completeness": "NOT_ASSESSED",
                "transaction_scope": "ods_and_dwd_single_transaction",
            }
    except (
        ProviderModelIngestIdentityError,
        ProviderModelIngestNotFoundError,
        ProviderModelIngestForbiddenError,
        ProviderModelIngestAuthorizationError,
        ProviderModelIngestValidationError,
        ProviderModelIngestConfigurationError,
        ProviderModelIngestTimeoutError,
        ProviderModelIngestCancelledError,
        ProviderModelIngestUnavailableError,
    ):
        raise
    except RequestExecutionDeadlineError:
        raise ProviderModelIngestTimeoutError from None
    except RequestExecutionCancelledError:
        raise ProviderModelIngestCancelledError from None
    except (RequestAuthorizationError, RequestBudgetScopeError):
        raise ProviderModelIngestAuthorizationError from None
    except RequestBudgetError:
        raise ProviderModelIngestAuthorizationError from None
    except ProviderModelIdentityError:
        raise ProviderModelIngestIdentityError from None
    except ProviderModelNotFoundError:
        raise ProviderModelIngestNotFoundError from None
    except Exception:
        raise ProviderModelIngestUnavailableError from None
    except BaseException:
        raise


__all__ = [
    "ProviderModelIngestAuthorizationError",
    "ProviderModelIngestCancelledError",
    "ProviderModelIngestConfigurationError",
    "ProviderModelIngestForbiddenError",
    "ProviderModelIngestIdentityError",
    "ProviderModelIngestNotFoundError",
    "ProviderModelIngestTimeoutError",
    "ProviderModelIngestUnavailableError",
    "ProviderModelIngestValidationError",
    "await_ingest_request_content",
    "ingest_provider_model",
]
