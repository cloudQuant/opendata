"""opendata - multi-source financial data platform."""

from typing import Any

__version__ = "0.1.0"
__author__ = "cloud"
__email__ = "cloud@example.com"

__all__ = ["settings"]


def __getattr__(name: str) -> Any:  # noqa: ANN401 - lazy module attribute
    """Load legacy package-level settings only when a caller requests it."""
    if name == "settings":
        from opendata.core.config import settings

        return settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
