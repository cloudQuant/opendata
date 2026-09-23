"""First-party fuyao (THS) provider package (A3.4).

The transport lives in :mod:`opendata_fuyao`; this package adapts it to the
three-stage provider protocol so ``source=ths`` becomes routable as the
domestic authority declared by ``authority.json``.
"""

from opendata.data.providers.ths.registration import FETCHERS, register

__all__ = ["FETCHERS", "register"]
