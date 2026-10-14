"""tiingo's crypto history: one GET for every ticker, each object's ``priceData`` exploded.

The declaration in :data:`~opendata.data.providers.tiingo.specs.CRYPTO_HISTORICAL` carries the
``/tiingo/crypto/prices`` path, the ``tickers``/``startDate``/``endDate``/``resampleFreq`` aliases,
the ``1m``->``1min`` interval remap, the one-year relative window, the per-ticker ``priceData``
explode that copies each object's ``ticker`` onto every row it yields, and the client-side
ascending date order, so this module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.tiingo._source import SOURCE
from opendata.data.providers.tiingo.specs import CRYPTO_HISTORICAL

#: Generated from :data:`CRYPTO_HISTORICAL`; the class carries the declaration it came from.
TiingoCryptoHistoricalFetcher = make_http_json_fetcher(SOURCE, CRYPTO_HISTORICAL)
