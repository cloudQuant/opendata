"""Lightweight public package interface for akshare."""


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
