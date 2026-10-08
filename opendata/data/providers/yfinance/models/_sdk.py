"""yfinance SDK access point (C1 P0, FR-7 optional-dependency rule).

The SDK is an **optional** dependency: this module is the only place that
imports it, lazily, so environments without it can still import and register
the provider package. A missing SDK fails closed with a stable code instead
of an ImportError (the routing layer then skips the capability).

Both seams translate the contract's dates into the upstream's conventions;
the measured behaviours that force the translation are documented on each
function.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pandas as pd

from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError

if TYPE_CHECKING:
    from datetime import date


def require_history_frame(
    symbol: str, *, start: date | None, end: date | None, timeout: float | None
) -> pd.DataFrame | None:
    """Fetch one symbol's daily history through the SDK.

    Three measured upstream behaviours shape this call:

    * ``auto_adjust=False`` is the whole point of the seam. The SDK's default
      folds **dividend** adjustment into ``Open``/``High``/``Low``/``Close``
      (measured against sina's as-traded close over 2026-08-03 .. 2026-09-22:
      KO ``5.93e-03``, MSFT ``1.88e-03``, AAPL ``8.62e-04``, while 0700.HK -
      no dividend in that window - is unaffected), which D10 forbids in
      storage. In raw mode the frame carries the split-adjusted ``Close``
      plus ``Adj Close``, ``Dividends`` and ``Stock Splits``.
      Recompute: ``python scripts/ops/yfinance_overseas_cross_check.py
      --counterfactual``.
    * ``end`` is **exclusive**, while every contract query date is inclusive,
      so the seam shifts it by a day (otherwise the last session of every
      window is silently dropped).
    * While a session is still open the SDK returns it as a bar with ``NaN``
      prices; the normalizer drops those rows.

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
    yfinance = _require_sdk()
    history_kwargs: dict[str, object] = {"interval": "1d", "auto_adjust": False}
    if start is not None:
        history_kwargs["start"] = start.isoformat()
    if end is not None:
        history_kwargs["end"] = (end + timedelta(days=1)).isoformat()
    if timeout is not None:
        history_kwargs["timeout"] = timeout
    history = yfinance.Ticker(symbol.strip()).history(**history_kwargs)
    return cast("pd.DataFrame | None", history)


def require_split_history(symbol: str) -> pd.Series:
    """Fetch one symbol's **complete** split history through the SDK.

    The full list, not the window's rows: the upstream de-visions a price by
    every later split regardless of the requested range (measured: AAPL
    2020-07-14 arrives as 97.0575 although the session traded 388.23, and the
    4:1 event sits outside that window).

    Args:
        symbol: Upstream ticker.

    Returns:
        Split ratios keyed by their session date; empty when the symbol never
        split.

    Raises:
        YfinanceProviderError: The SDK is not installed, or the request failed
            (an empty result would be indistinguishable from "no splits", so
            a transport error must not be read as "nothing to undo").
    """
    yfinance = _require_sdk()
    try:
        splits = yfinance.Ticker(symbol.strip()).splits
    except Exception as exc:  # the SDK re-raises transport errors as requests' own types
        raise YfinanceProviderError("YFINANCE_SPLITS_UNAVAILABLE") from exc
    if splits is None or len(splits) == 0:
        return pd.Series(dtype="float64")
    if isinstance(splits, pd.DataFrame):
        column = "Stock Splits" if "Stock Splits" in splits.columns else splits.columns[0]
        splits = splits[column]
    return cast("pd.Series", pd.Series(splits).astype("float64"))


def _require_sdk() -> Any:  # noqa: ANN401  # untyped optional SDK
    """Import the optional SDK or fail closed with the stable code.

    ``Any`` rather than ``ModuleType``: the SDK has no type stubs, and this
    module is the one seam that is allowed to see its untyped surface.

    Returns:
        The ``yfinance`` module.

    Raises:
        YfinanceProviderError: ``YFINANCE_SDK_MISSING``.
    """
    try:
        import yfinance
    except ImportError as exc:
        raise YfinanceProviderError("YFINANCE_SDK_MISSING") from exc
    return yfinance
