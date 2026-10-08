"""Source descriptions and controlled lazy bindings for synchronous fetchers."""

from __future__ import annotations

import importlib.util
import keyword
import os
from dataclasses import dataclass
from functools import cached_property
from importlib import import_module
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher


@dataclass(frozen=True)
class CredentialSpec:
    """Describe a credential without storing its value."""

    name: str
    environment_variable: str
    settings_attribute: str | None = None
    required_for_health: bool = False


@dataclass(frozen=True)
class OptionalDependencySpec:
    """Describe an optional runtime dependency for a provider."""

    distribution: str
    import_name: str
    required_for_health: bool = False


@dataclass(frozen=True)
class LazyFetcherBinding:
    """Bind one local fetcher class without importing its model module."""

    module: str
    class_name: str
    canonical_model_ids: tuple[str, ...] = ()
    scenario: str | None = None

    def __post_init__(self) -> None:
        """Validate explicitly declared provider model identities."""
        if not isinstance(self.canonical_model_ids, tuple):
            raise TypeError("canonical_model_ids must be a tuple of identifiers")
        if any(
            not isinstance(model_id, str)
            or not model_id.isidentifier()
            or keyword.iskeyword(model_id)
            or model_id.casefold() == "auto"
            for model_id in self.canonical_model_ids
        ):
            raise ValueError("canonical model ids must be non-auto identifiers")
        if len(self.canonical_model_ids) != len(set(self.canonical_model_ids)):
            raise ValueError("a fetcher binding cannot repeat canonical model ids")
        if self.scenario is not None and (
            not isinstance(self.scenario, str) or not self.scenario.strip()
        ):
            raise ValueError("scenario must be a non-blank string when provided")

    @property
    def model_key(self) -> str:
        """Return the model module basename for compatibility projections."""
        return self.module.rsplit(".", maxsplit=1)[-1]

    def load(self, source: str) -> Fetcher[Any, Any]:
        """Import and instantiate a fetcher from its provider's model package.

        Only ``opendata.data.providers.<source>.models`` modules are accepted.
        This deliberately rules out user modules and filesystem discovery.
        """
        prefix = f"opendata.data.providers.{source}.models."
        if not self.module.startswith(prefix) or not self.class_name.isidentifier():
            raise ValueError(f"fetcher binding is outside provider {source!r}")
        if not all(part.isidentifier() for part in self.module.split(".")):
            raise ValueError(f"invalid fetcher module path: {self.module!r}")

        module = import_module(self.module)
        fetcher_type = getattr(module, self.class_name, None)
        from opendata.data.protocol import Fetcher

        if not isinstance(fetcher_type, type) or not issubclass(fetcher_type, Fetcher):
            raise TypeError(f"{self.module}.{self.class_name} is not a Fetcher")
        fetcher = fetcher_type()
        if fetcher.capability.source != source:
            raise ValueError(
                f"fetcher capability source {fetcher.capability.source!r} "
                f"does not match provider {source!r}"
            )
        return fetcher


ProviderHealthStatus = Literal["ready", "missing_key", "missing_sdk", "not_implemented"]


@dataclass(frozen=True)
class ProviderHealth:
    """A local-only provider configuration and dependency result."""

    source: str
    status: ProviderHealthStatus
    missing_credentials: tuple[str, ...] = ()
    missing_dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Provider:
    """One provider's metadata and the sole list of its fetcher bindings."""

    source: str
    name: str
    description: str
    fetcher_bindings: tuple[LazyFetcherBinding, ...] = ()
    credentials: tuple[CredentialSpec, ...] = ()
    optional_dependencies: tuple[OptionalDependencySpec, ...] = ()
    website: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed source ids and repeated bindings early."""
        if (
            not isinstance(self.source, str)
            or not self.source.isidentifier()
            or keyword.iskeyword(self.source)
            or self.source.casefold() == "auto"
        ):
            raise ValueError(f"invalid provider source id: {self.source!r}")
        binding_ids = [(binding.module, binding.class_name) for binding in self.fetcher_bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError(f"provider {self.source!r} has duplicate fetcher bindings")
        model_ids = [
            model_id
            for binding in self.fetcher_bindings
            for model_id in (binding.canonical_model_ids or (binding.model_key,))
        ]
        if len(model_ids) != len(set(model_ids)):
            raise ValueError(f"provider {self.source!r} has duplicate fetcher model ids")

    @property
    def is_implemented(self) -> bool:
        """Whether this source has at least one locally bound fetcher."""
        return bool(self.fetcher_bindings)

    @property
    def fetcher_dict(self) -> Mapping[str, LazyFetcherBinding]:
        """Project model bindings from the sole tuple without duplicating it."""
        return MappingProxyType(
            {
                model_id: binding
                for binding in self.fetcher_bindings
                for model_id in (binding.canonical_model_ids or (binding.model_key,))
            }
        )

    @cached_property
    def fetchers(self) -> tuple[Fetcher[Any, Any], ...]:
        """Load this provider's synchronous fetchers once, on first use."""
        return tuple(binding.load(self.source) for binding in self.fetcher_bindings)

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        """Return capabilities derived from the concrete fetcher classes."""
        return tuple(fetcher.capability for fetcher in self.fetchers)

    def health_check(self) -> ProviderHealth:
        """Check local keys and optional imports without contacting a source."""
        if not self.is_implemented:
            return ProviderHealth(source=self.source, status="not_implemented")

        missing_credentials = tuple(
            spec.name
            for spec in self.credentials
            if spec.required_for_health and not _credential_is_configured(spec)
        )
        missing_dependencies = tuple(
            spec.distribution
            for spec in self.optional_dependencies
            if spec.required_for_health and not _dependency_is_installed(spec)
        )
        if missing_credentials:
            status: ProviderHealthStatus = "missing_key"
        elif missing_dependencies:
            status = "missing_sdk"
        else:
            status = "ready"
        return ProviderHealth(
            source=self.source,
            status=status,
            missing_credentials=missing_credentials,
            missing_dependencies=missing_dependencies,
        )


def _credential_is_configured(spec: CredentialSpec) -> bool:
    """Check an environment variable, then its optional local settings field."""
    value = os.environ.get(spec.environment_variable)
    if value and value.strip():
        return True
    if spec.settings_attribute is None:
        return False

    from opendata.core.config import get_settings

    setting_value = getattr(get_settings(), spec.settings_attribute, None)
    return isinstance(setting_value, str) and bool(setting_value.strip())


def _dependency_is_installed(spec: OptionalDependencySpec) -> bool:
    """Inspect import metadata without importing the optional SDK."""
    try:
        return importlib.util.find_spec(spec.import_name) is not None
    except (ImportError, ValueError):
        return False
