"""Registration facade for benzinga, keeping the provider descriptor as the source of truth.

The descriptor is the whole wiring: ``register_provider`` reads its bindings, so this module
has no fetcher table to keep in step, and the ``FETCHERS`` export below is a projection of
the same descriptor rather than a second list.
"""

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
    """Register benzinga's provider-described fetchers into the registry."""
    return register_provider("benzinga", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the ``FETCHERS`` export from the provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("benzinga")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
