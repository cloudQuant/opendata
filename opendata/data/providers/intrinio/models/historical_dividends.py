"""intrinio's dividend/split adjustment feed: one GET per security, rows under a nested key.

The declaration in :data:`~opendata.data.providers.intrinio.specs.HISTORICAL_DIVIDENDS` carries the
``{symbol}`` path, the row key, the three renames and the derive plan that stamps the queried symbol
onto every row, so this module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import HISTORICAL_DIVIDENDS

#: Generated from :data:`HISTORICAL_DIVIDENDS`; the class carries the declaration it came from.
IntrinioHistoricalDividendsFetcher = make_http_json_fetcher(SOURCE, HISTORICAL_DIVIDENDS)
