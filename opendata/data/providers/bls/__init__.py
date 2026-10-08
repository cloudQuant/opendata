"""Lightweight BLS provider facade."""


def __getattr__(name: str) -> object:
    """Load compatibility exports only when explicitly requested."""
    if name == "register":
        from opendata.data.providers.bls.registration import register

        return register
    if name == "FETCHERS":
        from opendata.data.providers.bls.registration import FETCHERS

        return FETCHERS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
