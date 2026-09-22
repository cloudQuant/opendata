"""Akshare-source provider fetchers for the P0 domains (A2.4).

Wraps the ported ``opendata_http`` flat API in the three-stage
protocol (design §7.1). This package is self-developed code: the
port itself stays pristine in ``opendata_http/`` while the routing
surface - query validation, field mapping, unit conversion - lives
here.

All P0 capabilities register with ``verified=False``: the data has
not passed cross-source validation (A4.5), so ``source="auto"``
routing skips them by design (FR-3) until verification flips the
flag. Explicit ``source=akshare`` routing works immediately.
"""

from opendata.data.capability import Capability
from opendata.data.providers.akshare.registration import register

__all__ = ["register", "Capability"]
