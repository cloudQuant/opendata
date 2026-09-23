"""First-party fred provider package (C1 P0, FR-7).

FRED is a plain JSON REST service (no SDK): the HTTP access point is
``models/_client.py``. A missing API key fails closed with a stable code,
so routing and registration work without credentials.
"""

from opendata.data.providers.fred.registration import FETCHERS, register

__all__ = ["FETCHERS", "register"]
