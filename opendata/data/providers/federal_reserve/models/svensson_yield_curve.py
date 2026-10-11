"""The Fed's Svensson fitted-factor yield curve, pivoted out of its wide CSV by the engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.specs import SVENSSON_YIELD_CURVE

#: Generated from :data:`SVENSSON_YIELD_CURVE`; the class carries the declaration it came from.
FederalReserveSvenssonYieldCurveFetcher = make_http_json_fetcher(SOURCE, SVENSSON_YIELD_CURVE)
