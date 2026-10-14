"""Registration facade for econdb, keeping the provider descriptor as the source of truth.

The descriptor is the whole wiring: ``register_provider`` reads its bindings, so this module holds
no fetcher table to keep in step and ``FETCHERS`` below is a projection of the same descriptor.
Since the C99 splice this is an implemented provider: ``catalog.py`` carries ``ECONDB_PROVIDER``
in ``_UPSTREAM_PROVIDERS``, the ``econdb_available_indicators`` domain block stands in
``opendata/data/domains.yaml`` with ``contract: EconDBAvailableIndicators``, and
``opendata/data/models/__init__.py`` binds that reviewed class -- so calling :func:`register`
publishes the binding instead of raising. Those three shared files stayed outside this package's
exclusive scope while the row was declared and witnessed offline: the splice that made the source
discoverable was the main thread's act, recorded in the iteration-2 wave notes.
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
    """Register econdb's provider-described fetchers into the registry."""
    return register_provider("econdb", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the ``FETCHERS`` export from the provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("econdb")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
