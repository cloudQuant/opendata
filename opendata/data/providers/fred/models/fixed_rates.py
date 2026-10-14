"""Bounded, documented fixed-series FRED rate adapters."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeAlias

from pydantic import ValidationError

from opendata.data.capability import Capability
from opendata.data.models.fred_iter2 import (
    FredAmeribor,
    FredDiscountWindowPrimaryCreditRate,
    FredEuropeanCentralBankInterestRate,
    FredTipsYield,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.series import FredSeriesFetcher, FredSeriesQuery
from opendata.data.providers.fred.models.treasury_rates import _FixedRateQuery

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet

AmeriborFrequency: TypeAlias = Literal[
    "a", "q", "m", "w", "wef", "weth", "wew", "wetu", "wem", "wesu", "wesa", "bwew", "bwem"
]
TipsFrequency: TypeAlias = Literal[
    "a", "q", "m", "w", "d", "wef", "weth", "wew", "wetu", "wem", "wesu", "wesa", "bwew", "bwem"
]
RateTransform: TypeAlias = Literal["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"]
DiscountWindowFrequency: TypeAlias = Literal["w", "m", "a"]

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_AMERIBOR_SERIES: dict[str, tuple[str, str, str]] = {
    "overnight": (
        "AMERIBOR",
        "overnight",
        "Overnight Unsecured AMERIBOR Benchmark Interest Rate",
    ),
    "average_30d": (
        "AMBOR30",
        "30d",
        "30-Day Moving Average AMERIBOR Benchmark Interest Rate",
    ),
    "average_90d": (
        "AMBOR90",
        "90d",
        "90-Day Moving Average AMERIBOR Benchmark Interest Rate",
    ),
    "term_30d": (
        "AMBOR30T",
        "30d",
        "AMERIBOR Term-30 Derived Interest Rate Index",
    ),
    "term_90d": (
        "AMBOR90T",
        "90d",
        "AMERIBOR Term-90 Derived Interest Rate Index",
    ),
}
_ECB_SERIES = {
    "deposit": "ECBDFR",
    "lending": "ECBMLFR",
    "refinancing": "ECBMRRFR",
}
_TIPS_SERIES: dict[str, tuple[str, str]] = {
    "5": (
        "DFII5",
        "Market Yield on U.S. Treasury Securities at 5-Year Constant Maturity, "
        "Quoted on an Investment Basis, Inflation-Indexed",
    ),
    "10": (
        "DFII10",
        "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity, "
        "Quoted on an Investment Basis, Inflation-Indexed",
    ),
    "20": (
        "DFII20",
        "Market Yield on U.S. Treasury Securities at 20-Year Constant Maturity, "
        "Quoted on an Investment Basis, Inflation-Indexed",
    ),
    "30": (
        "DFII30",
        "Market Yield on U.S. Treasury Securities at 30-Year Constant Maturity, "
        "Quoted on an Investment Basis, Inflation-Indexed",
    ),
}
_RateRow = (
    FredAmeribor
    | FredDiscountWindowPrimaryCreditRate
    | FredEuropeanCentralBankInterestRate
    | FredTipsYield
)


class _ObservedRateQuery(_FixedRateQuery):
    """Common date and real-time selection for a known FRED series family."""

    frequency: str | None = None
    aggregation_method: Literal["avg", "sum", "eop"] = "avg"
    transform: RateTransform = "lin"


class AmeriborQuery(_ObservedRateQuery):
    """Documented active AMERIBOR measures, with all as the default."""

    market: Literal["us"] | None = None
    frequency: AmeriborFrequency | None = None
    maturity: (
        Literal["all", "overnight", "average_30d", "average_90d", "term_30d", "term_90d"] | None
    ) = "all"


class DiscountWindowPrimaryCreditRateQuery(_FixedRateQuery):
    """Daily DPCREDIT observations with documented lower-frequency views."""

    market: Literal["us"] | None = None
    parameter: Literal["daily_excl_weekend", "monthly", "weekly", "daily", "annual"] = (
        "daily_excl_weekend"
    )


class EuropeanCentralBankInterestRatesQuery(_FixedRateQuery):
    """One of the three documented ECB policy-rate series."""

    market: Literal["eu"] | None = None
    interest_rate_type: Literal["deposit", "lending", "refinancing"] = "lending"


class TipsYieldsQuery(_ObservedRateQuery):
    """One or all of the four documented constant-maturity TIPS series."""

    market: Literal["us"] | None = None
    maturity: Literal["5", "10", "20", "30"] | None = None
    frequency: TipsFrequency | None = None


class _FixedSeriesFetcher(Fetcher[QueryParams, list[dict[str, Any]]]):
    """Read only explicitly mapped series through the shared FRED pager."""

    async_mode = "bounded_thread"
    query_model: ClassVar[type[QueryParams]]
    output_model: ClassVar[type[Any]]
    row_model: ClassVar[type[ContractModel]]
    canonical_model: ClassVar[str | None]
    capability: ClassVar[Capability]
    #: Offline witness seam; ``None`` keeps the governed production HTTP client path.
    http_transport: ClassVar[HttpGet | None] = None

    def transform_query(self, **kwargs: object) -> QueryParams:
        """Reject unknown choices and invalid date/PIT combinations before I/O."""
        return self.query_model.model_validate(kwargs)

    def selected_series(self, params: QueryParams) -> list[tuple[str, str]]:
        """Return stable (selection key, official FRED series ID) pairs."""
        raise NotImplementedError

    def source_frequency(self, params: QueryParams) -> str | None:
        """Return the FRED aggregation frequency, if this model uses one."""
        return getattr(params, "frequency", None)

    def source_transform(self, params: QueryParams) -> str:
        return getattr(params, "transform", "lin")

    def extract_data(self, params: QueryParams, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch each fixed series under the existing record/page bounds."""
        rows: list[dict[str, Any]] = []
        try:
            for selection, series_id in self.selected_series(params):
                query_values = params.model_dump(mode="python")
                observation_values = {
                    key: value
                    for key, value in query_values.items()
                    if key in FredSeriesQuery.model_fields
                }
                observation_query = FredSeriesQuery.model_validate(
                    {
                        **observation_values,
                        "series_id": series_id,
                        "transform_units": self.source_transform(params),
                        "output_type": 1,
                        "frequency": self.source_frequency(params),
                        "aggregation_method": getattr(params, "aggregation_method", "avg"),
                    }
                )
                inner = FredSeriesFetcher()
                inner.http_transport = self.http_transport
                observations = inner.extract_data(observation_query, ctx)
                rows.extend(
                    {"_selection": selection, "_series_id": series_id, "observation": item}
                    for item in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return rows

    def make_output_row(
        self,
        *,
        selection: str,
        series_id: str,
        observation_date: date_type,
        value: float | None,
    ) -> _RateRow:
        raise NotImplementedError

    def transform_data(self, raw: list[dict[str, Any]], params: QueryParams) -> FetchResult:
        """Validate date, value and vintage identity without hiding revisions."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        seen: set[tuple[str, date_type]] = set()
        rows: list[Any] = []
        try:
            for item in raw:
                if type(item) is not dict or type(item.get("observation")) is not dict:
                    raise ValueError("an observation wrapper must be a JSON object")
                selection = item["_selection"]
                series_id = item["_series_id"]
                observation = item["observation"]
                if type(selection) is not str or type(series_id) is not str:
                    raise ValueError("fixed series identity must be text")
                observation_date = _parse_date(observation["date"])
                realtime_start = _parse_date(observation["realtime_start"])
                realtime_end = _parse_date(observation["realtime_end"])
                if realtime_start > realtime_end:
                    raise ValueError("real-time interval is reversed")
                if params.start_date is not None and observation_date < params.start_date:
                    raise ValueError("observation precedes requested start date")
                if params.end_date is not None and observation_date > params.end_date:
                    raise ValueError("observation follows requested end date")
                _validate_vintage(realtime_start, realtime_end, params)
                identity = (series_id, observation_date)
                if identity in seen:
                    raise ValueError("multiple revisions do not fit the output key")
                seen.add(identity)
                row = self.make_output_row(
                    selection=selection,
                    series_id=series_id,
                    observation_date=observation_date,
                    value=_parse_rate(observation["value"]),
                )
                row._realtime_start = realtime_start
                row._realtime_end = realtime_end
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        reverse = getattr(params, "sort_order", "asc") == "desc"
        rows.sort(key=lambda row: (row.date, getattr(row, "symbol", "")), reverse=reverse)
        return tuple(rows)


class AmeriborFetcher(_FixedSeriesFetcher):
    """FRED's five active AMERIBOR rate series."""

    canonical_model = "Ameribor"
    capability = Capability(
        asset_class="macro",
        domain="fred_ameribor",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = AmeriborQuery
    output_model = FredAmeribor
    row_model: ClassVar[type[ContractModel]] = FredAmeribor

    def selected_series(self, params: QueryParams) -> list[tuple[str, str]]:
        """Resolve `all` to the five separately identified active series."""
        if not isinstance(params, AmeriborQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        maturity = params.maturity or "all"
        selections = list(_AMERIBOR_SERIES) if maturity == "all" else [maturity]
        return [(selection, _AMERIBOR_SERIES[selection][0]) for selection in selections]

    def make_output_row(
        self,
        *,
        selection: str,
        series_id: str,
        observation_date: date_type,
        value: float | None,
    ) -> FredAmeribor:
        """Build the documented row with its selected series metadata."""
        if value is None:
            raise ValueError("AMERIBOR output requires a numeric rate")
        _, maturity, title = _AMERIBOR_SERIES[selection]
        return FredAmeribor.model_validate(
            {
                "date": observation_date,
                "symbol": series_id,
                "maturity": maturity,
                "rate": value,
                "title": title,
            }
        )


class DiscountWindowPrimaryCreditRateFetcher(_FixedSeriesFetcher):
    """FRED DPCREDIT observations, optionally aggregated by FRED."""

    canonical_model = "DiscountWindowPrimaryCreditRate"
    capability = Capability(
        asset_class="macro",
        domain="fred_discount_window_primary_credit_rate",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = DiscountWindowPrimaryCreditRateQuery
    output_model = FredDiscountWindowPrimaryCreditRate
    row_model: ClassVar[type[ContractModel]] = FredDiscountWindowPrimaryCreditRate
    _FREQUENCY_BY_PARAMETER: ClassVar[dict[str, DiscountWindowFrequency | None]] = {
        "daily_excl_weekend": None,
        "daily": None,
        "weekly": "w",
        "monthly": "m",
        "annual": "a",
    }

    def selected_series(
        self,
        params: QueryParams,
    ) -> list[tuple[str, str]]:
        """Use the one official daily primary-credit series."""
        if not isinstance(params, DiscountWindowPrimaryCreditRateQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        return [(params.parameter, "DPCREDIT")]

    def source_frequency(
        self,
        params: QueryParams,
    ) -> DiscountWindowFrequency | None:
        """Use only the frequency aggregate selected by the public parameter."""
        if not isinstance(params, DiscountWindowPrimaryCreditRateQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        return self._FREQUENCY_BY_PARAMETER[params.parameter]

    def make_output_row(
        self,
        *,
        selection: str,
        series_id: str,
        observation_date: date_type,
        value: float | None,
    ) -> FredDiscountWindowPrimaryCreditRate:
        """Build the date/rate row and retain the source ID privately."""
        del selection
        row = FredDiscountWindowPrimaryCreditRate.model_validate(
            {"date": observation_date, "rate": value}
        )
        row._series_id = series_id
        return row


class EuropeanCentralBankInterestRatesFetcher(_FixedSeriesFetcher):
    """ECB deposit, marginal-lending, and main-refinancing rates."""

    canonical_model = "EuropeanCentralBankInterestRates"
    capability = Capability(
        asset_class="macro",
        domain="fred_european_central_bank_interest_rates",
        period="1D",
        market="eu",
        source="fred",
        verified=False,
    )
    query_model = EuropeanCentralBankInterestRatesQuery
    output_model = FredEuropeanCentralBankInterestRate
    row_model: ClassVar[type[ContractModel]] = FredEuropeanCentralBankInterestRate

    def selected_series(
        self,
        params: QueryParams,
    ) -> list[tuple[str, str]]:
        """Select the requested ECB policy-rate series."""
        if not isinstance(params, EuropeanCentralBankInterestRatesQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        return [(params.interest_rate_type, _ECB_SERIES[params.interest_rate_type])]

    def make_output_row(
        self,
        *,
        selection: str,
        series_id: str,
        observation_date: date_type,
        value: float | None,
    ) -> FredEuropeanCentralBankInterestRate:
        """Build the documented date/rate row and retain the source ID."""
        del selection
        row = FredEuropeanCentralBankInterestRate.model_validate(
            {"date": observation_date, "rate": value}
        )
        row._series_id = series_id
        return row


class TipsYieldsFetcher(_FixedSeriesFetcher):
    """Inflation-indexed Treasury constant-maturity yields (5/10/20/30y)."""

    canonical_model = "TipsYields"
    capability = Capability(
        asset_class="macro",
        domain="fred_tips_yields",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = TipsYieldsQuery
    output_model = FredTipsYield
    row_model: ClassVar[type[ContractModel]] = FredTipsYield

    def selected_series(self, params: QueryParams) -> list[tuple[str, str]]:
        """Select one fixed maturity or all four official curve series."""
        if not isinstance(params, TipsYieldsQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        maturities = [params.maturity] if params.maturity is not None else list(_TIPS_SERIES)
        return [(maturity, _TIPS_SERIES[maturity][0]) for maturity in maturities]

    def make_output_row(
        self,
        *,
        selection: str,
        series_id: str,
        observation_date: date_type,
        value: float | None,
    ) -> FredTipsYield:
        """Build a curve point; constant-maturity rows have no issue due date."""
        _, name = _TIPS_SERIES[selection]
        return FredTipsYield.model_validate(
            {
                "date": observation_date,
                "symbol": series_id,
                # These are constant-maturity curve observations, not dated bonds.
                "due": None,
                "name": name,
                "value": value,
            }
        )


def _parse_date(value: object) -> date_type:
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("FRED source date must be canonical YYYY-MM-DD text")
    parsed = date_type.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("FRED source date must be canonical YYYY-MM-DD text")
    return parsed


def _parse_rate(value: object) -> float | None:
    if type(value) is not str:
        raise ValueError("FRED rate token must be text")
    text = value.strip()
    if text == ".":
        return None
    if not text:
        raise ValueError("empty rate token is malformed")
    parsed = float(text)
    if not math.isfinite(parsed):
        raise ValueError("rate must be finite")
    return parsed


def _validate_vintage(
    realtime_start: date_type,
    realtime_end: date_type,
    params: QueryParams,
) -> None:
    if realtime_start > realtime_end:
        raise ValueError("real-time interval is reversed")
    as_of = getattr(params, "as_of", None)
    realtime_window_start = getattr(params, "realtime_start", None)
    realtime_window_end = getattr(params, "realtime_end", None)
    vintage_dates = getattr(params, "vintage_dates", None)
    if as_of is not None and not realtime_start <= as_of <= realtime_end:
        raise ValueError("observation does not cover requested as_of date")
    if realtime_window_start is not None and realtime_end < realtime_window_start:
        raise ValueError("observation ends before requested real-time window")
    if realtime_window_end is not None and realtime_start > realtime_window_end:
        raise ValueError("observation starts after requested real-time window")
    if vintage_dates is not None and not any(
        realtime_start <= vintage <= realtime_end for vintage in vintage_dates
    ):
        raise ValueError("observation does not cover a requested vintage date")


__all__ = (
    "AmeriborFetcher",
    "AmeriborQuery",
    "DiscountWindowPrimaryCreditRateFetcher",
    "DiscountWindowPrimaryCreditRateQuery",
    "EuropeanCentralBankInterestRatesFetcher",
    "EuropeanCentralBankInterestRatesQuery",
    "TipsYieldsFetcher",
    "TipsYieldsQuery",
)
