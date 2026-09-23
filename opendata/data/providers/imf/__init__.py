"""First-party imf provider package (C1 P0, FR-7).

The IMF DataMapper is public (no key) and speaks JSON: the HTTP access
point is ``models/_client.py``. Registration and routing never touch the
network.
"""

from opendata.data.providers.imf.registration import FETCHERS, register

__all__ = ["FETCHERS", "register"]
