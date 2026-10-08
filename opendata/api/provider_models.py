"""Read-only metadata endpoints for models present in the provider registry."""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_export import router as provider_model_export_router
from opendata.api.provider_model_ingest import router as provider_model_ingest_router
from opendata.api.provider_model_query import router as provider_model_query_router
from opendata.api.provider_model_warehouse import router as provider_model_warehouse_router
from opendata.api.schemas import APIResponse
from opendata.services.provider_models import (
    ProviderModelIdentityError,
    ProviderModelMetadata,
    ProviderModelNotFoundError,
    ProviderModelSchemaError,
    find_provider_model,
    list_provider_models,
    query_schema_for_model,
)

router = APIRouter()


def get_provider_model_registry() -> Any:  # noqa: ANN401 - DI accepts the concrete process registry
    """Return the existing registry without registering catalog metadata."""
    from opendata.data.registry import get_registry

    return get_registry()


def _raise_identity_error(exc: ProviderModelIdentityError) -> NoReturn:
    """Translate malformed identity input without reflecting its contents."""
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Malformed provider/model identity",
    ) from exc


def _raise_not_found(exc: ProviderModelNotFoundError) -> NoReturn:
    """Return a consistent not-found response for exact identity misses."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Provider model not found",
    ) from exc


@router.get("/models", response_model=APIResponse)
async def list_registered_provider_models(
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    source: str | None = Query(default=None),
    registry: Any = Depends(get_provider_model_registry),  # noqa: ANN401 - injected registry
) -> APIResponse:
    """List exact registered model identities visible to this principal."""
    try:
        models = list_provider_models(registry, source=source)
    except ProviderModelIdentityError as exc:
        _raise_identity_error(exc)
    except ProviderModelNotFoundError as exc:
        _raise_not_found(exc)

    visible = [model.as_dict() for model in models if current_principal.allows_domain(model.domain)]
    return APIResponse(success=True, message="success", data=visible)


@router.get("/{source}/models/{model}/schema", response_model=APIResponse)
async def get_registered_provider_model_schema(
    source: str,
    model: str,
    current_principal: Any = Depends(get_current_principal),  # noqa: ANN401 - auth principal
    registry: Any = Depends(get_provider_model_registry),  # noqa: ANN401 - injected registry
) -> APIResponse:
    """Return one registered fetcher's complete validation query schema."""
    try:
        descriptor = find_provider_model(registry, source, model)
    except ProviderModelIdentityError as exc:
        _raise_identity_error(exc)
    except ProviderModelNotFoundError as exc:
        _raise_not_found(exc)

    if not current_principal.allows_domain(descriptor.domain):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this provider model",
        )

    try:
        schema = query_schema_for_model(registry, descriptor)
    except ProviderModelNotFoundError as exc:
        _raise_not_found(exc)
    except ProviderModelSchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registered provider model schema is unavailable",
        ) from exc

    metadata = ProviderModelMetadata(
        source=descriptor.source,
        model=descriptor.model,
        domain=descriptor.domain,
        capability_identity={
            "asset_class": descriptor.full_capability_identity[0],
            "domain": descriptor.full_capability_identity[1],
            "period": descriptor.full_capability_identity[2],
            "market": descriptor.full_capability_identity[3],
            "source": descriptor.full_capability_identity[4],
        },
        verified=descriptor.verified,
    )
    data = metadata.as_dict()
    data["schema"] = schema
    return APIResponse(success=True, message="success", data=data)


router.include_router(provider_model_query_router)
router.include_router(provider_model_warehouse_router)
router.include_router(provider_model_export_router)
router.include_router(provider_model_ingest_router)


__all__ = ["get_provider_model_registry", "router"]
