"""Clean-room Federal Reserve money and rate fetchers.

This module uses only public Federal Reserve Board and New York Fed endpoints.
It is an offline candidate until source-shape and public-output comparisons are
independently reviewed.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import GovernedHttpClient, get_shared_http_client
from opendata.data.models.federal_reserve_clean_rates import (
    FederalReserveFederalFundsRate,
    FederalReserveMoneyMeasure,
    FederalReserveOvernightBankFundingRate,
    FederalReserveSvenssonYieldCurve,
    FederalReserveTreasuryRate,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers._engine.http_json import ProviderEngineQueryError

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

SOURCE = "federal_reserve"
MARKET = "us"
FED_DDP_URL = "https://www.federalreserve.gov/datadownload/Output.aspx"
FEDS_CURVE_URL = "https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv"
NYFED_RATE_URL = "https://markets.newyorkfed.org/api/rates/unsecured/{rate_type}/search.json"
H6_PACKAGE_ID = "798e2796917702a5f8423426ba7e6b42"
H15_WEEK4_PACKAGE_ID = "0245b5533869431aaa4fb50bdd7d258a"
DEFAULT_TIMEOUT_SECONDS = 30.0

_NULL_MARKERS = frozenset({"", "NA", "N/A"})
_H15_NULL_MARKERS = _NULL_MARKERS | {"ND"}
_MONEY_SERIES = {
    "m1": "M1.M",
    "m2": "M2.M",
    "currency": "MCU.M",
    "demand_deposits": "MDD.M",
    "retail_money_market_funds": "MMFGB.M",
    "other_liquid_deposits": "MDL.M",
    "small_denomination_time_deposits": "MDTS.M",
}
_TREASURY_SOURCE_FIELDS = {
    "month_1": "RIFLGFCM01_N.B",
    "month_3": "RIFLGFCM03_N.B",
    "month_6": "RIFLGFCM06_N.B",
    "year_1": "RIFLGFCY01_N.B",
    "year_2": "RIFLGFCY02_N.B",
    "year_3": "RIFLGFCY03_N.B",
    "year_5": "RIFLGFCY05_N.B",
    "year_7": "RIFLGFCY07_N.B",
    "year_10": "RIFLGFCY10_N.B",
    "year_20": "RIFLGFCY20_N.B",
    "year_30": "RIFLGFCY30_N.B",
    "week_4": "RIFSGFSW04_N.B",
}
_SVENSSON_SOURCE_FIELDS = (
    "BETA0",
    "BETA1",
    "BETA2",
    "BETA3",
    "TAU1",
    "TAU2",
    "SVEN1F01",
    "SVEN1F04",
    "SVEN1F09",
    *(f"SVENF{maturity:02d}" for maturity in range(1, 31)),
    *(f"SVENPY{maturity:02d}" for maturity in range(1, 31)),
    *(f"SVENY{maturity:02d}" for maturity in range(1, 31)),
)
_SVENSSON_SOURCE_TO_PUBLIC = {
    source_name: source_name.lower() for source_name in _SVENSSON_SOURCE_FIELDS
}
_SVENSSON_GROUPS = {
    "parameters": frozenset({"beta0", "beta1", "beta2", "beta3", "tau1", "tau2"}),
    "forward_1y": frozenset({"sven1f01", "sven1f04", "sven1f09"}),
    "forward_instantaneous": frozenset(f"svenf{i:02d}" for i in range(1, 31)),
    "par_yield": frozenset(f"svenpy{i:02d}" for i in range(1, 31)),
    "zero_coupon": frozenset(f"sveny{i:02d}" for i in range(1, 31)),
}
_SVENSSON_SELECTORS = frozenset(_SVENSSON_GROUPS) | frozenset(_SVENSSON_SOURCE_TO_PUBLIC.values())


class FederalReserveSourceError(ValueError):
    """Malformed source payload or unsupported source response shape."""


class FederalReserveQueryError(ProviderEngineQueryError):
    """Invalid Federal Reserve query; details are intentionally not echoed."""

    def __init__(self) -> None:
        """Use a stable safe code recognized by the governed query service."""
        super().__init__("FEDERAL_RESERVE_QUERY_INVALID")


class _DateQuery(BaseModel):
    """Common strict query identity and inclusive date bounds."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source: Literal["federal_reserve"] = "federal_reserve"
    market: Literal["us"] = "us"
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _ordered_dates(self) -> _DateQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        return self


class MoneyMeasuresQuery(_DateQuery):
    """H.6 date bounds and seasonal-adjustment selector."""

    adjusted: bool | None = True


class TreasuryRatesQuery(_DateQuery):
    """H.15 inclusive daily date bounds."""


class SvenssonYieldCurveQuery(_DateQuery):
    """FEDS CSV date bounds and public series selector."""

    series_type: str | None = "all"

    @field_validator("series_type")
    @classmethod
    def _validate_series_type(cls, value: str | None) -> str:
        if value is None:
            return "all"
        tokens = tuple(token.strip().lower() for token in value.split(","))
        if not tokens or any(not token for token in tokens):
            raise ValueError("series_type must contain one or more selectors")
        if len(tokens) != len(set(tokens)):
            raise ValueError("series_type selectors must not repeat")
        if "all" in tokens and len(tokens) != 1:
            raise ValueError("all cannot be combined with another series_type selector")
        unknown = set(tokens) - (_SVENSSON_SELECTORS | {"all"})
        if unknown:
            raise ValueError(f"unsupported series_type selector(s): {sorted(unknown)}")
        return ",".join(tokens)


class OvernightBankFundingRateQuery(_DateQuery):
    """NY Fed OBFR inclusive date bounds."""


class FederalFundsRateQuery(_DateQuery):
    """NY Fed EFFR date bounds and public effr_only projection."""

    effr_only: bool | None = False


class _FederalReserveFetcher(Fetcher[Any, object]):
    """Shared governed transport and strict CSV/JSON decoding helpers."""

    async_mode: ClassVar[str] = "bounded_thread"
    capability: ClassVar[Capability]
    canonical_model: ClassVar[str]
    query_type: ClassVar[type[BaseModel]]

    def __init__(self, http_client: GovernedHttpClient | None = None) -> None:
        """Inject an optional governed client for offline transport fixtures."""
        self._http_client = http_client

    def transform_query(self, **kwargs: object) -> BaseModel:
        """Validate query fields, including provider service identity injection."""
        try:
            return self.query_type.model_validate(kwargs)
        except ValidationError:
            raise FederalReserveQueryError() from None
        except (TypeError, ValueError) as exc:
            raise FederalReserveSourceError("invalid Federal Reserve query") from exc

    def _get(
        self, url: str, ctx: FetchContext, *, params: Mapping[str, str] | None = None
    ) -> bytes:
        client = self._http_client or get_shared_http_client()
        response = client.get(
            url,
            params=params,
            headers={"Accept": "text/csv, application/json"},
            timeout=ctx.timeout or DEFAULT_TIMEOUT_SECONDS,
            source=SOURCE,
            cache_context={"model": self.canonical_model},
        )
        return response.content

    @staticmethod
    def _text(payload: object, *, model: str) -> str:
        if not isinstance(payload, bytes):
            raise FederalReserveSourceError(f"{model}: expected a byte response")
        try:
            return payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FederalReserveSourceError(f"{model}: source payload is not UTF-8") from exc


def _date_value(raw: str, *, monthly: bool = False) -> date:
    value = raw.strip()
    if monthly and len(value) == 7 and value[4] == "-":
        try:
            year, month = (int(part) for part in value.split("-", maxsplit=1))
            return date(year, month, 1)
        except (OverflowError, ValueError) as exc:
            raise FederalReserveSourceError(f"invalid monthly date: {value!r}") from exc
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise FederalReserveSourceError(f"invalid observation date: {value!r}") from exc


def _number(
    raw: object,
    *,
    field_name: str,
    null_markers: frozenset[str] = _NULL_MARKERS,
) -> float | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if value.upper() in null_markers:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FederalReserveSourceError(f"invalid numeric {field_name}: {value!r}") from exc
    if not math.isfinite(number):
        raise FederalReserveSourceError(f"non-finite numeric {field_name}")
    return number


def _period_in_range(observation: date, query: _DateQuery) -> bool:
    return not (
        (query.start_date is not None and observation < query.start_date)
        or (query.end_date is not None and observation > query.end_date)
    )


def _source_series_id(value: str) -> str:
    return value.strip().rsplit("/", maxsplit=1)[-1].strip().upper()


def _parse_ddp_csv(
    text: str,
    *,
    required_ids: set[str],
    model: str,
) -> tuple[tuple[str, ...], list[list[str]]]:
    """Read the official DDP metadata block and date-by-series table."""
    rows = csv.reader(io.StringIO(text, newline=""))
    header: tuple[str, ...] | None = None
    data_rows: list[list[str]] = []
    for row in rows:
        if not row:
            continue
        first = row[0].strip().casefold()
        if header is None:
            if first not in {"time period", "observation date"}:
                continue
            header = tuple(_source_series_id(item) for item in row[1:])
            if len(header) != len(set(header)):
                raise FederalReserveSourceError(f"{model}: duplicate DDP series identifiers")
            missing = required_ids - set(header)
            if missing:
                raise FederalReserveSourceError(
                    f"{model}: DDP response lacks required series {sorted(missing)}"
                )
            continue
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header) + 1:
            raise FederalReserveSourceError(f"{model}: DDP row width changed")
        data_rows.append(row)
    if header is None:
        raise FederalReserveSourceError(f"{model}: DDP response has no Time Period header")
    return header, data_rows


def _parse_tabular_csv(
    text: str,
    *,
    required_fields: set[str],
    model: str,
) -> tuple[tuple[str, ...], list[list[str]]]:
    """Find a named CSV header after optional Federal Reserve metadata rows."""
    reader = csv.reader(io.StringIO(text, newline=""))
    header: tuple[str, ...] | None = None
    data_rows: list[list[str]] = []
    for row in reader:
        if not row:
            continue
        first = row[0].strip().casefold()
        if header is None:
            if first not in {"date", "time period", "observation date"}:
                continue
            header = tuple(item.strip().upper() for item in row)
            if len(header) != len(set(header)):
                raise FederalReserveSourceError(f"{model}: duplicate source header names")
            missing = required_fields - set(header)
            if missing:
                raise FederalReserveSourceError(
                    f"{model}: source header lacks required fields {sorted(missing)}"
                )
            continue
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise FederalReserveSourceError(f"{model}: CSV row width changed")
        data_rows.append(row)
    if header is None:
        raise FederalReserveSourceError(f"{model}: source response has no data header")
    return header, data_rows


class MoneyMeasuresFetcher(_FederalReserveFetcher):
    """Retrieve H.6 money measures from a documented DDP series package."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_money_measures",
        period="monthly",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes=(
            "offline clean-room candidate; source-shape and output parity "
            "not independently verified"
        ),
    )
    canonical_model = "MoneyMeasures"
    query_type = MoneyMeasuresQuery

    def extract_data(self, params: MoneyMeasuresQuery, ctx: FetchContext) -> object:
        """Request the H.6 package and keep all date/seasonal choices explicit."""
        query_params = {
            "rel": "H6",
            "series": H6_PACKAGE_ID,
            "lastobs": "",
            "from": "" if params.start_date is None else params.start_date.isoformat(),
            "to": "" if params.end_date is None else params.end_date.isoformat(),
            "filetype": "csv",
            "label": "include",
            "layout": "seriescolumn",
        }
        return self._get(FED_DDP_URL, ctx, params=query_params)

    def transform_data(
        self, raw: object, params: MoneyMeasuresQuery
    ) -> list[FederalReserveMoneyMeasure]:
        """Map selected H.6 seasonal series into billions without rescaling."""
        text = self._text(raw, model=self.canonical_model)
        suffix = "_N" if params.adjusted is False else ""
        selected_ids = {
            public_name: series_id.replace(".M", f"{suffix}.M")
            for public_name, series_id in _MONEY_SERIES.items()
        }
        header, rows = _parse_ddp_csv(
            text,
            required_ids=set(selected_ids.values()),
            model=self.canonical_model,
        )
        index = {series_id: position + 1 for position, series_id in enumerate(header)}
        output: list[FederalReserveMoneyMeasure] = []
        for row in rows:
            observation = _date_value(row[0], monthly=True)
            if not _period_in_range(observation, params):
                continue
            parsed = {
                field_name: _number(row[index[series_id]], field_name=field_name)
                for field_name, series_id in selected_ids.items()
            }
            m1 = parsed.pop("m1")
            m2 = parsed.pop("m2")
            if m1 is None or m2 is None:
                raise FederalReserveSourceError("MoneyMeasures: required M1 or M2 value is missing")
            output.append(FederalReserveMoneyMeasure(month=observation, m1=m1, m2=m2, **parsed))
        return output


class TreasuryRatesFetcher(_FederalReserveFetcher):
    """Retrieve the explicitly sourced H.15 maturity and 4-week fields."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_treasury_rates",
        period="daily",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes="offline clean-room candidate; month_2 has no source series and remains null",
    )
    canonical_model = "TreasuryRates"
    query_type = TreasuryRatesQuery

    def extract_data(self, params: TreasuryRatesQuery, ctx: FetchContext) -> object:
        """Request the captured H.15 package with inclusive source date bounds."""
        query_params = {
            "rel": "H15",
            "series": H15_WEEK4_PACKAGE_ID,
            "lastobs": "",
            "from": "" if params.start_date is None else params.start_date.isoformat(),
            "to": "" if params.end_date is None else params.end_date.isoformat(),
            "filetype": "csv",
            "label": "include",
            "layout": "seriescolumn",
        }
        return self._get(FED_DDP_URL, ctx, params=query_params)

    def transform_data(
        self, raw: object, params: TreasuryRatesQuery
    ) -> list[FederalReserveTreasuryRate]:
        """Preserve H.15 annual-percent values and do not conflate rate bases."""
        text = self._text(raw, model=self.canonical_model)
        required_ids = set(_TREASURY_SOURCE_FIELDS.values())
        header, rows = _parse_ddp_csv(text, required_ids=required_ids, model=self.canonical_model)
        index = {series_id: position + 1 for position, series_id in enumerate(header)}
        output: list[FederalReserveTreasuryRate] = []
        for row in rows:
            observation = _date_value(row[0])
            if not _period_in_range(observation, params):
                continue
            values = {
                field_name: _number(
                    row[index[series_id]],
                    field_name=field_name,
                    null_markers=_H15_NULL_MARKERS,
                )
                for field_name, series_id in _TREASURY_SOURCE_FIELDS.items()
            }
            values["month_2"] = None
            output.append(FederalReserveTreasuryRate(date=observation, **values))
        return output


class SvenssonYieldCurveFetcher(_FederalReserveFetcher):
    """Retrieve the public FEDS nominal yield-curve CSV and filter locally."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_svensson_yield_curve",
        period="daily",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes="offline clean-room candidate; source numeric scale and output parity are unverified",
    )
    canonical_model = "SvenssonYieldCurve"
    query_type = SvenssonYieldCurveQuery

    def extract_data(self, params: SvenssonYieldCurveQuery, ctx: FetchContext) -> object:
        """Fetch the official daily CSV; its date and selector filters are local."""
        return self._get(FEDS_CURVE_URL, ctx)

    def transform_data(self, raw: object, params: SvenssonYieldCurveQuery) -> list[ContractModel]:
        """Preserve FEDS numeric values and project selected public columns."""
        text = self._text(raw, model=self.canonical_model)
        required = {"DATE", *_SVENSSON_SOURCE_FIELDS}
        header, rows = _parse_tabular_csv(
            text, required_fields=required, model=self.canonical_model
        )
        column_indexes = {name: index for index, name in enumerate(header)}
        selected = _selected_svensson_fields(params.series_type)
        output: list[ContractModel] = []
        for row in rows:
            observation = _date_value(row[column_indexes["DATE"]])
            if not _period_in_range(observation, params):
                continue
            values: dict[str, object] = {"date": observation}
            for source_name, public_name in _SVENSSON_SOURCE_TO_PUBLIC.items():
                raw_value = row[column_indexes[source_name]]
                values[public_name] = (
                    _number(raw_value, field_name=public_name) if public_name in selected else None
                )
            output.append(FederalReserveSvenssonYieldCurve.model_validate(values))
        return output


def _selected_svensson_fields(selector: str | None) -> frozenset[str]:
    tokens = ["all"] if selector is None else selector.split(",")
    if tokens == ["all"]:
        return frozenset(_SVENSSON_SOURCE_TO_PUBLIC.values())
    selected: set[str] = set()
    for token in tokens:
        selected.update(_SVENSSON_GROUPS.get(token, frozenset({token})))
    return frozenset(selected)


def _json_document(raw: object, *, model: str) -> Mapping[str, object]:
    text = _FederalReserveFetcher._text(raw, model=model)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FederalReserveSourceError(f"{model}: invalid NY Fed JSON") from exc
    if not isinstance(payload, Mapping):
        raise FederalReserveSourceError(f"{model}: NY Fed response must be an object")
    return payload


def _reference_rows(document: Mapping[str, object], *, model: str) -> Sequence[object]:
    rows = document.get("refRates")
    if not isinstance(rows, list):
        raise FederalReserveSourceError(f"{model}: NY Fed refRates must be an array")
    return rows


def _nyfed_params(query: _DateQuery) -> dict[str, str]:
    params: dict[str, str] = {}
    if query.start_date is not None:
        params["startDate"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["endDate"] = query.end_date.isoformat()
    return params


def _nyfed_number(
    row: Mapping[str, object], source_field: str, *, model: str, required: bool = False
) -> float | None:
    value = _number(row.get(source_field), field_name=source_field)
    if required and value is None:
        raise FederalReserveSourceError(
            f"{model}: required NY Fed field {source_field!r} is missing"
        )
    return value


def _nyfed_rate(row: Mapping[str, object], *, model: str) -> float:
    """Read the documented NY Fed rate aliases as finite JSON numbers."""
    fields = ("percentRate", "percent")
    present = [field for field in fields if field in row]
    if not present:
        raise FederalReserveSourceError(f"{model}: required NY Fed rate field is missing")

    values: list[float] = []
    for field in present:
        raw = row[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise FederalReserveSourceError(f"{model}: invalid numeric NY Fed field {field!r}")
        try:
            value = float(raw)
        except (OverflowError, ValueError) as exc:
            raise FederalReserveSourceError(
                f"{model}: invalid numeric NY Fed field {field!r}"
            ) from exc
        if not math.isfinite(value):
            raise FederalReserveSourceError(f"{model}: non-finite NY Fed field {field!r}")
        values.append(value)

    if len(values) == 2 and values[0] != values[1]:
        raise FederalReserveSourceError(f"{model}: NY Fed rate aliases conflict")
    return values[0]


def _nyfed_date(row: Mapping[str, object], *, model: str) -> date:
    value = row.get("effectiveDate")
    if not isinstance(value, str):
        raise FederalReserveSourceError(f"{model}: effectiveDate must be a string")
    return _date_value(value)


class OvernightBankFundingRateFetcher(_FederalReserveFetcher):
    """Retrieve NY Fed OBFR records with date bounds and all rate statistics."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_overnight_bank_funding_rate",
        period="daily",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes=(
            "offline candidate; captured response replayed; current source parity and "
            "output parity unverified"
        ),
    )
    canonical_model = "OvernightBankFundingRate"
    query_type = OvernightBankFundingRateQuery

    def extract_data(self, params: OvernightBankFundingRateQuery, ctx: FetchContext) -> object:
        """Use the NY Fed documented unsecured OBFR search route."""
        return self._get(
            NYFED_RATE_URL.format(rate_type="obfr"),
            ctx,
            params=_nyfed_params(params),
        )

    def transform_data(
        self, raw: object, params: OvernightBankFundingRateQuery
    ) -> list[FederalReserveOvernightBankFundingRate]:
        """Map NY Fed rate aliases and ``volumeInBillions`` fields."""
        document = _json_document(raw, model=self.canonical_model)
        output: list[FederalReserveOvernightBankFundingRate] = []
        for item in _reference_rows(document, model=self.canonical_model):
            if not isinstance(item, Mapping):
                raise FederalReserveSourceError("OvernightBankFundingRate: row must be an object")
            observation = _nyfed_date(item, model=self.canonical_model)
            if not _period_in_range(observation, params):
                continue
            revision = item.get("revisionIndicator")
            if revision is not None and not isinstance(revision, str):
                raise FederalReserveSourceError(
                    "OvernightBankFundingRate: invalid revisionIndicator"
                )
            output.append(
                FederalReserveOvernightBankFundingRate(
                    date=observation,
                    rate=_nyfed_rate(item, model=self.canonical_model),
                    percentile_1=_nyfed_number(
                        item, "percentPercentile1", model=self.canonical_model
                    ),
                    percentile_25=_nyfed_number(
                        item, "percentPercentile25", model=self.canonical_model
                    ),
                    percentile_75=_nyfed_number(
                        item, "percentPercentile75", model=self.canonical_model
                    ),
                    percentile_99=_nyfed_number(
                        item, "percentPercentile99", model=self.canonical_model
                    ),
                    volume=_nyfed_number(item, "volumeInBillions", model=self.canonical_model),
                    revision_indicator=revision,
                )
            )
        return output


class FederalFundsRateFetcher(_FederalReserveFetcher):
    """Retrieve NY Fed EFFR and documented current or historical statistics."""

    capability = Capability(
        asset_class="macro",
        domain="federal_reserve_federal_funds_rate",
        period="daily",
        market=MARKET,
        source=SOURCE,
        verified=False,
        notes="offline candidate; legacy capture replayed; independent output parity unverified",
    )
    canonical_model = "FederalFundsRate"
    query_type = FederalFundsRateQuery

    def extract_data(self, params: FederalFundsRateQuery, ctx: FetchContext) -> object:
        """Use the NY Fed documented unsecured EFFR search route."""
        return self._get(
            NYFED_RATE_URL.format(rate_type="effr"),
            ctx,
            params=_nyfed_params(params),
        )

    def transform_data(
        self, raw: object, params: FederalFundsRateQuery
    ) -> list[FederalReserveFederalFundsRate]:
        """Normalize EFFR and map the documented pre-2016 intraday statistics."""
        document = _json_document(raw, model=self.canonical_model)
        output: list[FederalReserveFederalFundsRate] = []
        for item in _reference_rows(document, model=self.canonical_model):
            if not isinstance(item, Mapping):
                raise FederalReserveSourceError("FederalFundsRate: row must be an object")
            observation = _nyfed_date(item, model=self.canonical_model)
            if not _period_in_range(observation, params):
                continue
            revision = item.get("revisionIndicator")
            if revision is not None and not isinstance(revision, str):
                raise FederalReserveSourceError("FederalFundsRate: invalid revisionIndicator")
            output.append(
                FederalReserveFederalFundsRate(
                    date=observation,
                    rate=_nyfed_rate(item, model=self.canonical_model),
                    target_range_upper=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "targetRateTo", model=self.canonical_model)
                    ),
                    target_range_lower=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "targetRateFrom", model=self.canonical_model)
                    ),
                    percentile_1=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "percentPercentile1", model=self.canonical_model)
                    ),
                    percentile_25=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "percentPercentile25", model=self.canonical_model)
                    ),
                    percentile_75=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "percentPercentile75", model=self.canonical_model)
                    ),
                    percentile_99=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "percentPercentile99", model=self.canonical_model)
                    ),
                    volume=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "volumeInBillions", model=self.canonical_model)
                    ),
                    intraday_low=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "intraDayLow", model=self.canonical_model)
                    ),
                    intraday_high=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "intraDayHigh", model=self.canonical_model)
                    ),
                    standard_deviation=(
                        None
                        if params.effr_only
                        else _nyfed_number(item, "stdDeviation", model=self.canonical_model)
                    ),
                    revision_indicator=revision,
                )
            )
        return output


__all__ = [
    "FederalFundsRateFetcher",
    "FederalReserveQueryError",
    "FederalReserveSourceError",
    "MoneyMeasuresFetcher",
    "OvernightBankFundingRateFetcher",
    "SvenssonYieldCurveFetcher",
    "TreasuryRatesFetcher",
]
