"""The H.6 monthly money stock measures, built from its declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.specs import MONEY_MEASURES

#: Generated from :data:`MONEY_MEASURES`; the class carries the declaration it came from.
FederalReserveMoneyMeasuresFetcher = make_http_json_fetcher(SOURCE, MONEY_MEASURES)
