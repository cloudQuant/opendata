"""The EconDB indicator roster, built from its declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.econdb._source import SOURCE
from opendata.data.providers.econdb.specs import AVAILABLE_INDICATORS

#: Generated from :data:`AVAILABLE_INDICATORS`. The engine names the class from the *source* label
#: ("econdb" -> ``Econdb``), so this symbol is not spelled with the ``EconDB`` brand capital.
EcondbAvailableIndicatorsFetcher = make_http_json_fetcher(SOURCE, AVAILABLE_INDICATORS)
