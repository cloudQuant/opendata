"""European index constituent quotes, built from their declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import INDEX_CONSTITUENTS

#: Generated from :data:`INDEX_CONSTITUENTS`; the symbol is addressed in the request path.
CboeIndexConstituentsFetcher = make_http_json_fetcher(SOURCE, INDEX_CONSTITUENTS)
