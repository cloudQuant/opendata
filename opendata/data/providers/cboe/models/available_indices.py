"""The cboe index directory, built from its declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import AVAILABLE_INDICES

#: Generated from :data:`AVAILABLE_INDICES`; the class carries the declaration it came from.
CboeAvailableIndicesFetcher = make_http_json_fetcher(SOURCE, AVAILABLE_INDICES)
