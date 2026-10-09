"""cboe's index search: the published directory, narrowed by a declared client-side filter.

The request is the one :data:`~opendata.data.providers.cboe.specs.AVAILABLE_INDICES` makes; what
makes this a model of its own is the selection, and the selection is declared as two gated
:class:`~opendata.data.providers._engine.spec.RowFilterSpec` predicates rather than written as a
fetcher subclass.
"""

from __future__ import annotations

from opendata.data.providers._engine.http_json import make_http_json_fetcher
from opendata.data.providers.cboe._source import SOURCE
from opendata.data.providers.cboe.specs import INDEX_SEARCH

#: Generated from :data:`INDEX_SEARCH`; the class carries the declaration it came from.
CboeIndexSearchFetcher = make_http_json_fetcher(SOURCE, INDEX_SEARCH)
