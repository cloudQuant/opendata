"""Read-only metadata helpers for registered provider models.

The service deliberately avoids loading the built-in catalog or provider
models. Callers supply the process registry, and schemas are derived only from
model identities already present in that registry.
"""

from __future__ import annotations

import ast
import inspect
import keyword
from dataclasses import dataclass
from types import FunctionType, GenericAlias
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from opendata.data.registry import ProviderModelDescriptor, ProviderRegistry


class ProviderModelIdentityError(ValueError):
    """Raised when a provider/model identifier is malformed or reserved."""


class ProviderModelNotFoundError(LookupError):
    """Raised when an exact provider/model identity is not registered."""


class ProviderModelSchemaError(RuntimeError):
    """Raised when a registered fetcher does not expose a safe query schema."""


_MISSING = object()


def _invalid_query_schema() -> ProviderModelSchemaError:
    """Build a sanitized error for an invalid static query declaration."""
    return ProviderModelSchemaError("registered model query schema is invalid")


def _concrete_query_model(value: object) -> type[BaseModel]:
    """Require a concrete Pydantic model without executing provider code."""
    if value is BaseModel or not isinstance(value, type):
        raise _invalid_query_schema()
    from opendata.data.protocol import BaseQueryParams, MultiSymbolQueryParams

    if value is BaseQueryParams or value is MultiSymbolQueryParams:
        raise _invalid_query_schema()
    try:
        model_mro = type.__getattribute__(value, "__mro__")
    except TypeError:
        raise _invalid_query_schema() from None
    if not isinstance(model_mro, tuple) or BaseModel not in model_mro:
        raise _invalid_query_schema()
    return value


def _raw_method(fetcher_type: type[object], name: str) -> FunctionType | None:
    """Read a method function statically; never bind or call a descriptor."""
    member = inspect.getattr_static(fetcher_type, name, _MISSING)
    if member is _MISSING:
        return None
    if isinstance(member, (staticmethod, classmethod)):
        member = member.__func__
    if not isinstance(member, FunctionType):
        raise _invalid_query_schema()
    return member


def _annotation_expression(annotation: object) -> ast.expr:
    """Parse only a simple name, allowing Python's quoted future annotations."""
    if not isinstance(annotation, str):
        raise _invalid_query_schema()
    try:
        expression = ast.parse(annotation, mode="eval").body
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            expression = ast.parse(expression.value, mode="eval").body
    except (RecursionError, SyntaxError, ValueError, TypeError):
        raise _invalid_query_schema() from None
    return expression


def _named_model(expression: ast.expr, namespace: dict[str, object]) -> type[BaseModel]:
    """Resolve one AST name through the function's existing globals mapping."""
    if not isinstance(expression, ast.Name):
        raise _invalid_query_schema()
    return _concrete_query_model(namespace.get(expression.id, _MISSING))


def _plain_return_model(function: FunctionType) -> type[BaseModel]:
    """Resolve an exact model return annotation without evaluating strings."""
    annotations = function.__annotations__
    if not isinstance(annotations, dict) or "return" not in annotations:
        raise _invalid_query_schema()
    annotation = annotations["return"]
    if isinstance(annotation, type):
        return _concrete_query_model(annotation)
    expression = _annotation_expression(annotation)
    return _named_model(expression, function.__globals__)


def _query_type_return_model(function: FunctionType) -> type[BaseModel]:
    """Safely resolve ``type[ConcreteQuery]`` used by legacy query factories."""
    annotations = function.__annotations__
    if not isinstance(annotations, dict) or "return" not in annotations:
        raise _invalid_query_schema()
    annotation = annotations["return"]
    if isinstance(annotation, str):
        expression = _annotation_expression(annotation)
        if (
            not isinstance(expression, ast.Subscript)
            or not isinstance(expression.value, ast.Name)
            or expression.value.id != "type"
        ):
            raise _invalid_query_schema()
        return _named_model(expression.slice, function.__globals__)

    if type(annotation) is not GenericAlias or annotation.__origin__ is not type:
        raise _invalid_query_schema()
    arguments = annotation.__args__
    if len(arguments) != 1:
        raise _invalid_query_schema()
    return _concrete_query_model(arguments[0])


def resolve_query_model_type(fetcher: object) -> type[BaseModel]:
    """Find a fetcher's concrete query model from its static declaration.

    Explicit ``query_model``/``query_type`` class attributes take priority.
    If neither is present, the resolver accepts only a raw, simple model return
    annotation or a raw ``_query_type() -> type[ConcreteModel]`` annotation.
    It never invokes fetcher methods or descriptors, and never evaluates an
    annotation expression.
    """
    fetcher_type = fetcher if isinstance(fetcher, type) else type(fetcher)
    declared_models: list[type[BaseModel]] = []
    for name in ("query_model", "query_type"):
        declaration = inspect.getattr_static(fetcher_type, name, _MISSING)
        if declaration is not _MISSING:
            declared_models.append(_concrete_query_model(declaration))
    if declared_models:
        if any(model is not declared_models[0] for model in declared_models[1:]):
            raise _invalid_query_schema()
        return declared_models[0]

    query_type_method = _raw_method(fetcher_type, "_query_type")
    if query_type_method is not None:
        return _query_type_return_model(query_type_method)

    transform_method = _raw_method(fetcher_type, "transform_query")
    if transform_method is None:
        raise _invalid_query_schema()
    return _plain_return_model(transform_method)


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
        query_type = resolve_query_model_type(fetcher)
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
