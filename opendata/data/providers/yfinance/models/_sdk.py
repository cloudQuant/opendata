"""yfinance SDK access point (C1 P0, FR-7 optional-dependency rule).

The SDK is an **optional** dependency: this module is the only place that
imports it, lazily, so environments without it can still import and register
the provider package. A missing SDK fails closed with a stable code instead
of an ImportError (the routing layer then skips the capability).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd


def require_history_frame(
    symbol: str, *, start: date | None, end: date | None, timeout: float | None
) -> pd.DataFrame | None:
    """Fetch one symbol's daily history through the SDK.

    Args:
        symbol: Upstream ticker (for example ``AAPL``).
        start: Inclusive start date; ``None`` lets the SDK default apply.
        end: Inclusive end date; ``None`` lets the SDK default apply.
        timeout: Optional request timeout override.

    Returns:
        The SDK's history frame, or ``None`` when the SDK returns nothing.

    Raises:
        YfinanceProviderError: The SDK is not installed (stable code, so the
            caller can treat the capability as unavailable).
    """
    try:
        import yfinance
    except ImportError as exc:
        raise YfinanceProviderError("YFINANCE_SDK_MISSING") from exc
    history_kwargs: dict[str, object] = {"interval": "1d", "auto_adjust": True}
    if start is not None:
        history_kwargs["start"] = start.isoformat()
    if end is not None:
        history_kwargs["end"] = end.isoformat()
    if timeout is not None:
        history_kwargs["timeout"] = timeout
    return yfinance.Ticker(symbol.strip()).history(**history_kwargs)
