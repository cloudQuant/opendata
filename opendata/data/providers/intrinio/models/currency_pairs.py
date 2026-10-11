"""intrinio's forex pair directory: one GET, rows under 'pairs', a declared substring search.

The declaration in :data:`~opendata.data.providers.intrinio.specs.CURRENCY_PAIRS` carries the
path, the row key, the renamed symbol column and the gated ``contains`` filter, so this module is
the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import CURRENCY_PAIRS

#: Generated from :data:`CURRENCY_PAIRS`; the class carries the declaration it came from.
IntrinioCurrencyPairsFetcher = make_http_json_fetcher(SOURCE, CURRENCY_PAIRS)
