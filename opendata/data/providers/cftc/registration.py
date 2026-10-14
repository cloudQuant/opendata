"""Registration facade for CFTC."""

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
    """Register the provider once its catalog descriptor is wired."""
    return register_provider("cftc", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Expose fetchers from the central provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("cftc")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
