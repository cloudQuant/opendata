"""The H.15 daily Treasury nominal yields, built from their declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.specs import TREASURY_RATES

#: Generated from :data:`TREASURY_RATES`; the class carries the declaration it came from.
FederalReserveTreasuryRatesFetcher = make_http_json_fetcher(SOURCE, TREASURY_RATES)
