"""The secured overnight financing rate, built from its declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.specs import SOFR

#: Generated from :data:`SOFR`; the class carries the declaration it came from.
FederalReserveSOFRFetcher = make_http_json_fetcher(SOURCE, SOFR)
