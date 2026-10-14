"""intrinio's dividend/split adjustment feed: cursor-paged rows under a nested key.

The declaration in :data:`~opendata.data.providers.intrinio.specs.HISTORICAL_DIVIDENDS` carries the
``{symbol}`` path, the row key, the three renames and the derive plan that stamps the queried symbol
onto every row, so this module is the binding, not the logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from opendata.data.providers._engine.http_json import ProviderEngineError, make_http_json_fetcher
from opendata.data.providers.intrinio._source import SOURCE
from opendata.data.providers.intrinio.specs import HISTORICAL_DIVIDENDS

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
    _GeneratedFetcher = make_http_json_fetcher(SOURCE, HISTORICAL_DIVIDENDS)


class IntrinioHistoricalDividendsFetcher(_GeneratedFetcher):
    """Generated row handling with strict validation of the documented cursor envelope."""

    def extract_data(self, params: object, ctx: FetchContext) -> tuple[object, ...]:
        """Fetch through the governed engine and reject absent or invalid cursor metadata."""
        pages = super().extract_data(params, ctx)
        for page in pages:
            if not isinstance(page, dict) or "next_page" not in page:
                raise ProviderEngineError("INTRINIO_SHAPE_INVALID")
            cursor = page["next_page"]
            if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
                raise ProviderEngineError("INTRINIO_SHAPE_INVALID")
        return pages
