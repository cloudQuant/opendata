"""Strict API route for bounded local native provider-model reads."""

from __future__ import annotations

import json
import time
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_context import current_provider_model_context
from opendata.api.provider_model_query import (
    _object_from_unique_pairs,
    _ProviderModelQueryBodyTooLargeError,
    _read_bounded_request_body,
    _reject_nonfinite_json_constant,
    _validate_json_values,
)
from opendata.api.schemas import APIResponse
from opendata.data.protocol import (
    FetchContext,  # noqa: TC001 - FastAPI resolves Depends annotations
)
from opendata.data.request_budget import RequestOperation
from opendata.services.provider_model_warehouse import (
    ProviderModelWarehouseAuthorizationError,
    ProviderModelWarehouseCancelledError,
    ProviderModelWarehouseConfigurationError,
    ProviderModelWarehouseForbiddenError,
    ProviderModelWarehouseIdentityError,
    ProviderModelWarehouseNotFoundError,
    ProviderModelWarehouseTimeoutError,
    ProviderModelWarehouseUnavailableError,
    ProviderModelWarehouseValidationError,
    await_warehouse_request_content,
    query_provider_model_warehouse,
)

router = APIRouter()


class ProviderModelWarehouseRequest(BaseModel):
    """The HTTP envelope contains only the bounded warehouse query object."""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: dict[str, Any]


def get_provider_model_warehouse_registry() -> Any:  # noqa: ANN401 - DI supplies process registry
    """Return the existing registry without importing source adapters."""
    from opendata.data.registry import get_registry

    return get_registry()


def get_provider_model_warehouse_context(
    source: str,
    model: str,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
) -> FetchContext | None:
    """Load a fresh, exact QUERY context for the warehouse route."""
    return current_provider_model_context(
        source, model, RequestOperation.QUERY, current_principal=current_principal
    )


def _default_warehouse_engine_factory() -> Any:  # noqa: ANN401 - lazy SQLAlchemy engine seam
    """Resolve the established warehouse engine only when the worker calls it."""
    from opendata.api.data_query import get_warehouse_engine

    return get_warehouse_engine()


# This callable is passed as a factory, never injected with Depends or invoked
# on the event loop. Tests may replace this trusted internal seam.
warehouse_engine_factory = _default_warehouse_engine_factory


@router.post(
    "/{source}/models/{model}/warehouse/query",
    response_model=APIResponse,
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
async def query_registered_provider_model_warehouse(
    source: str,
    model: str,
    request: Request,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    registry: Any = Depends(get_provider_model_warehouse_registry),  # noqa: ANN401 - process registry
    ctx: FetchContext | None = Depends(get_provider_model_warehouse_context),
) -> APIResponse:
    """Return one typed SQL-only page from a reviewed native DWD table."""
    admission_started = time.monotonic()
    try:
        raw_body = await await_warehouse_request_content(
            lambda: _read_bounded_request_body(request),
            ctx=ctx,
            admission_started=admission_started,
        )
    except _ProviderModelQueryBodyTooLargeError:
        _raise(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Warehouse query request is too large")
    except ProviderModelWarehouseTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Warehouse query timed out")
    except ProviderModelWarehouseCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Warehouse query was cancelled")
    except ProviderModelWarehouseValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid warehouse query request")

    try:
        payload: object = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_object_from_unique_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
        if not _validate_json_values(payload):
            raise ValueError
        body = ProviderModelWarehouseRequest.model_validate(payload)
    except (RecursionError, UnicodeDecodeError, ValueError, ValidationError):
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid warehouse query request")

    try:
        data = await query_provider_model_warehouse(
            registry=registry,
            source=source,
            model=model,
            query=body.query,
            principal=current_principal,
            engine_factory=warehouse_engine_factory,
            ctx=ctx,
            _admission_started=admission_started,
        )
    except ProviderModelWarehouseIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelWarehouseValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid warehouse query")
    except ProviderModelWarehouseNotFoundError:
        _raise(status.HTTP_404_NOT_FOUND, "Provider model not found")
    except ProviderModelWarehouseForbiddenError:
        _raise(status.HTTP_403_FORBIDDEN, "Not authorized for this provider model")
    except ProviderModelWarehouseAuthorizationError:
        _raise(status.HTTP_403_FORBIDDEN, "Warehouse query is not authorized")
    except ProviderModelWarehouseConfigurationError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Warehouse query is unavailable")
    except ProviderModelWarehouseTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Warehouse query timed out")
    except ProviderModelWarehouseCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Warehouse query was cancelled")
    except ProviderModelWarehouseUnavailableError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Warehouse query is unavailable")
    except Exception:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Warehouse query is unavailable")
    return APIResponse(success=True, message="success", data=data)


def _raise(status_code: int, detail: str) -> NoReturn:
    """Emit a fixed public error without reflecting SQL or internal details."""
    raise HTTPException(status_code=status_code, detail=detail) from None


__all__ = [
    "ProviderModelWarehouseRequest",
    "get_provider_model_warehouse_context",
    "get_provider_model_warehouse_registry",
    "router",
    "warehouse_engine_factory",
]
