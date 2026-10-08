"""Lightweight public exports for the built-in provider catalog."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendata.data.providers.catalog import PROVIDERS as PROVIDERS
    from opendata.data.providers.catalog import fetchers_for as fetchers_for
    from opendata.data.providers.catalog import get_provider as get_provider
    from opendata.data.providers.catalog import health_check as health_check
    from opendata.data.providers.catalog import list_providers as list_providers
    from opendata.data.providers.catalog import register_provider as register_provider
    from opendata.data.providers.catalog import register_providers as register_providers

_CATALOG_EXPORTS = {
    "PROVIDERS",
    "fetchers_for",
    "get_provider",
    "health_check",
    "list_providers",
    "register_provider",
    "register_providers",
}


def __getattr__(name: str) -> object:
    """Expose catalog operations without importing models at package import."""
    if name not in _CATALOG_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    catalog = import_module("opendata.data.providers.catalog")
    return getattr(catalog, name)


__all__ = sorted(_CATALOG_EXPORTS)
