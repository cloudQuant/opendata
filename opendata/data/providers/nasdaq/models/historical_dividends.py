"""Nasdaq historical-dividend fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqHistoricalDividend
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    build_query,
    optional_date,
    optional_float,
    optional_text,
    symbols_from,
)

MAX_DIVIDEND_DAYS = 3660


class HistoricalDividendsQuery(QueryParams):
    """Dividend lookup for a bounded list of symbols and historical window."""

    symbol: str


class HistoricalDividendsFetcher(
    NasdaqFetcher[HistoricalDividendsQuery, tuple[tuple[str, Mapping[str, Any]], ...]]
):
    """Fetch bounded symbol history using Nasdaq's stock asset-class route."""

    canonical_model: ClassVar[str] = "HistoricalDividends"
    row_model: ClassVar[type[NasdaqHistoricalDividend]] = NasdaqHistoricalDividend
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_historical_dividends",
        period="series",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> HistoricalDividendsQuery:
        """Validate the ticker list and cap the date range before any request."""
        query = build_query(
            HistoricalDividendsQuery,
            kwargs,
            {"symbol", "start_date", "end_date"},
        )
        try:
            symbols_from(query.symbol)
        except ValueError as error:
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("symbol",)) from error
        start = query.start_date or (date.today() - timedelta(days=365))
        end = query.end_date or date.today()
        if start > end or (end - start).days + 1 > MAX_DIVIDEND_DAYS:
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date"))
        return query.model_copy(update={"start_date": start, "end_date": end})

    def extract_data(
        self, params: HistoricalDividendsQuery, ctx: FetchContext
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Fetch each validated symbol using the asset-class query from Nasdaq's UI client."""
        output: list[tuple[str, Mapping[str, Any]]] = []
        for symbol in symbols_from(params.symbol):
            path = f"/api/quote/{symbol}/dividends"
            document = self._json(path, {"assetclass": "stocks"}, ctx)
            output.append((symbol, document))
        return tuple(output)

    def transform_data(
        self,
        raw: tuple[tuple[str, Mapping[str, Any]], ...],
        params: HistoricalDividendsQuery,
    ) -> FetchResult:
        """Filter the bounded period and stamp each row with its requested symbol."""
        start = params.start_date
        end = params.end_date
        if start is None or end is None:  # pragma: no cover - transform_query fills both
            raise ProviderEngineError("NASDAQ_QUERY_INVALID")
        output: list[NasdaqHistoricalDividend] = []
        for symbol, document in raw:
            for item in self._rows_or_empty(document, "data", "dividends", "rows"):
                ex_date = optional_date(item.get("exOrEffDate"))
                if ex_date is None or not start <= ex_date <= end:
                    continue
                output.append(
                    self._typed(
                        NasdaqHistoricalDividend,
                        {
                            "symbol": symbol,
                            "ex_dividend_date": ex_date,
                            "amount": optional_float(item.get("amount")),
                            "dividend_type": optional_text(item.get("type")),
                            "currency": optional_text(item.get("currency")),
                            "record_date": optional_date(item.get("recordDate")),
                            "payment_date": optional_date(item.get("paymentDate")),
                            "declaration_date": optional_date(item.get("declarationDate")),
                        },
                    )
                )
        output.sort(key=lambda row: (row.ex_dividend_date, row.symbol or ""))
        return tuple(output)
