"""Read-only metadata helpers for registered provider models.

The service deliberately avoids loading the built-in catalog or provider
models. Callers supply the process registry, and schemas are derived only from
model identities already present in that registry.
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, get_type_hints

if TYPE_CHECKING:
    from opendata.data.registry import ProviderModelDescriptor, ProviderRegistry


class ProviderModelIdentityError(ValueError):
    """Raised when a provider/model identifier is malformed or reserved."""


class ProviderModelNotFoundError(LookupError):
    """Raised when an exact provider/model identity is not registered."""


class ProviderModelSchemaError(RuntimeError):
    """Raised when a registered fetcher does not expose a safe query schema."""


@dataclass(frozen=True)
class ProviderModelMetadata:
    """Public JSON-ready projection of one registered model identity."""

    source: str
    model: str
    domain: str
    capability_identity: dict[str, str]
    verified: bool

    def as_dict(self) -> dict[str, object]:
        """Return the metadata as a JSON-compatible mapping."""
        return {
            "source": self.source,
            "model": self.model,
            "domain": self.domain,
            "capability_identity": dict(self.capability_identity),
            "verified": self.verified,
        }


def validate_source_identity(source: str) -> None:
    """Reject malformed and auto-routed source names without echoing input."""
    if (
        not isinstance(source, str)
        or not source.isidentifier()
        or keyword.iskeyword(source)
        or source.casefold() == "auto"
    ):
        raise ProviderModelIdentityError("malformed provider identity")


def validate_model_identity(source: str, model: str) -> None:
    """Reject malformed and auto-routed exact model identities."""
    validate_source_identity(source)
    if (
        not isinstance(model, str)
        or not model.isidentifier()
        or keyword.iskeyword(model)
        or model.casefold() == "auto"
    ):
        raise ProviderModelIdentityError("malformed provider identity")


def _capability_identity(
    descriptor: ProviderModelDescriptor,
) -> dict[str, str]:
    """Give every part of the registry's full routing identity a name."""
    asset_class, domain, period, market, source = descriptor.full_capability_identity
    return {
        "asset_class": asset_class,
        "domain": domain,
        "period": period,
        "market": market,
        "source": source,
    }


def _metadata(descriptor: ProviderModelDescriptor) -> ProviderModelMetadata:
    """Project an immutable registry descriptor into its API representation."""
    return ProviderModelMetadata(
        source=descriptor.source,
        model=descriptor.model,
        domain=descriptor.domain,
        capability_identity=_capability_identity(descriptor),
        verified=descriptor.verified,
    )


def list_provider_models(
    registry: ProviderRegistry,
    *,
    source: str | None = None,
) -> tuple[ProviderModelMetadata, ...]:
    """List canonical models already registered, optionally for one source.

    An empty registry stays empty. The built-in catalog is metadata only and
    is never used as an implicit source of registrations.
    """
    if source is not None:
        validate_source_identity(source)

    descriptors = registry.list_model_descriptors()
    if source is not None and not any(
        capability.source == source for capability in registry.capabilities()
    ):
        raise ProviderModelNotFoundError("provider not found")
    return tuple(
        _metadata(descriptor)
        for descriptor in descriptors
        if source is None or descriptor.source == source
    )


def find_provider_model(
    registry: ProviderRegistry,
    source: str,
    model: str,
) -> ProviderModelDescriptor:
    """Find one exact descriptor, with no catalog or auto fallback."""
    validate_model_identity(source, model)
    for descriptor in registry.list_model_descriptors():
        if descriptor.source == source and descriptor.model == model:
            return descriptor
    raise ProviderModelNotFoundError("provider model not found")


def query_schema_for_model(
    registry: ProviderRegistry,
    descriptor: ProviderModelDescriptor,
) -> dict[str, Any]:
    """Return the validation JSON Schema declared by the registered fetcher."""
    try:
        fetcher = registry.resolve_model(descriptor.source, descriptor.model)
    except (LookupError, ValueError):
        raise ProviderModelNotFoundError("provider model not found") from None

    try:
        query_type = get_type_hints(type(fetcher).transform_query).get("return")
        from opendata.data.protocol import QueryParams

        if not isinstance(query_type, type) or not issubclass(query_type, QueryParams):
            raise ProviderModelSchemaError("registered model query schema is invalid")
        schema = query_type.model_json_schema(mode="validation")
    except ProviderModelSchemaError:
        raise
    except Exception:
        raise ProviderModelSchemaError("registered model query schema is unavailable") from None

    if not isinstance(schema, dict):
        raise ProviderModelSchemaError("registered model query schema is invalid")
    return schema


__all__ = [
    "ProviderModelIdentityError",
    "ProviderModelMetadata",
    "ProviderModelNotFoundError",
    "ProviderModelSchemaError",
    "find_provider_model",
    "list_provider_models",
    "query_schema_for_model",
    "validate_model_identity",
    "validate_source_identity",
]
