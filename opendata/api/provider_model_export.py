"""Strict HTTP download route for complete native provider-model snapshots."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, NoReturn

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.responses import PlainTextResponse, StreamingResponse

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_context import current_provider_model_context
from opendata.api.provider_model_query import (
    _object_from_unique_pairs,
    _ProviderModelQueryBodyTooLargeError,
    _read_bounded_request_body,
    _reject_nonfinite_json_constant,
    _validate_json_values,
)
from opendata.data.protocol import FetchContext  # noqa: TC001 - FastAPI resolves Depends
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudgetError,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
)
from opendata.services import provider_model_export_request
from opendata.services.provider_model_export_request import (
    NativeModelExportLease,
    ProviderModelExportRequestCancelledError,
    ProviderModelExportRequestForbiddenError,
    ProviderModelExportRequestIdentityError,
    ProviderModelExportRequestLimitError,
    ProviderModelExportRequestNotFoundError,
    ProviderModelExportRequestTimeoutError,
    ProviderModelExportRequestUnavailableError,
    ProviderModelExportRequestValidationError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import BinaryIO

    from starlette.types import Receive, Scope, Send

router = APIRouter()
_STREAM_CHUNK_BYTES = 64 * 1024
_LIVENESS_POLL_SECONDS = 0.05


class ProviderModelExportRequest(BaseModel):
    """Only one export query object is accepted from the caller."""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: dict[str, Any]


def get_provider_model_export_registry() -> Any:  # noqa: ANN401 - DI supplies process registry
    """Return the existing registry without loading any new providers."""
    from opendata.data.registry import get_registry

    return get_registry()


def get_provider_model_export_context(
    source: str,
    model: str,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
) -> FetchContext | None:
    """Load a fresh, exact EXPORT context from server-owned source policy."""
    return current_provider_model_context(
        source, model, RequestOperation.EXPORT, current_principal=current_principal
    )


def _default_export_engine_factory() -> Any:  # noqa: ANN401 - lazy SQLAlchemy engine seam
    """Resolve the configured warehouse engine only after worker admission."""
    from opendata.api.data_query import get_warehouse_engine

    return get_warehouse_engine()


export_engine_factory = _default_export_engine_factory


class _ExportResponseResourceOwner:
    """Retain a file and lease until every real reader has returned."""

    def __init__(self, lease: NativeModelExportLease, file_handle: BinaryIO) -> None:
        self._lease: NativeModelExportLease | None = lease
        self._file: BinaryIO | None = file_handle
        self._lock = threading.Lock()
        self._active_readers = 0
        self._dispose_requested = False
        self._disposed = False

    @property
    def file_handle(self) -> BinaryIO | None:
        with self._lock:
            return self._file

    def read_chunk(self, size: int) -> bytes:
        """Read without holding the state lock; release only from reader-finally."""
        with self._lock:
            file_handle = self._file
            if self._disposed or self._dispose_requested or file_handle is None:
                return b""
            self._active_readers += 1
        try:
            return file_handle.read(size)
        finally:
            self._reader_terminal()

    def dispose(self) -> None:
        """Request cleanup, deferring it if an AnyIO worker is still reading."""
        with self._lock:
            if self._disposed:
                return
            self._dispose_requested = True
            resources = self._take_resources_if_idle()
        self._finish(resources)

    def _reader_terminal(self) -> None:
        resources: tuple[BinaryIO | None, NativeModelExportLease | None] | None = None
        with self._lock:
            self._active_readers -= 1
            if self._active_readers < 0:
                self._active_readers = 0
                return
            resources = self._take_resources_if_idle()
        self._finish(resources)

    def _take_resources_if_idle(
        self,
    ) -> tuple[BinaryIO | None, NativeModelExportLease | None] | None:
        if not self._dispose_requested or self._active_readers or self._disposed:
            return None
        self._disposed = True
        file_handle = self._file
        lease = self._lease
        self._file = None
        self._lease = None
        return file_handle, lease

    @staticmethod
    def _finish(
        resources: tuple[BinaryIO | None, NativeModelExportLease | None] | None,
    ) -> None:
        if resources is None:
            return
        file_handle, lease = resources
        if file_handle is not None:
            with suppress(Exception):
                file_handle.close()
        if lease is not None:
            lease.cleanup()


class NativeModelExportResponse(StreamingResponse):
    """Stream one completed private spool and own it through ASGI termination."""

    def __init__(self, lease: NativeModelExportLease) -> None:
        """Open the private spool and schedule deadline-owned cleanup."""
        self._lease = lease
        self._resources: _ExportResponseResourceOwner | None = None
        self._disposed = False
        self._active_task: asyncio.Task[Any] | None = None
        self._loop = asyncio.get_running_loop()
        self._timer: asyncio.TimerHandle | None = None
        file_handle: BinaryIO | None = None
        try:
            file_handle = lease.artifact.open_binary()
            self._resources = _ExportResponseResourceOwner(lease, file_handle)
            headers = {
                "Content-Disposition": 'attachment; filename="provider-model-snapshot.ndjson"',
                "Content-Length": str(lease.artifact.byte_count),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Content-SHA256": lease.artifact.sha256,
                "X-Row-Count": str(lease.artifact.row_count),
                "X-NDJSON-Schema-Version": str(lease.artifact.schema_version),
                "X-Snapshot-Complete": "true",
                "X-Snapshot-Consistency": lease.artifact.consistency,
                "X-Completeness": lease.artifact.completeness,
                "X-Export-Created-At": lease.artifact.created_at.isoformat(),
            }
            super().__init__(
                self._iterate_file(),
                status_code=status.HTTP_200_OK,
                headers=headers,
                media_type="application/x-ndjson",
            )
            self._timer = self._loop.call_later(
                _LIVENESS_POLL_SECONDS,
                self._watch_liveness,
            )
        except Exception:
            if self._resources is None and file_handle is not None:
                with suppress(Exception):
                    file_handle.close()
            self._dispose()
            raise

    async def _iterate_file(self) -> AsyncIterator[bytes]:
        while True:
            self._lease.check_live()
            chunk = await anyio.to_thread.run_sync(self._read_chunk, abandon_on_cancel=False)
            if not chunk:
                return
            self._lease.check_live()
            yield chunk

    def _read_chunk(self) -> bytes:
        """Read one bounded block away from the ASGI event loop."""
        resources = self._resources
        if resources is None:
            return b""
        return resources.read_chunk(_STREAM_CHUNK_BYTES)

    @property
    def _file(self) -> BinaryIO | None:
        """Expose the handle state for narrow lifecycle diagnostics and tests."""
        resources = self._resources
        return None if resources is None else resources.file_handle

    def _watch_liveness(self) -> None:
        self._timer = None
        if self._disposed:
            return
        try:
            self._lease.check_live()
        except Exception:
            task = self._active_task
            if task is not None and not task.done():
                task.cancel()
            self._dispose()
            return
        self._timer = self._loop.call_later(
            _LIVENESS_POLL_SECONDS,
            self._watch_liveness,
        )

    def _dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        resources = self._resources
        if resources is None:
            self._lease.cleanup()
        else:
            resources.dispose()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream the file or emit a fixed liveness failure before headers."""
        self._active_task = asyncio.current_task()
        try:
            self._lease.check_live()
        except RequestExecutionDeadlineError:
            self._dispose()
            await PlainTextResponse(
                "Provider model export timed out",
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            )(scope, receive, send)
            return
        except RequestExecutionCancelledError:
            self._dispose()
            await PlainTextResponse(
                "Provider model export was cancelled",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )(scope, receive, send)
            return
        except (RequestAuthorizationError, RequestBudgetScopeError, RequestBudgetError):
            self._dispose()
            await PlainTextResponse(
                "Provider model export is not authorized",
                status_code=status.HTTP_403_FORBIDDEN,
            )(scope, receive, send)
            return
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._active_task = None
            self._dispose()


@router.post(
    "/{source}/models/{model}/warehouse/export",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["query"],
                        "properties": {"query": {"type": "object"}},
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
async def export_registered_provider_model_warehouse(
    source: str,
    model: str,
    request: Request,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    registry: Any = Depends(get_provider_model_export_registry),  # noqa: ANN401 - registry DI
    ctx: FetchContext | None = Depends(get_provider_model_export_context),
) -> StreamingResponse:
    """Return a complete, verified-file-metadata NDJSON snapshot download."""
    admission_started = time.monotonic()
    try:
        raw_body = await provider_model_export_request.await_provider_model_export_request_content(
            lambda: _read_bounded_request_body(request),
            registry=registry,
            source=source,
            model=model,
            principal=current_principal,
            ctx=ctx,
            admission_started=admission_started,
        )
    except _ProviderModelQueryBodyTooLargeError:
        _raise(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Provider model export request is too large"
        )
    except ProviderModelExportRequestIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelExportRequestValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model export request")
    except ProviderModelExportRequestNotFoundError:
        _raise(status.HTTP_404_NOT_FOUND, "Provider model export is unavailable")
    except ProviderModelExportRequestForbiddenError:
        _raise(status.HTTP_403_FORBIDDEN, "Provider model export is not authorized")
    except ProviderModelExportRequestTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Provider model export timed out")
    except ProviderModelExportRequestCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model export was cancelled")
    except ProviderModelExportRequestUnavailableError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model export is unavailable")

    try:
        payload: object = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_object_from_unique_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
        if not _validate_json_values(payload):
            raise ValueError
        body = ProviderModelExportRequest.model_validate(payload)
    except (RecursionError, UnicodeDecodeError, ValueError, ValidationError):
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model export request")

    lease: NativeModelExportLease | None = None
    try:
        lease = await provider_model_export_request.export_provider_model_request(
            engine_factory=export_engine_factory,
            registry=registry,
            source=source,
            model=model,
            query=body.query,
            principal=current_principal,
            ctx=ctx,
            admission_started=admission_started,
        )
        response = NativeModelExportResponse(lease)
        lease = None
        return response
    except ProviderModelExportRequestIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelExportRequestValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model export request")
    except ProviderModelExportRequestNotFoundError:
        _raise(status.HTTP_404_NOT_FOUND, "Provider model export is unavailable")
    except ProviderModelExportRequestForbiddenError:
        _raise(status.HTTP_403_FORBIDDEN, "Provider model export is not authorized")
    except ProviderModelExportRequestLimitError:
        _raise(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Provider model export exceeds its limit")
    except ProviderModelExportRequestTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Provider model export timed out")
    except ProviderModelExportRequestCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model export was cancelled")
    except ProviderModelExportRequestUnavailableError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model export is unavailable")
    except Exception:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model export is unavailable")
    finally:
        if lease is not None:
            lease.cleanup()


def _raise(status_code: int, detail: str) -> NoReturn:
    """Raise a fixed public error without reflecting request or database text."""
    raise HTTPException(status_code=status_code, detail=detail) from None


__all__ = [
    "NativeModelExportResponse",
    "ProviderModelExportRequest",
    "export_registered_provider_model_warehouse",
    "get_provider_model_export_context",
    "get_provider_model_export_registry",
    "router",
]
