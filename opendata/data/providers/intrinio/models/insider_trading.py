"""intrinio's insider-trading feed: one GET per company, every filing's transactions exploded.

The declaration in :data:`~opendata.data.providers.intrinio.specs.INSIDER_TRADING` carries the
``{symbol}`` path, the ``transaction_filings`` row key, the per-filing explode that copies the
filing-level scalars onto every transaction row, the six renamed transaction keys and the ``''``
sentinels the upstream before-validator nulls, so this module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import INSIDER_TRADING

#: Generated from :data:`INSIDER_TRADING`; the class carries the declaration it came from.
IntrinioInsiderTradingFetcher = make_http_json_fetcher(SOURCE, INSIDER_TRADING)
