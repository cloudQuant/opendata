"""Alpha Vantage reported annual and quarterly EPS history."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date as _date
from typing import TYPE_CHECKING, ClassVar, Literal, TypedDict, overload

from pydantic import StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.alpha_vantage import AlphaVantageHistoricalEps
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.alpha_vantage.client import (
    AlphaVantageClient,
    AlphaVantageResponseError,
)

if TYPE_CHECKING:
    from typing_extensions import Self

_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_NULL_VALUES = {"None", "null", ""}
_KNOWN_PERIOD_KEYS = {
    "fiscalDateEnding",
    "reportedDate",
    "reportedEPS",
    "estimatedEPS",
    "surprise",
    "surprisePercentage",
    "reportTime",
}


class _EarningsRow(TypedDict):
    """Parsed EPS values before typed contract validation."""

    symbol: str
    date: _date
    reported_date: _date | None
    eps_actual: float | None
    eps_estimated: float | None
    surprise: float | None
    surprise_percent: float | None
    report_time: str | None


class AlphaVantageHistoricalEpsQuery(QueryParams):
    """Bounded filter for the provider's unpaginated EARNINGS response."""

    symbol: StrictStr
    period: Literal["annual", "quarter"] | None = "quarter"
    limit: StrictInt | None = None

    @field_validator("symbol")
    @classmethod
    def _trimmed_symbol(cls, value: str) -> str:
        if not value or value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("symbol must be non-empty, trimmed, and contain no controls")
        return value

    @model_validator(mode="after")
    def _valid_filters(self) -> Self:
        if self.source not in {"auto", "alpha_vantage"}:
            raise ValueError("source must be auto or alpha_vantage")
        if self.market not in {None, "global"}:
            raise ValueError("Alpha Vantage declares global capability, not market filtering")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("limit must be a positive integer")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        if self.end_date is not None and self.end_date > _date.today():
            raise ValueError("end_date cannot be in the future")
        return self


class AlphaVantageHistoricalEpsFetcher(
    Fetcher[AlphaVantageHistoricalEpsQuery, tuple[_EarningsRow, ...]]
):
    """Fetch EPS records from one documented, non-paginated endpoint."""

    capability = Capability(
        asset_class="equity",
        domain="alpha_vantage_historical_eps",
        period="historical",
        market="global",
        source="alpha_vantage",
        verified=False,
        notes="Offline fixtures only; provider identity and live semantics are unverified.",
    )
    async_mode = "bounded_thread"
    canonical_model = "HistoricalEps"
    row_model: ClassVar[type[AlphaVantageHistoricalEps]] = AlphaVantageHistoricalEps

    def __init__(self, *, client: AlphaVantageClient | None = None) -> None:
        """Use the supplied Alpha Vantage client or configure the shared client."""
        self._client = client or AlphaVantageClient()

    def transform_query(self, **kwargs: object) -> AlphaVantageHistoricalEpsQuery:
        """Validate annual or quarterly EPS query parameters."""
        return AlphaVantageHistoricalEpsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: AlphaVantageHistoricalEpsQuery,
        ctx: FetchContext,
    ) -> tuple[_EarningsRow, ...]:
        """Fetch and filter the selected EPS series from the EARNINGS response."""
        payload = self._client.get_json(
            {"function": "EARNINGS", "symbol": params.symbol, "datatype": "json"},
            timeout=ctx.timeout,
        )
        allowed = {"symbol", "annualEarnings", "quarterlyEarnings", "name", "currency"}
        unexpected = set(payload) - allowed
        if unexpected:
            raise AlphaVantageResponseError(
                f"Alpha Vantage earnings response has unexpected keys: {sorted(unexpected)}"
            )
        response_symbol = payload.get("symbol")
        if not isinstance(response_symbol, str) or response_symbol.upper() != params.symbol.upper():
            raise AlphaVantageResponseError(
                "Alpha Vantage earnings symbol does not match the query"
            )
        series_key = "annualEarnings" if params.period == "annual" else "quarterlyEarnings"
        records = payload.get(series_key)
        if not isinstance(records, list):
            raise AlphaVantageResponseError(f"Alpha Vantage response is missing {series_key!r}")
        rows = [_parse_earnings_record(params.symbol, item) for item in records]
        rows = [
            row for row in rows if _within_dates(row["date"], params.start_date, params.end_date)
        ]
        rows.sort(key=lambda row: row["date"], reverse=True)
        if params.limit is not None:
            rows = rows[: params.limit]
        return tuple(rows)

    def transform_data(
        self,
        raw: tuple[_EarningsRow, ...],
        params: AlphaVantageHistoricalEpsQuery,
    ) -> tuple[AlphaVantageHistoricalEps, ...]:
        """Validate parsed EPS rows as typed output contracts."""
        return tuple(AlphaVantageHistoricalEps.model_validate(row) for row in raw)


def _parse_earnings_record(symbol: str, value: object) -> _EarningsRow:
    if not isinstance(value, Mapping):
        raise AlphaVantageResponseError("Alpha Vantage earnings item must be an object")
    keys = set(value)
    if "fiscalDateEnding" not in keys or not keys <= _KNOWN_PERIOD_KEYS:
        raise AlphaVantageResponseError("Alpha Vantage earnings item keys are invalid")
    fiscal_date = _parse_date(value["fiscalDateEnding"], "fiscalDateEnding", required=True)
    reported_date = _parse_date(value.get("reportedDate"), "reportedDate", required=False)
    surprise_percent = _parse_optional_number(value.get("surprisePercentage"), "surprisePercentage")
    return {
        "symbol": symbol,
        "date": fiscal_date,
        "reported_date": reported_date,
        "eps_actual": _parse_optional_number(value.get("reportedEPS"), "reportedEPS"),
        "eps_estimated": _parse_optional_number(value.get("estimatedEPS"), "estimatedEPS"),
        "surprise": _parse_optional_number(value.get("surprise"), "surprise"),
        # Alpha Vantage reports percentage points (e.g., 10.0); OpenBB's field is a fraction.
        "surprise_percent": None if surprise_percent is None else surprise_percent / 100.0,
        "report_time": _parse_report_time(value.get("reportTime")),
    }


@overload
def _parse_date(value: object, name: str, *, required: Literal[True]) -> _date: ...


@overload
def _parse_date(value: object, name: str, *, required: Literal[False]) -> _date | None: ...


def _parse_date(value: object, name: str, *, required: bool) -> _date | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be an ISO date")
    try:
        parsed = _date.fromisoformat(value)
    except ValueError as exc:
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be an ISO date")
    return parsed


def _parse_optional_number(value: object, name: str) -> float | None:
    if value is None or (isinstance(value, str) and value in _NULL_VALUES):
        return None
    if not isinstance(value, str) or not _NUMBER.fullmatch(value):
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be a numeric string or null")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise AlphaVantageResponseError(f"Alpha Vantage {name} must be finite")
    return parsed


def _parse_report_time(value: object) -> str | None:
    if value is None or (isinstance(value, str) and value in _NULL_VALUES):
        return None
    if not isinstance(value, str) or value != value.strip() or not value:
        raise AlphaVantageResponseError(
            "Alpha Vantage reportTime must be a non-empty string or null"
        )
    return value


def _within_dates(value: object, start: _date | None, end: _date | None) -> bool:
    if not isinstance(value, _date):
        raise AlphaVantageResponseError("Alpha Vantage fiscal date was not parsed")
    return (start is None or value >= start) and (end is None or value <= end)
