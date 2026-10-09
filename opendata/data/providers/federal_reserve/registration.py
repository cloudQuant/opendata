"""Registration facade for federal_reserve, keeping the provider descriptor as the source of truth.

The descriptor is the whole wiring: ``register_provider`` reads its bindings, so this module has
no fetcher table to keep in step. The catalog still hands ``federal_reserve`` to
``_reserved_provider`` (catalog.py:61), which is why ``register`` returns nothing until that one
entry is replaced by this package's ``PROVIDER`` -- flipping it also moves three literal
provider-count assertions in ``tests/test_provider_catalog.py`` (the implemented-source set, the
46-binding total and the 24-reserved total), which are not this package's files to edit.
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
    """Register federal_reserve's provider-described fetchers into the registry."""
    return register_provider("federal_reserve", registry)


def __getattr__(name: str) -> tuple[Fetcher[Any, Any], ...]:
    """Project the ``FETCHERS`` export from the provider descriptor."""
    if name == "FETCHERS":
        return fetchers_for("federal_reserve")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
