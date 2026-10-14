"""Compatibility registration facade for Nasdaq."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.providers.catalog import fetchers_for, register_provider

if TYPE_CHECKING:
    from typing import Any

    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

    FETCHERS: tuple[Fetcher[Any, Any], ...]


def register(registry: ProviderRegistry | None = None) -> list[Capability]:
    """Register descriptor-backed Nasdaq fetchers into the requested registry."""
    return register_provider("nasdaq", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the compatibility fetcher tuple from the provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("nasdaq")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
