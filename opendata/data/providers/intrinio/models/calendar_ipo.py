"""intrinio's IPO calendar: one GET, rows under a nested key, nested scalars projected flat.

The declaration in :data:`~opendata.data.providers.intrinio.specs.CALENDAR_IPO` carries the row
key, the four query aliases, the parameter value domains and the ``RecordRepointPlan`` that reads
each item's scalars and its nested ``company``/``security`` fields into one flat row, so this
module is the binding, not the logic.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import CALENDAR_IPO

#: Generated from :data:`CALENDAR_IPO`; the class carries the declaration it came from.
IntrinioCalendarIpoFetcher = make_http_json_fetcher(SOURCE, CALENDAR_IPO)
