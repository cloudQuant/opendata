"""cboe's options company directory: a CSV download the engine decodes into rows.

The declaration in :data:`~opendata.data.providers.cboe.specs.EQUITY_SEARCH` carries everything
this model does -- the ``?download=csv`` payload constant, the delimited body decoder, the four
published columns and the two gated ``contains`` filters that pick the symbol leg or the company
name leg -- so this module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import EQUITY_SEARCH

#: Generated from :data:`EQUITY_SEARCH`; the class carries the declaration it came from.
CboeEquitySearchFetcher = make_http_json_fetcher(SOURCE, EQUITY_SEARCH)
