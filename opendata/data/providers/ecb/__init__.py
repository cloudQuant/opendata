"""First-party ecb provider package (C1 P0, FR-7).

The ECB Data Portal is public (no key) and speaks CSV: the HTTP access
point is ``models/_client.py``. Registration and routing never touch the
network.
"""

from opendata.data.providers.ecb.registration import FETCHERS, register

__all__ = ["FETCHERS", "register"]
