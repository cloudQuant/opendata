"""Stable FMP single-symbol quote fetcher."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ValidationError, model_validator

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fmp.models._client import FMPProviderError, fetch_array
from opendata.data.providers.fmp.models._contracts import EquityQuote, FMPQueryParams


class EquityQuoteQuery(FMPQueryParams):
    """One-symbol quote query with no historical window parameters."""

    @model_validator(mode="after")
    def _reject_date_window(self) -> EquityQuoteQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("quote queries do not accept a historical date window")
        return self


class EquityQuoteFetcher(Fetcher[EquityQuoteQuery, list[dict[str, Any]]]):
    """Fetch a complete ordinary quote array for exactly one requested symbol."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityQuote"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="equity_quote",
        period="snapshot",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquityQuoteQuery:
        """Validate the ordinary quote query before key access."""
        return EquityQuoteQuery.model_validate(kwargs)

    def extract_data(self, params: EquityQuoteQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read only the stable ordinary-quote endpoint."""
        return fetch_array("quote", {"symbol": params.symbol}, ctx.timeout)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EquityQuoteQuery,
    ) -> FetchResult:
        """Validate the full single-symbol response and preserve raw timestamp units."""
        if not raw:
            return ()
        if len(raw) != 1:
            raise FMPProviderError("FMP_QUOTE_EXPECTED_ONE_ROW")

        valid = True
        try:
            record = EquityQuote.model_validate(raw[0])
        except (ValidationError, TypeError, ValueError):
            valid = False
        if not valid:
            raise FMPProviderError("FMP_BAD_SHAPE")
        if record.symbol.casefold() != params.symbol.casefold():
            raise FMPProviderError("FMP_WRONG_SYMBOL")
        return (record,)
