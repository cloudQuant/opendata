"""intrinio's company search: one GET, rows under 'companies', keys renamed on publish.

The declaration in :data:`~opendata.data.providers.intrinio.specs.EQUITY_SEARCH` carries the
path, the row key, the id->intrinio_id and ticker->symbol renames and the limit->page_size
alias, so this module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import EQUITY_SEARCH

#: Generated from :data:`EQUITY_SEARCH`; the class carries the declaration it came from.
IntrinioEquitySearchFetcher = make_http_json_fetcher(SOURCE, EQUITY_SEARCH)
