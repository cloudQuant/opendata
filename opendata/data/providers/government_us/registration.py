"""Compatibility registration facade for government_us."""

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
    """Register the source descriptor into the selected registry."""
    return register_provider("government_us", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project FETCHERS lazily from the central provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("government_us")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
