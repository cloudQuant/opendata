"""First-party yfinance provider package (C1 P0, FR-7).

The SDK is an optional dependency imported only by ``models/_sdk.py``;
registration and routing work without it, and fetching fails closed with a
stable code when it is absent.
"""

from opendata.data.providers.yfinance.registration import FETCHERS, register

__all__ = ["FETCHERS", "register"]
