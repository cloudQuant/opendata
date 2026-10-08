"""Lightweight public package interface for yfinance."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendata.data.providers.yfinance.registration import FETCHERS as FETCHERS
    from opendata.data.providers.yfinance.registration import register as register


def __getattr__(name: str) -> object:
    """Load compatibility exports only when explicitly requested."""
    if name == "register":
        from opendata.data.providers.yfinance.registration import register

        return register
    if name == "FETCHERS":
        from opendata.data.providers.yfinance.registration import FETCHERS

        return FETCHERS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
