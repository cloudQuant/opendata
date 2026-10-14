"""The overnight bank funding rate, built from its declaration by the shared engine."""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.federal_reserve._source import SOURCE
from opendata.data.providers.federal_reserve.specs import OVERNIGHT_BANK_FUNDING_RATE

#: Generated from :data:`OVERNIGHT_BANK_FUNDING_RATE`; the class carries the declaration it came
#: from. Census carrier OBB2-federal_reserve-OvernightBankFundingRate; C99 registered the domain in
#: domains.yaml beside federal_reserve_sofr, so ``row_model`` is the reviewed contract class itself.
FederalReserveOvernightBankFundingRateFetcher = make_http_json_fetcher(
    SOURCE, OVERNIGHT_BANK_FUNDING_RATE
)
