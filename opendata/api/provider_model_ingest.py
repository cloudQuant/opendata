"""Strict API route for bounded provider capture and native DWD storage."""

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
from opendata.data.protocol import FetchContext  # noqa: TC001 - FastAPI resolves Depends
from opendata.data.request_budget import RequestOperation
from opendata.services.provider_model_ingest import (
    ProviderModelIngestAuthorizationError,
    ProviderModelIngestCancelledError,
    ProviderModelIngestConfigurationError,
    ProviderModelIngestForbiddenError,
    ProviderModelIngestIdentityError,
    ProviderModelIngestNotFoundError,
    ProviderModelIngestTimeoutError,
    ProviderModelIngestUnavailableError,
    ProviderModelIngestValidationError,
    await_ingest_request_content,
    ingest_provider_model,
)

router = APIRouter()


class ProviderModelIngestRequest(BaseModel):
    """The request envelope contains only one source-native query mapping."""

    model_config = ConfigDict(extra="forbid", strict=True)

    query: dict[str, Any]


def get_provider_model_ingest_context(
    source: str,
    model: str,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
) -> FetchContext | None:
    """Load a fresh, exact STORE context from server-owned source policy."""
    return current_provider_model_context(
        source, model, RequestOperation.STORE, current_principal=current_principal
    )


def get_provider_model_ingest_registry() -> Any:  # noqa: ANN401 - process registry DI
    """Return the existing process registry without loading new providers."""
    from opendata.data.registry import get_registry

    return get_registry()


def _default_ingest_engine_factory() -> Any:  # noqa: ANN401 - lazy SQLAlchemy engine
    """Resolve the configured warehouse engine only in the bounded writer worker."""
    from opendata.api.data_query import get_warehouse_engine

    return get_warehouse_engine()


ingest_engine_factory = _default_ingest_engine_factory


@router.post(
    "/{source}/models/{model}/ingest",
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
async def ingest_registered_provider_model(
    source: str,
    model: str,
    request: Request,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    registry: Any = Depends(get_provider_model_ingest_registry),  # noqa: ANN401 - process registry
    ctx: FetchContext | None = Depends(get_provider_model_ingest_context),
) -> APIResponse:
    """Capture source output once, then atomically store its ODS/DWD batch."""
    admission_started = time.monotonic()
    try:
        raw_body = await await_ingest_request_content(
            lambda: _read_bounded_request_body(request),
            ctx=ctx,
            admission_started=admission_started,
        )
    except _ProviderModelQueryBodyTooLargeError:
        _raise(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Provider model ingest request is too large",
        )
    except ProviderModelIngestIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelIngestValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model ingest request")
    except ProviderModelIngestTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Provider model ingest timed out")
    except ProviderModelIngestCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model ingest was cancelled")

    try:
        payload: object = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=_object_from_unique_pairs,
            parse_constant=_reject_nonfinite_json_constant,
        )
        if not _validate_json_values(payload):
            raise ValueError
        body = ProviderModelIngestRequest.model_validate(payload)
    except (RecursionError, UnicodeDecodeError, ValueError, ValidationError):
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model ingest request")

    try:
        result = await ingest_provider_model(
            registry=registry,
            source=source,
            model=model,
            query=body.query,
            principal=current_principal,
            engine_factory=ingest_engine_factory,
            ctx=ctx,
            _admission_started=admission_started,
        )
    except ProviderModelIngestIdentityError:
        _raise(status.HTTP_400_BAD_REQUEST, "Malformed provider/model identity")
    except ProviderModelIngestValidationError:
        _raise(status.HTTP_400_BAD_REQUEST, "Invalid provider model ingest request")
    except ProviderModelIngestNotFoundError:
        _raise(status.HTTP_404_NOT_FOUND, "Provider model not found")
    except ProviderModelIngestForbiddenError:
        _raise(status.HTTP_403_FORBIDDEN, "Not authorized for this provider model")
    except ProviderModelIngestAuthorizationError:
        _raise(status.HTTP_403_FORBIDDEN, "Provider model store is not authorized")
    except ProviderModelIngestTimeoutError:
        _raise(status.HTTP_504_GATEWAY_TIMEOUT, "Provider model ingest timed out")
    except ProviderModelIngestCancelledError:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model ingest was cancelled")
    except (
        ProviderModelIngestConfigurationError,
        ProviderModelIngestUnavailableError,
    ):
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model ingest is unavailable")
    except Exception:
        _raise(status.HTTP_503_SERVICE_UNAVAILABLE, "Provider model ingest is unavailable")
    return APIResponse(success=True, message="success", data=result)


def _raise(status_code: int, detail: str) -> NoReturn:
    """Raise fixed public errors without reflecting payloads or exception text."""
    raise HTTPException(status_code=status_code, detail=detail) from None


__all__ = [
    "ProviderModelIngestRequest",
    "get_provider_model_ingest_context",
    "get_provider_model_ingest_registry",
    "ingest_engine_factory",
    "ingest_registered_provider_model",
    "router",
]
