"""Registration facade for the Finviz provider descriptor."""

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
    """Register Finviz's provider-described fetchers into the registry."""
    return register_provider("finviz", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the provider descriptor's fetchers for legacy imports."""
    if name == "FETCHERS":
        return fetchers_for("finviz")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
