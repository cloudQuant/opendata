"""Registration facade for congress_gov, keeping the provider descriptor as the source of truth."""

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
    """Register congress_gov's provider-described fetchers into the registry."""
    return register_provider("congress_gov", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the ``FETCHERS`` export from the provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("congress_gov")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
