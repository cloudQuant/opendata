"""FINRA weekly summary rows mapped to the public OTC aggregate contract."""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.finra import OTCAggregate
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.finra.models import _client

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpGet

DATASET_URL = f"{_client.API_BASE}/data/group/otcmarket/name/weeklysummary"
PARTITIONS_URL = f"{_client.API_BASE}/partitions/group/otcmarket/name/weeklysummary"
TIER_SOURCE_CODE = {"T1": "T1", "T2": "T2", "OTCE": "OTC"}
SUMMARY_TYPE = {True: "ATS_W_SMBL", False: "OTC_W_SMBL"}
FIELDS = (
    "issueSymbolIdentifier",
    "totalWeeklyShareQuantity",
    "totalWeeklyTradeCount",
    "lastUpdateDate",
)


class OTCAggregateQuery(QueryParams):
    """Bounded query for one latest FINRA weekly OTC/ATS partition."""

    source: Literal["finra"] = "finra"
    market: Literal["us"] = "us"
    symbol: str | None = None
    tier: Literal["T1", "T2", "OTCE"] = "T1"
    is_ats: bool | None = True

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().upper()
        if not cleaned or len(cleaned) > 20 or re.fullmatch(r"[A-Z0-9.$-]+", cleaned) is None:
            raise ValueError("FINRA symbol must be a bounded plain ticker")
        return cleaned

    @model_validator(mode="after")
    def _reject_unpublished_filters(self) -> OTCAggregateQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("FINRA OTC aggregate selects the latest source partition")
        return self


class OTCAggregateFetcher(Fetcher[OTCAggregateQuery, list[dict[str, Any]]]):
    """Fetch one latest, source-filtered FINRA weekly summary partition."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OTCAggregate"
    row_model: ClassVar[type[OTCAggregate]] = OTCAggregate
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="finra_otc_aggregate",
        period="weekly",
        market="us",
        source="finra",
        verified=False,
        notes="FINRA weeklySummary mapping implemented; credentialed provider run pending.",
    )

    def transform_query(self, **kwargs: object) -> OTCAggregateQuery:
        """Validate query fields before authentication or network access."""
        return OTCAggregateQuery.model_validate(kwargs)

    def extract_data(self, params: OTCAggregateQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Resolve the latest source partition and page its bounded row set."""
        token = _client.access_token(ctx, transport=self.http_transport)
        partition_doc, _ = _client.get_json(
            PARTITIONS_URL,
            params=None,
            token=token,
            context=ctx,
            transport=self.http_transport,
        )
        week_start = self._latest_week(partition_doc, TIER_SOURCE_CODE[params.tier])
        filters = [
            self._filter("summaryStartDate", week_start.isoformat()),
            self._filter("tierIdentifier", TIER_SOURCE_CODE[params.tier]),
            self._filter(
                "summaryTypeCode",
                SUMMARY_TYPE[True if params.is_ats is None else params.is_ats],
            ),
        ]
        if params.symbol is not None:
            filters.append(self._filter("issueSymbolIdentifier", params.symbol))
        return _client.post_rows(
            DATASET_URL,
            token=token,
            fields=FIELDS,
            compare_filters=filters,
            context=ctx,
            transport=self.http_transport,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: OTCAggregateQuery,
    ) -> FetchResult:
        """Project FINRA's named weekly summary fields into typed contract rows."""
        result: list[OTCAggregate] = []
        for row in raw:
            source_symbol = row.get("issueSymbolIdentifier")
            if (
                params.symbol is not None
                and isinstance(source_symbol, str)
                and source_symbol.strip().upper() != params.symbol
            ):
                raise _client.FinraProviderError("FINRA_QUERY_SYMBOL_MISMATCH")
            mapped = {
                "update_date": row.get("lastUpdateDate"),
                "symbol": row.get("issueSymbolIdentifier"),
                "share_quantity": row.get("totalWeeklyShareQuantity"),
                "trade_quantity": row.get("totalWeeklyTradeCount"),
            }
            try:
                result.append(OTCAggregate.model_validate(mapped))
            except (TypeError, ValueError):
                raise _client.FinraProviderError("FINRA_OTC_AGGREGATE_BAD_ROW") from None
        return tuple(result)

    @staticmethod
    def _latest_week(document: object, tier: str) -> date:
        """Require documented partition names and choose the newest matching week."""
        if not isinstance(document, dict):
            raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_SHAPE")
        fields = document.get("partitionFields")
        partitions = document.get("availablePartitions")
        if fields != ["weekStartDate", "tierIdentifier"] or not isinstance(partitions, list):
            raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_SHAPE")
        matches: list[date] = []
        for item in partitions:
            if not isinstance(item, dict):
                raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_SHAPE")
            values = item.get("partitions")
            if not isinstance(values, list) or len(values) != 2:
                raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_SHAPE")
            raw_date, raw_tier = values
            if raw_tier != tier:
                continue
            if not isinstance(raw_date, str):
                raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_SHAPE")
            try:
                matches.append(date.fromisoformat(raw_date))
            except ValueError:
                raise _client.FinraProviderError("FINRA_PARTITIONS_BAD_DATE") from None
        if not matches:
            raise _client.FinraProviderError("FINRA_PARTITION_NOT_FOUND")
        return max(matches)

    @staticmethod
    def _filter(name: str, value: str) -> dict[str, str]:
        return {"fieldName": name, "fieldValue": value, "compareType": "EQUAL"}
