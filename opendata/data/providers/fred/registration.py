"""Capability registration for the fred source (C1 P0).

One fetcher per implemented domain; the HTTP access lives in
``models/_client.py`` so registration never touches the network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opendata.data.providers.fred.models.cpi import FredCpiFetcher

if TYPE_CHECKING:
    from opendata.data.capability import Capability
    from opendata.data.protocol import Fetcher
    from opendata.data.registry import ProviderRegistry

#: The implemented fred fetchers.
FETCHERS: tuple[Fetcher[Any, Any], ...] = (FredCpiFetcher(),)


def register(registry: ProviderRegistry | None = None) -> list[Capability]:
    """Register the fred fetchers into the registry, idempotently.

    Args:
        registry: Target registry; defaults to the process-wide singleton.

    Returns:
        The capabilities newly registered by this call.
    """
    if registry is None:
        from opendata.data.registry import get_registry

        registry = get_registry()
    existing = {(capability.domain, capability.source) for capability in registry.capabilities()}
    registered: list[Capability] = []
    for fetcher in FETCHERS:
        capability = fetcher.capability
        if (capability.domain, capability.source) in existing:
            continue
        registry.register(fetcher)
        registered.append(capability)
    return registered


__all__ = ["FETCHERS", "register"]
