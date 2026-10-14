"""Intrinio's parameterless forex-pair directory with local query selection.

The official ``/forex/pairs`` endpoint documents no query parameters. ``CurrencyPairs.query`` is a
public consumer-side filter, so the adapter removes it from the wire request while the generated
normalizer applies the declaration's filter to the returned directory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    fetch_pages,
    make_http_json_fetcher,
)
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import CURRENCY_PAIRS

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
    from opendata.data.providers._engine.http_json import HttpGet

    class _GeneratedFetcher(Fetcher[QueryParams, tuple[object, ...]]):
        """Static interface of the fetcher generated from the source declaration."""

        query_model: ClassVar[type[QueryParams]]
        http_transport: ClassVar[HttpGet | None]

        def transform_query(self, **kwargs: object) -> QueryParams: ...

        def extract_data(self, params: object, ctx: FetchContext) -> tuple[object, ...]: ...

        def transform_data(self, raw: tuple[object, ...], params: QueryParams) -> FetchResult: ...
else:
    _GeneratedFetcher = make_http_json_fetcher(SOURCE, CURRENCY_PAIRS)


class IntrinioCurrencyPairsFetcher(_GeneratedFetcher):
    """Generated row handling with the local-only query omitted from HTTP parameters."""

    def extract_data(self, params: object, ctx: FetchContext) -> tuple[object, ...]:
        """Fetch the documented parameterless endpoint through the governed engine transport."""
        if type(params) is not self.query_model:
            raise ProviderEngineError("INTRINIO_QUERY_INVALID")
        wire_query = params.model_copy(update={"query": None})
        return fetch_pages(
            CURRENCY_PAIRS,
            wire_query,
            ctx,
            transport=self.http_transport,
            source=SOURCE,
        )
