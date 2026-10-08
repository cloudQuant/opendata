"""Governed execution endpoint for one exact registered provider model."""

from __future__ import annotations

import json
import math
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_context import current_provider_model_context
from opendata.api.schemas import APIResponse
from opendata.data.protocol import (
    FetchContext,  # noqa: TC001 - FastAPI resolves Depends annotations
)
from opendata.data.request_budget import RequestOperation
from opendata.services.provider_model_query import (
    ProviderModelQueryAuthorizationError,
    ProviderModelQueryBudgetError,
    ProviderModelQueryCancelledError,
    ProviderModelQueryConfigurationError,
    ProviderModelQueryForbiddenError,
    ProviderModelQueryIdentityError,
    ProviderModelQueryNotFoundError,
    ProviderModelQueryOutputError,
    ProviderModelQueryTimeoutError,
    ProviderModelQueryUnavailableError,
    ProviderModelQueryUpstreamError,
    ProviderModelQueryValidationError,
    query_provider_model,
)

router = APIRouter()

_MAX_PROVIDER_MODEL_QUERY_BODY_BYTES = 1024 * 1024
_MAX_PROVIDER_MODEL_QUERY_DEPTH = 256


class ProviderModelQueryRequest(BaseModel):
    """The HTTP request admits only the caller's source query object."""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: dict[str, Any]


def get_provider_model_query_registry() -> Any:  # noqa: ANN401 - DI accepts the process registry
    """Return the existing process registry without loading source adapters."""
    from opendata.data.registry import get_registry

    return get_registry()


def get_provider_model_query_context(
    source: str,
    model: str,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
) -> FetchContext | None:
    """Load a fresh, exact QUERY context from server-owned source policy."""
    return current_provider_model_context(
        source, model, RequestOperation.QUERY, current_principal=current_principal
    )


def _validate_json_values(value: object) -> bool:
    """Accept JSON values iteratively and cap query nesting at 256 levels."""
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_PROVIDER_MODEL_QUERY_DEPTH:
            return False
        if current is None or isinstance(current, (str, int, bool)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
            continue
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                return False
            pending.extend((item, depth + 1) for item in current.values())
            continue
        return False
    return True


class _ProviderModelQueryBodyTooLargeError(Exception):
    """Signal that a streamed request exceeded the bounded JSON body size."""


async def _read_bounded_request_body(request: Request) -> bytes:
    """Read at most one MiB without trusting Content-Length or buffering past it."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_PROVIDER_MODEL_QUERY_BODY_BYTES:
            raise _ProviderModelQueryBodyTooLargeError
        body.extend(chunk)
    return bytes(body)


def _object_from_unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate keys at every level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    """Reject NaN and infinities accepted by Python's permissive JSON parser."""
    raise ValueError("non-finite JSON number")


@router.post(
    "/{source}/models/{model}/query",
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
async def query_registered_provider_model(
    source: str,
    model: str,
    request: Request,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    registry: Any = Depends(get_provider_model_query_registry),  # noqa: ANN401 - injected registry
    ctx: FetchContext | None = Depends(get_provider_model_query_context),
) -> APIResponse:
    """Run one governed Fetcher query without exposing internal error text."""
    body_too_large = False
    raw_body = b""
    try:
        raw_body = await _read_bounded_request_body(request)
    except _ProviderModelQueryBodyTooLargeError:
        body_too_large = True
    if body_too_large:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Provider model query request is too large",
        )

    parse_failed = False
    payload: Any = None
    try:
        payload = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_object_from_unique_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (RecursionError, UnicodeDecodeError, ValueError):
        parse_failed = True
    if (
        not parse_failed
        and isinstance(payload, dict)
        and "query" in payload
        and not _validate_json_values(payload["query"])
    ):
        parse_failed = True
    if parse_failed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid provider model query request",
        )

    body: ProviderModelQueryRequest | None = None
    validation_failed = False
    try:
        body = ProviderModelQueryRequest.model_validate(payload)
    except ValidationError:
        validation_failed = True
    if validation_failed or body is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid provider model query request",
        )

    try:
        data = await query_provider_model(
            registry=registry,
            source=source,
            model=model,
            query=body.query,
            principal=current_principal,
            ctx=ctx,
        )
    except ProviderModelQueryIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelQueryValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model query")
    except ProviderModelQueryNotFoundError:
        _raise(status.HTTP_404_NOT_FOUND, "Provider model not found")
    except ProviderModelQueryForbiddenError:
        _raise(status.HTTP_403_FORBIDDEN, "Not authorized for this provider model")
    except ProviderModelQueryAuthorizationError:
        _raise(status.HTTP_403_FORBIDDEN, "Provider model query is not authorized")
    except ProviderModelQueryConfigurationError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model query is unavailable")
    except ProviderModelQueryUnavailableError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model query is unavailable")
    except ProviderModelQueryBudgetError:
        _raise(status.HTTP_429_TOO_MANY_REQUESTS, "Provider model query budget exhausted")
    except ProviderModelQueryTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Provider model query timed out")
    except ProviderModelQueryCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model query was cancelled")
    except (ProviderModelQueryOutputError, ProviderModelQueryUpstreamError):
        _raise(status.HTTP_502_BAD_GATEWAY, "Provider model query failed")
    except Exception:
        _raise(status.HTTP_502_BAD_GATEWAY, "Provider model query failed")
    return APIResponse(success=True, message="success", data=data)


def _raise(status_code: int, detail: str) -> None:
    """Raise a stable public error without including request or upstream data."""
    raise HTTPException(status_code=status_code, detail=detail) from None


__all__ = [
    "ProviderModelQueryRequest",
    "get_provider_model_query_context",
    "get_provider_model_query_registry",
    "router",
]
