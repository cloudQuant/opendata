"""Nasdaq Trader directory downloader and local symbol search."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqEquitySearch
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq._source import NASDAQ_TRADER_DIRECTORY_URL
from opendata.data.providers.nasdaq.models._common import (
    MAX_DIRECTORY_ROWS,
    NasdaqFetcher,
    build_query,
    optional_float,
    optional_text,
    pipe_file_rows,
)


class EquitySearchQuery(QueryParams):
    """Local directory filters; no arbitrary source-side query is accepted."""

    query: str | None = None
    is_etf: bool | None = None
    include_test_issues: bool = False
    limit: int = Field(default=500, ge=1, le=MAX_DIRECTORY_ROWS)


class EquitySearchFetcher(NasdaqFetcher[EquitySearchQuery, bytes | str]):
    """Download the official pipe-delimited symbol directory and filter it locally."""

    canonical_model: ClassVar[str] = "EquitySearch"
    row_model: ClassVar[type[NasdaqEquitySearch]] = NasdaqEquitySearch
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_equity_search",
        period="snapshot",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquitySearchQuery:
        """Validate bounded local filters before downloading the directory."""
        query = build_query(
            EquitySearchQuery,
            kwargs,
            {"query", "is_etf", "include_test_issues", "limit"},
        )
        term = query.query.strip() if query.query is not None else None
        if term is not None and (not term or len(term) > 100):
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("query",))
        return query.model_copy(update={"query": term})

    def extract_data(self, params: EquitySearchQuery, ctx: FetchContext) -> bytes | str:
        """Download the pipe-delimited Nasdaq Trader directory through governed HTTP."""
        del params
        response = self._get(
            NASDAQ_TRADER_DIRECTORY_URL, {}, ctx, headers=None, decoder="delimited"
        )
        if response.payload is None:
            raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("payload",))
        return response.payload

    def transform_data(self, raw: bytes | str, params: EquitySearchQuery) -> FetchResult:
        """Parse directory columns, filter locally, and return strict symbol records."""
        source_rows = pipe_file_rows(raw)
        if len(source_rows) > MAX_DIRECTORY_ROWS:
            raise ProviderEngineError("NASDAQ_INCOMPLETE")
        term = (params.query or "").casefold()
        output: list[NasdaqEquitySearch] = []
        for item in source_rows:
            symbol = optional_text(item.get("Symbol"))
            name = optional_text(item.get("Security Name"))
            if symbol is None:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID", missing=("Symbol",))
            if term and term not in symbol.casefold() and term not in (name or "").casefold():
                continue
            etf = optional_text(item.get("ETF"))
            if params.is_etf is not None and (etf or "").casefold() != (
                "y" if params.is_etf else "n"
            ):
                continue
            test_issue = optional_text(item.get("Test Issue"))
            if not params.include_test_issues and (test_issue or "").casefold() == "y":
                continue
            output.append(
                self._typed(
                    NasdaqEquitySearch,
                    {
                        "symbol": symbol,
                        "name": name,
                        "nasdaq_traded": optional_text(item.get("Nasdaq Traded")),
                        "exchange": optional_text(item.get("Listing Exchange")),
                        "market_category": optional_text(item.get("Market Category")),
                        "etf": etf,
                        "round_lot_size": optional_float(item.get("Round Lot Size")),
                        "test_issue": test_issue,
                        "financial_status": optional_text(item.get("Financial Status")),
                        "cqs_symbol": optional_text(item.get("CQS Symbol")),
                        "nasdaq_symbol": optional_text(item.get("NASDAQ Symbol")),
                        "next_shares": optional_text(item.get("NextShares")),
                    },
                )
            )
        output.sort(key=lambda row: (row.symbol, row.exchange or ""))
        if len(output) > params.limit:
            output = output[: params.limit]
        return tuple(output)
