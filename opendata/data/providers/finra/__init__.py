"""FINRA query API adapters for selected OTC datasets."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendata.data.providers.finra.registration import FETCHERS as FETCHERS
    from opendata.data.providers.finra.registration import register as register


def __getattr__(name: str) -> object:
    """Load compatibility exports only when explicitly requested."""
    if name == "register":
        from opendata.data.providers.finra.registration import register

        return register
    if name == "FETCHERS":
        from opendata.data.providers.finra.registration import FETCHERS

        return FETCHERS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register"]
