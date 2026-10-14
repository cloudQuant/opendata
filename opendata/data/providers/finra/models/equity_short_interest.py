"""FINRA consolidated short interest mapped to the public output contract."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.finra import EquityShortInterest
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.finra.models import _client

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

DATASET_URL = f"{_client.API_BASE}/data/group/otcmarket/name/consolidatedshortinterest"
FIELDS = (
    "settlementDate",
    "symbolCode",
    "issueName",
    "marketClassCode",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
    "changePreviousNumber",
    "changePercent",
)


class EquityShortInterestQuery(QueryParams):
    """Fetch consolidated short interest history for one OTC symbol."""

    source: Literal["finra"] = "finra"
    market: Literal["us"] = "us"
    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned or len(cleaned) > 20 or re.fullmatch(r"[A-Z0-9.$-]+", cleaned) is None:
            raise ValueError("FINRA symbol must be a bounded plain ticker")
        return cleaned

    @model_validator(mode="after")
    def _reject_date_filters(self) -> EquityShortInterestQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("FINRA short-interest history is filtered by symbol only")
        return self


class EquityShortInterestFetcher(
    Fetcher[EquityShortInterestQuery, list[dict[str, Any]]],
):
    """Fetch all bounded history rows for a single symbol."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityShortInterest"
    row_model: ClassVar[type[EquityShortInterest]] = EquityShortInterest
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="finra_equity_short_interest",
        period="settlement",
        market="us",
        source="finra",
        verified=False,
        notes=(
            "FINRA consolidatedShortInterest mapping implemented; "
            "credentialed provider run pending."
        ),
    )

    def transform_query(self, **kwargs: object) -> EquityShortInterestQuery:
        """Validate a required ticker and reject date values before I/O."""
        return EquityShortInterestQuery.model_validate(kwargs)

    def extract_data(
        self, params: EquityShortInterestQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Request the selected symbol using FINRA's documented compare filter."""
        token = _client.access_token(ctx, transport=self.http_transport)
        return _client.post_rows(
            DATASET_URL,
            token=token,
            fields=FIELDS,
            compare_filters=[
                {"fieldName": "symbolCode", "fieldValue": params.symbol, "compareType": "EQUAL"}
            ],
            context=ctx,
            transport=self.http_transport,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EquityShortInterestQuery,
    ) -> FetchResult:
        """Map documented source names, including the documented null-volume rule."""
        result: list[EquityShortInterest] = []
        for row in raw:
            source_symbol = row.get("symbolCode")
            if isinstance(source_symbol, str) and source_symbol.strip().upper() != params.symbol:
                raise _client.FinraProviderError("FINRA_QUERY_SYMBOL_MISMATCH")
            average_volume = row.get("averageDailyVolumeQuantity")
            mapped = {
                "settlement_date": row.get("settlementDate"),
                "symbol": row.get("symbolCode"),
                "issue_name": row.get("issueName"),
                "market_class": row.get("marketClassCode"),
                "current_short_position": row.get("currentShortPositionQuantity"),
                "previous_short_position": row.get("previousShortPositionQuantity"),
                # OpenBB's documented contract explicitly translates source NULL to zero.
                "avg_daily_volume": 0 if average_volume is None else average_volume,
                "days_to_cover": row.get("daysToCoverQuantity"),
                "change": row.get("changePreviousNumber"),
                "change_pct": row.get("changePercent"),
            }
            try:
                result.append(EquityShortInterest.model_validate(mapped))
            except (TypeError, ValueError):
                raise _client.FinraProviderError("FINRA_SHORT_INTEREST_BAD_ROW") from None
        result.sort(key=lambda item: item.settlement_date)
        return tuple(result)
