"""Lightweight public package interface for akshare."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendata.data.capability import Capability as Capability
    from opendata.data.providers.akshare.registration import register as register


def __getattr__(name: str) -> object:
    """Load compatibility exports only when explicitly requested."""
    if name == "register":
        from opendata.data.providers.akshare.registration import register

        return register
    if name == "Capability":
        from opendata.data.capability import Capability

        return Capability
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["register", "Capability"]
