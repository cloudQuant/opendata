"""Stable FMP historical EOD prices with explicit request and record budgets."""

from __future__ import annotations

from collections import deque
from datetime import date as date_type
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import Field, ValidationError, model_validator

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fmp.models._client import (
    FMP_HISTORICAL_RESPONSE_LIMIT,
    FMPProviderError,
    FMPQueryLimitError,
    fetch_array,
)
from opendata.data.providers.fmp.models._contracts import (
    EquityHistorical,
    FMPQueryParams,
)


class EquityHistoricalQuery(FMPQueryParams):
    """Optional complete date window plus explicit transport and record budgets."""

    max_requests: int = Field(default=32, ge=1, le=256, strict=True)
    max_records: int = Field(default=20_000, ge=1, le=200_000, strict=True)

    @model_validator(mode="after")
    def _validate_window(self) -> EquityHistoricalQuery:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be provided together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class EquityHistoricalFetcher(Fetcher[EquityHistoricalQuery, list[dict[str, Any]]]):
    """Fetch stable EOD rows and fail if a provider or caller limit truncates them."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquityHistorical"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="equity_historical",
        period="1d",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquityHistoricalQuery:
        """Validate symbol, explicit windows, and budgets before key access."""
        return EquityHistoricalQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: EquityHistoricalQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch every requested block; a partial set is always an error."""
        explicit_window = params.start_date is not None and params.end_date is not None
        pending: deque[tuple[date_type, date_type] | None] = deque()
        if explicit_window:
            pending.append((params.start_date, params.end_date))  # type: ignore[arg-type]
        else:
            pending.append(None)

        raw_rows: list[dict[str, Any]] = []
        requests_made = 0
        while pending:
            window = pending.popleft()
            if requests_made >= params.max_requests:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_REQUEST_BUDGET",
                    limit_kind="max_requests",
                    maximum=params.max_requests,
                    requests_made=requests_made,
                )

            request_params = {"symbol": params.symbol}
            if window is not None:
                request_params["from"] = window[0].isoformat()
                request_params["to"] = window[1].isoformat()
            rows = fetch_array(
                "historical-price-eod/full",
                request_params,
                ctx.timeout,
            )
            requests_made += 1

            if len(rows) > FMP_HISTORICAL_RESPONSE_LIMIT:
                raise FMPProviderError(
                    "FMP_PROVIDER_LIMIT_VIOLATION",
                    partial=True,
                    requests_made=requests_made,
                )
            if len(rows) == FMP_HISTORICAL_RESPONSE_LIMIT:
                if window is None:
                    raise FMPQueryLimitError(
                        "FMP_INCOMPLETE_PROVIDER_LIMIT",
                        limit_kind="provider_max_records",
                        maximum=FMP_HISTORICAL_RESPONSE_LIMIT,
                        requests_made=requests_made,
                    )
                left, right = _split_window(*window)
                if left is None or right is None:
                    raise FMPQueryLimitError(
                        "FMP_INCOMPLETE_PROVIDER_LIMIT",
                        limit_kind="provider_max_records",
                        maximum=FMP_HISTORICAL_RESPONSE_LIMIT,
                        requests_made=requests_made,
                    )
                pending.appendleft(right)
                pending.appendleft(left)
                continue

            if len(raw_rows) + len(rows) > params.max_records:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_RECORD_BUDGET",
                    limit_kind="max_records",
                    maximum=params.max_records,
                    requests_made=requests_made,
                )
            raw_rows.extend(rows)

        return raw_rows

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EquityHistoricalQuery,
    ) -> FetchResult:
        """Validate source rows, reject out-of-window/conflicting data, and sort."""
        scope = "explicit" if params.start_date is not None else "provider_default_unknown"
        rows_by_date: dict[date_type, EquityHistorical] = {}
        for item in raw:
            record_data = {
                **item,
                "query_window_scope": scope,
                "window_boundary_semantics": "source_unverified",
                "provider_default_window_semantics": "source_unverified",
            }
            valid = True
            try:
                record = EquityHistorical.model_validate(record_data)
            except (ValidationError, TypeError, ValueError):
                valid = False
            if not valid:
                raise FMPProviderError("FMP_BAD_SHAPE")

            if record.symbol.casefold() != params.symbol.casefold():
                raise FMPProviderError("FMP_WRONG_SYMBOL")
            if (
                params.start_date is not None
                and params.end_date is not None
                and not params.start_date <= record.date <= params.end_date
            ):
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")

            previous = rows_by_date.get(record.date)
            if previous is not None:
                if previous.model_dump() != record.model_dump():
                    raise FMPProviderError("FMP_CONFLICTING_DUPLICATE_DATE")
                continue
            rows_by_date[record.date] = record

        return tuple(rows_by_date[day] for day in sorted(rows_by_date))


def _split_window(
    start: date_type,
    end: date_type,
) -> tuple[tuple[date_type, date_type] | None, tuple[date_type, date_type] | None]:
    """Split a date range into smaller requests sharing the split boundary."""
    span_days = (end - start).days
    if span_days <= 0:
        return None, None
    if span_days == 1:
        return (start, start), (end, end)
    midpoint = start + timedelta(days=span_days // 2)
    return (start, midpoint), (midpoint, end)
