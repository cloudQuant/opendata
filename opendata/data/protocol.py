"""Three-stage data source protocol (design §4.2, provider template §7.1).

``QueryParams`` -> ``Fetcher`` -> standardized output. The stage names
follow the concrete template of design §7.1 and map onto the §4.2
pipeline semantics::

    transform_query  ==  validate   (build + validate query params)
    extract_data     ==  fetch_raw  (transport, source-native shape)
    transform_data   ==  normalize   (the single owner of field
                                      mapping, unit conversion and
                                      key normalization, §4.2)
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING, ClassVar, Generic, TypeAlias, TypeVar, cast

from pydantic import BaseModel, ConfigDict

from opendata.data.async_execution import (
    AsyncExecutionTimeoutError,
    acquire_instance,
    acquire_instance_sync,
    await_native_extraction,
    check_worker_liveness,
    run_bounded,
)
from opendata.data.request_budget import (
    RequestBudget,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
    capture_request_scopes,
    check_scope_liveness,
    request_execution_scope,
    validate_scope_grants,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from threading import Event

    import pandas as pd

    from opendata.data.capability import Capability
    from opendata.data.models import ContractModel
    from opendata.data.request_budget import RequestExecutionScope

#: Query parameter type; concrete fetchers narrow it (bound: QueryParams).
QueryT = TypeVar("QueryT", bound="QueryParams")
#: Raw extraction type; an implementation detail of each fetcher.
RawT = TypeVar("RawT")
CaptureT = TypeVar("CaptureT")
_AwaitT = TypeVar("_AwaitT")
# Quoted: pandas is untyped, so the unquoted form would not be a valid
# type alias for mypy (Sequence[ContractModel] | Any collapses to Any).
FetchResult: TypeAlias = "Sequence[ContractModel] | pd.DataFrame"


def _fetcher_source(fetcher: object) -> str:
    """Read the stable routing source from a Fetcher's declared capability."""
    capability = getattr(fetcher, "capability", None)
    source = getattr(capability, "source", None)
    return source if isinstance(source, str) else "unspecified"


class _CombinedCancellation:
    """Read several cancellation signals as one without losing their owners."""

    def __init__(
        self,
        *signals: Event | _CombinedCancellation | None,
    ) -> None:
        self._signals = tuple(signal for signal in signals if signal is not None)
        self._local = threading.Event()

    def is_set(self) -> bool:
        """Return whether this call or any enclosing caller was cancelled."""
        return self._local.is_set() or any(signal.is_set() for signal in self._signals)

    def set(self) -> None:
        """Mark the local execution signal without mutating caller signals."""
        self._local.set()


def _effective_fetch_deadline(
    context: FetchContext,
    admission_started: float,
    inherited_scopes: tuple[RequestExecutionScope, ...],
) -> float | None:
    """Combine this call's timeout and every inherited monotonic deadline."""
    deadlines: list[float] = []
    for scope in inherited_scopes:
        deadline = scope.deadline
        if deadline is None:
            continue
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not _is_finite_number(deadline)
        ):
            raise RequestExecutionDeadlineError("request execution deadline is invalid")
        deadlines.append(float(deadline))

    context_deadline = context._deadline_monotonic
    if context_deadline is not None and (
        isinstance(context_deadline, bool)
        or not isinstance(context_deadline, (int, float))
        or not _is_finite_number(context_deadline)
    ):
        raise RequestExecutionDeadlineError("FetchContext deadline must be finite monotonic time")
    if context_deadline is not None:
        deadlines.append(float(context_deadline))

    if context.timeout is not None:
        timeout_deadline = admission_started + float(context.timeout)
        if not _is_finite_number(timeout_deadline):
            raise RequestExecutionDeadlineError("FetchContext timeout deadline is invalid")
        deadlines.append(timeout_deadline)
    return min(deadlines) if deadlines else None


def _is_finite_number(value: object) -> bool:
    """Return whether a numeric deadline converts to a finite float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


async def _await_with_cancellation(
    awaitable: Awaitable[_AwaitT],
    cancellation: _CombinedCancellation,
    *,
    on_cancelled_result: Callable[[_AwaitT], None] | None = None,
) -> _AwaitT:
    """Poll an async admission step so an external Event can cancel its wait."""
    task = asyncio.ensure_future(awaitable)

    async def cancel_and_dispose_result() -> None:
        task.cancel()
        try:
            result = await task
        except BaseException:
            return
        if on_cancelled_result is not None:
            on_cancelled_result(result)

    try:
        while not task.done():
            if cancellation.is_set():
                await cancel_and_dispose_result()
                raise RequestExecutionCancelledError("request execution was cancelled")
            await asyncio.wait((task,), timeout=0.01)
        result = await task
        if cancellation.is_set():
            if on_cancelled_result is not None:
                on_cancelled_result(result)
            raise RequestExecutionCancelledError("request execution was cancelled")
        return result
    except asyncio.CancelledError:
        await cancel_and_dispose_result()
        raise


class QueryParams(BaseModel):
    """Common query parameters shared by every domain (design §4.2).

    Domain-specific queries subclass this and add their own fields
    (and may narrow ``symbol`` to a required field, as in the §7.1
    template). Unknown fields are rejected so typos fail closed.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: str = "auto"
    market: str | None = None
    symbol: str | None = None
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True)
class FetchContext:
    """Per-call execution context passed to the extraction stage.

    ``timeout`` bounds an async fetch from admission through normalization.
    The private fields carry the resulting deadline and cancellation signal
    into a synchronous provider call running in the bounded thread pool.
    """

    timeout: float | None = None
    _deadline_monotonic: float | None = field(default=None, repr=False, compare=False)
    _thread_cancel_event: Event | None = field(default=None, repr=False, compare=False)
    request_budget: RequestBudget | None = None
    operation: RequestOperation = RequestOperation.QUERY

    def __post_init__(self) -> None:
        """Reject unusable deadline values at context construction."""
        if self.timeout is not None:
            try:
                valid_timeout = (
                    not isinstance(self.timeout, bool)
                    and isinstance(self.timeout, (int, float))
                    and math.isfinite(float(self.timeout))
                    and self.timeout > 0
                )
            except (OverflowError, ValueError):
                valid_timeout = False
            if not valid_timeout:
                raise ValueError("timeout must be a finite positive number")
        if self.request_budget is not None and not isinstance(self.request_budget, RequestBudget):
            raise ValueError("request_budget must be a RequestBudget")
        try:
            operation = RequestOperation(self.operation)
        except (TypeError, ValueError) as exc:
            raise ValueError("operation must be query, store, export, or subscribe") from exc
        object.__setattr__(self, "operation", operation)


class UnsupportedAsyncFetcherError(RuntimeError):
    """Raised when a fetcher explicitly disables the async entry point."""


class UnknownAsyncFetcherModeError(RuntimeError):
    """Raised when a fetcher declares an unrecognized async execution mode."""


class NativeAsyncExtractionNotImplementedError(RuntimeError):
    """Raised when native async mode has no extraction implementation."""


@dataclass(frozen=True)
class CapturedFetch(Generic[CaptureT]):
    """A trusted raw-source snapshot paired with its normalized result.

    ``capture`` is produced by a trusted in-process callback before provider
    normalization can mutate the raw result or validated query parameters.
    """

    capture: CaptureT
    results: FetchResult


class Fetcher(ABC, Generic[QueryT, RawT]):
    """Base class for every data source fetcher.

    Concrete fetchers declare their :class:`~opendata.data.capability.Capability`
    as the ``capability`` class attribute (design §7.1) and implement
    the three stages. ``fetch`` is the template method wiring the
    stages together; callers (service layer, pipeline) only use
    ``fetch``.
    """

    capability: ClassVar[Capability]
    async_mode: ClassVar[str] = "unsupported"
    canonical_model: ClassVar[str | None] = None

    def fetch(self, *, ctx: FetchContext | None = None, **kwargs: object) -> FetchResult:
        """Run the validate -> extract -> normalize pipeline.

        Args:
            ctx: Optional per-call execution context (timeouts, later
                the shared HTTP session).
            **kwargs: Domain-specific query fields, forwarded to
                ``transform_query`` and validated there.

        Returns:
            Standardized contract models or a DataFrame.

        Raises:
            pydantic.ValidationError: If the query parameters are
                invalid or unknown (fail closed in the first stage).
        """
        context = ctx if ctx is not None else FetchContext()
        deadline = context._deadline_monotonic
        if deadline is None and context.timeout is not None:
            deadline = time.monotonic() + context.timeout
        instance_lock = acquire_instance_sync(self)
        try:
            with request_execution_scope(
                source=_fetcher_source(self),
                canonical_model=self.canonical_model,
                operation=context.operation,
                budget=context.request_budget,
                deadline=deadline,
                cancellation=context._thread_cancel_event,
            ):
                params = self.transform_query(**kwargs)
                validate_scope_grants(capture_request_scopes())
                raw = self.extract_data(params, context)
                return self.transform_data(raw, params)
        finally:
            instance_lock.release()

    async def fetch_async(
        self,
        *,
        ctx: FetchContext | None = None,
        **kwargs: object,
    ) -> FetchResult:
        """Run validate -> extract -> normalize through a bounded async entry.

        Synchronous fetchers run the complete pipeline in the shared bounded
        pool. Native async fetchers override :meth:`extract_data_async`; their
        validation and normalization stages remain the same synchronous
        methods used by :meth:`fetch`.
        """
        return cast(
            "FetchResult",
            await self._fetch_async(normalize=True, ctx=ctx, query_kwargs=kwargs),
        )

    async def fetch_raw_async(
        self,
        *,
        ctx: FetchContext | None = None,
        **kwargs: object,
    ) -> RawT:
        """Run validate -> extract without provider normalization.

        This is the raw-source seam for consumers such as the ODS pipeline,
        whose storage contract intentionally retains source-native columns
        and units. It shares admission, deadlines, cancellation, and native
        extraction with :meth:`fetch_async`.
        """
        return cast(
            "RawT",
            await self._fetch_async(normalize=False, ctx=ctx, query_kwargs=kwargs),
        )

    async def fetch_captured_async(
        self,
        *,
        capture: Callable[[RawT, QueryT], CaptureT],
        ctx: FetchContext | None = None,
        **kwargs: object,
    ) -> CapturedFetch[CaptureT]:
        """Capture trusted raw/query state before normalization in one fetch.

        The callback is an internal pure-snapshot seam, not request input. It
        runs once after extraction and before the shared normalizer while the
        same instance gate, deadline, cancellation, and request scope remain
        active. The capture can therefore preserve source-native provenance
        even if normalization mutates its arguments.

        Args:
            capture: Trusted in-process callback that snapshots extracted raw
                data and the validated query. It must not perform I/O.
            ctx: Optional per-call execution context.
            **kwargs: Domain-specific query fields forwarded to validation.

        Returns:
            The callback's snapshot together with the normalized result.

        Raises:
            TypeError: If ``capture`` is not callable.
            UnsupportedAsyncFetcherError: If this Fetcher disables async use.
            UnknownAsyncFetcherModeError: If this Fetcher declares an unknown
                async execution mode.
            NativeAsyncExtractionNotImplementedError: If native async mode
                lacks its extraction implementation.
        """
        if not callable(capture):
            raise TypeError("capture must be callable")
        return cast(
            "CapturedFetch[CaptureT]",
            await self._fetch_async(
                normalize=True,
                ctx=ctx,
                capture_callback=capture,
                query_kwargs=kwargs,
            ),
        )

    async def _fetch_async(
        self,
        *,
        normalize: bool,
        ctx: FetchContext | None,
        query_kwargs: dict[str, object],
        capture_callback: Callable[[RawT, QueryT], CaptureT] | None = None,
    ) -> object:
        """Shared async execution path for normalized, raw, and captured entries."""
        mode = self.async_mode
        if mode == "unsupported":
            raise UnsupportedAsyncFetcherError(
                f"{type(self).__name__} does not support asynchronous fetching"
            )
        if mode not in ("native_async", "bounded_thread"):
            raise UnknownAsyncFetcherModeError(
                f"{type(self).__name__} declares unknown async_mode {mode!r}"
            )
        if mode == "native_async" and type(self).extract_data_async is Fetcher.extract_data_async:
            raise NativeAsyncExtractionNotImplementedError(
                f"{type(self).__name__} declares native_async but does not implement "
                "extract_data_async"
            )

        context = ctx if ctx is not None else FetchContext()
        admission_started = time.monotonic()
        inherited_scopes = capture_request_scopes()
        deadline = _effective_fetch_deadline(context, admission_started, inherited_scopes)
        check_scope_liveness(inherited_scopes)
        inherited_cancellations = tuple(scope.cancellation for scope in inherited_scopes)
        caller_cancellation = _CombinedCancellation(
            context._thread_cancel_event,
            *inherited_cancellations,
        )
        if caller_cancellation.is_set():
            raise RequestExecutionCancelledError("request execution was cancelled")
        check_worker_liveness(cast("Event", caller_cancellation), deadline)

        if mode == "bounded_thread":

            def operation(
                cancellation: Event,
                worker_deadline: float | None,
            ) -> object:
                operation_cancellation = _CombinedCancellation(
                    caller_cancellation,
                    cancellation,
                )
                worker_context = replace(
                    context,
                    _deadline_monotonic=worker_deadline,
                    _thread_cancel_event=cast("Event", operation_cancellation),
                )
                with request_execution_scope(
                    source=_fetcher_source(self),
                    canonical_model=self.canonical_model,
                    operation=worker_context.operation,
                    budget=worker_context.request_budget,
                    deadline=worker_deadline,
                    cancellation=cast("Event", operation_cancellation),
                    inherited_scopes=inherited_scopes,
                ):
                    scopes = capture_request_scopes()

                    def check_live(*, validate_grant: bool = False) -> None:
                        if caller_cancellation.is_set():
                            raise RequestExecutionCancelledError("request execution was cancelled")
                        check_worker_liveness(
                            cast("Event", operation_cancellation),
                            worker_deadline,
                        )
                        check_scope_liveness(scopes)
                        if validate_grant:
                            validate_scope_grants(scopes)

                    check_live()
                    params = self.transform_query(**query_kwargs)
                    check_live(validate_grant=True)
                    raw = self.extract_data(params, worker_context)
                    check_live(validate_grant=True)
                    captured = (
                        capture_callback(raw, params) if capture_callback is not None else None
                    )
                    if capture_callback is not None:
                        check_live(validate_grant=True)
                    if not normalize:
                        return raw
                    result = self.transform_data(raw, params)
                    check_live(validate_grant=True)
                    if capture_callback is not None:
                        return CapturedFetch(captured, result)
                    return result

            async def run_bounded_call() -> object:
                return await run_bounded(self, operation, deadline)

            return await _await_with_cancellation(run_bounded_call(), caller_cancellation)

        instance_lock = await _await_with_cancellation(
            acquire_instance(self, deadline),
            caller_cancellation,
            on_cancelled_result=lambda lock: lock.release(),
        )
        lock_deferred = False
        native_cancellation = _CombinedCancellation(caller_cancellation)

        def defer_instance_release() -> None:
            nonlocal lock_deferred
            lock_deferred = True
            native_cancellation.set()

        try:
            native_context = replace(
                context,
                _deadline_monotonic=deadline,
                _thread_cancel_event=cast("Event", native_cancellation),
            )
            with request_execution_scope(
                source=_fetcher_source(self),
                canonical_model=self.canonical_model,
                operation=native_context.operation,
                budget=native_context.request_budget,
                deadline=deadline,
                cancellation=cast("Event", native_cancellation),
                inherited_scopes=inherited_scopes,
            ):
                scopes = capture_request_scopes()

                def check_live(*, validate_grant: bool = False) -> None:
                    if caller_cancellation.is_set():
                        raise RequestExecutionCancelledError("request execution was cancelled")
                    check_worker_liveness(cast("Event", native_cancellation), deadline)
                    check_scope_liveness(scopes)
                    if validate_grant:
                        validate_scope_grants(scopes)

                check_live()
                params = self.transform_query(**query_kwargs)
                check_live(validate_grant=True)
                raw = await _await_with_cancellation(
                    await_native_extraction(
                        self.extract_data_async(params, native_context),
                        deadline,
                        on_abandon=defer_instance_release,
                        on_terminal=instance_lock.release,
                    ),
                    caller_cancellation,
                )
                if deadline is not None and time.monotonic() >= deadline:
                    phase = "before normalization" if normalize else "after async extraction"
                    raise AsyncExecutionTimeoutError(f"fetch deadline expired {phase}")
                check_live(validate_grant=True)
                captured = capture_callback(raw, params) if capture_callback is not None else None
                if capture_callback is not None:
                    # A synchronous snapshot callback may have run long enough
                    # for cancellation to be scheduled on this event loop.
                    await asyncio.sleep(0)
                    check_live(validate_grant=True)
                if not normalize:
                    return raw
                result = self.transform_data(raw, params)
                if capture_callback is not None:
                    await asyncio.sleep(0)
                check_live(validate_grant=True)
                if capture_callback is not None:
                    return CapturedFetch(captured, result)
                return result
        finally:
            if not lock_deferred:
                instance_lock.release()

    async def extract_data_async(self, params: QueryT, ctx: FetchContext) -> RawT:
        """Fetch raw data asynchronously for models declaring ``native_async``.

        The default is intentionally unsupported so a class cannot claim the
        native mode without providing an implementation.
        """
        raise NativeAsyncExtractionNotImplementedError(
            f"{type(self).__name__} has no native async extraction implementation"
        )

    @abstractmethod
    def transform_query(self, **kwargs: object) -> QueryT:
        """Build and validate the domain query (the validate stage).

        Args:
            **kwargs: Domain-specific query fields.

        Returns:
            The validated query params instance.
        """

    @abstractmethod
    def extract_data(self, params: QueryT, ctx: FetchContext) -> RawT:
        """Fetch raw source data (the fetch_raw stage).

        Args:
            params: Validated query parameters.
            ctx: Per-call execution context.

        Returns:
            Source-native raw data, opaque to the protocol.
        """

    @abstractmethod
    def transform_data(self, raw: RawT, params: QueryT) -> FetchResult:
        """Normalize raw data into contract models (the normalize stage).

        This is the single owner of field mapping, unit conversion
        (手 -> 股), adjust semantics and key normalization
        (``600519`` <-> ``600519.SH``), design §4.2.

        Args:
            raw: Output of ``extract_data``.
            params: The validated query parameters.

        Returns:
            Standardized contract models or a DataFrame.
        """
