"""Clean-room NY Fed Markets API fetchers for three Federal Reserve models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, ClassVar, Literal
from urllib.parse import quote

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.federal_reserve_clean_nyfed import (
    SOFR,
    PrimaryDealerFails,
    PrimaryDealerPositioning,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError, ProviderEngineQueryError
from opendata.data.request_budget import RequestBudgetError

SOURCE = "federal_reserve"
NYFED_API_BASE = "https://markets.newyorkfed.org/api"
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _validate_source_number(value: object) -> object:
    """Accept only finite JSON numbers, never booleans or numeric strings."""
    if type(value) is int:
        numeric_value: int | float = value
    elif type(value) is float:
        numeric_value = value
    else:
        raise ValueError("source observation must be a JSON number")
    try:
        finite = math.isfinite(numeric_value)
    except (OverflowError, TypeError):
        finite = False
    if not finite:
        raise ValueError("source observation must be finite")
    return value


_SourceFloat = Annotated[float, BeforeValidator(_validate_source_number)]
_SOURCE_FLOAT_ADAPTER = TypeAdapter(_SourceFloat)


class NyfedProviderError(ProviderEngineError):
    """Sanitized NY Fed failures with stable codes and no source payloads."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        partial: bool = False,
        requests_made: int = 0,
    ) -> None:
        """Create a sanitized provider error."""
        super().__init__(code, status=status)
        self.partial = partial
        self.requests_made = requests_made


class _NyfedQuery(QueryParams):
    """Common bounded date window used by NY Fed model queries."""

    @model_validator(mode="after")
    def validate_date_window(self) -> _NyfedQuery:
        """Reject reversed inclusive date windows before transport."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must be on or after start_date")
        return self


class SOFRQuery(_NyfedQuery):
    """Public V4 Federal Reserve SOFR query parameters."""


class PrimaryDealerFailsQuery(_NyfedQuery):
    """Public V4 dealer-fails query parameters."""

    asset_class: Literal["all", "treasuries", "tips", "agency", "mbs", "corporate"] = "all"
    unit: Literal["value", "percent"] = "value"


class PrimaryDealerPositioningQuery(_NyfedQuery):
    """Public V4 dealer-positioning query parameters."""

    category: Literal[
        "treasuries",
        "bills",
        "coupons",
        "notes",
        "tips",
        "mbs",
        "cmbs",
        "municipal",
        "corporate",
        "commercial_paper",
        "corporate_ig",
        "corporate_junk",
        "abs",
    ] = "treasuries"


class _SourceModel(BaseModel):
    """Typed projection of documented JSON fields, ignoring additive source fields."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class _SofrApiRecord(_SourceModel):
    """NY Fed SOFR record across the documented and captured rate-key names."""

    rate_type: Literal["SOFR"] = Field(alias="type")
    effective_date: date = Field(alias="effectiveDate")
    percent: _SourceFloat = Field(validation_alias=AliasChoices("percent", "percentRate"))
    percentile_1: _SourceFloat | None = Field(default=None, alias="percentPercentile1")
    percentile_25: _SourceFloat | None = Field(default=None, alias="percentPercentile25")
    percentile_75: _SourceFloat | None = Field(default=None, alias="percentPercentile75")
    percentile_99: _SourceFloat | None = Field(default=None, alias="percentPercentile99")
    volume_billions: _SourceFloat | None = Field(default=None, alias="volumeInBillions")

    @model_validator(mode="before")
    @classmethod
    def validate_rate_aliases(cls, value: object) -> object:
        """Require duplicate documented/live rate aliases to be valid and equal."""
        if not isinstance(value, dict) or "percent" not in value or "percentRate" not in value:
            return value
        try:
            documented_rate = _SOURCE_FLOAT_ADAPTER.validate_python(value["percent"])
            captured_rate = _SOURCE_FLOAT_ADAPTER.validate_python(value["percentRate"])
        except ValidationError as exc:
            raise ValueError("SOFR rate aliases must be finite JSON numbers") from exc
        if documented_rate != captured_rate:
            raise ValueError("SOFR rate aliases disagree")
        return value


class _SofraiApiRecord(_SourceModel):
    """Documented NY Fed SOFR averages and index record."""

    rate_type: Literal["SOFRAI"] = Field(alias="type")
    effective_date: date = Field(alias="effectiveDate")
    average_30d: _SourceFloat | None = Field(default=None, alias="average30day")
    average_90d: _SourceFloat | None = Field(default=None, alias="average90day")
    average_180d: _SourceFloat | None = Field(default=None, alias="average180day")
    index: _SourceFloat | None = None


class _ReferenceRateEnvelope(_SourceModel):
    """Documented secured-rate JSON envelope."""

    ref_rates: list[_SofrApiRecord | _SofraiApiRecord] = Field(alias="refRates")


class _DealerSeriesRecord(_SourceModel):
    """Documented NY Fed Primary Dealer series data record."""

    as_of_date: date = Field(alias="asofdate")
    key_id: str = Field(alias="keyid")
    value: str


class _DealerSeriesEnvelope(_SourceModel):
    """Documented NY Fed Primary Dealer JSON envelope."""

    series: list[_DealerSeriesRecord] = Field(validation_alias=AliasPath("pd", "timeseries"))


@dataclass(frozen=True, slots=True)
class _SofrSnapshot:
    """Typed SOFR and SOFRAI records captured from their source endpoints."""

    rate_rows: tuple[_SofrApiRecord, ...]
    average_rows: tuple[_SofraiApiRecord, ...]


# The IDs and descriptions are copied from the NY Fed's public SBN2024
# `/api/pd/list/timeseries.json` metadata and cross-checked against Table III.
_POSITIONING_DESCRIPTIONS: dict[str, str] = {
    "PDPOSMBS-TOT": (
        "Total - Mortgage-backed Securities: Federal Agency and GSE MBS: Long - M"
        "ortgage-backed Securities: Federal Agency and GSE MBS: Short"
    ),
    "PDPOSABS-TOT": "Total - ASSET-BACKED SECURITIES - LONG. - ASSET-BACKED SECURITIES - SHORT.",
    "PDPOSCS-TOT": (
        "Total - CORPORATE SECURITIES DEALER POSITION - LONG. - CORPORATE SECURIT"
        "IES: DEALER POSITION - SHORT."
    ),
    "PDPOSFGS-TOT": (
        "Total - FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) - DEALER POSIT"
        "ION - LONG - FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) - : DEALE"
        "R POSITION - SHORT"
    ),
    "PDPOSSMGO-TOT": (
        "Total - STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS : DEALER POSITION - L"
        "ONG. - STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS : DEALER POSITION - SH"
        "ORT."
    ),
    "PDPOSGST-TOT": (
        "Total - U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED"
        " SECURITIES (TIPS)) : DEALER POSITION - LONG. - U.S. TREASURY SECURITIES"
        " (EXCLUDING TREASURY INFLATION-PROTECTED SECURITIES (TIPS)) : DEALER POS"
        "ITION - SHORT."
    ),
    "PDPOSMBSFGS-TBA": (
        " Mortgage-backed Securities: Federal Agency and GSE MBS: TBAs - Outright"
        " Positions, Long - Mortgage-backed Securities: Federal Agency and GSE MB"
        "S: TBAs - Outright Positions, Short"
    ),
    "PDPOSCSBND-L13": (
        "CORPORATE SECURITIES: INVESTMENT GRADE BONDS, NOTES AND DEBENTURES DUE I"
        "N LESS THAN OR EQUAL TO 13 MONTHS: DEALER POSITION - LONG. - CORPORATE S"
        "ECURITIES: INVESTMENT GRADE BONDS, NOTES AND DEBENTURES DUE IN LESS THAN"
        " OR EQUAL TO 13 MONTHS: DEALER POSITION - SHORT."
    ),
    "PDPOSABS-ALB": (
        "ASSET-BACKED SECURITIES: AUTOMOBILE LOAN-BACKED SECURITIES: DEALER POSIT"
        "ION - LONG. - ASSET-BACKED SECURITIES: AUTOMOBILE LOAN-BACKED SECURITIES"
        ": DEALER POSITION - SHORT."
    ),
    "PDPOSABS-CCB": (
        "ASSET-BACKED SECURITIES: CREDIT CARD-BACKED SECURITIES: DEALER POSITION "
        "- LONG. - ASSET-BACKED SECURITIES: CREDIT CARD-BACKED SECURITIES: DEALER"
        " POSITION - SHORT."
    ),
    "PDPOSABS-OAB": (
        "ASSET-BACKED SECURITIES: OTHER ASSET-BACKED SECURITIES: DEALER POSITION "
        "- LONG. - ASSET-BACKED SECURITIES: OTHER ASSET-BACKED SECURITIES: DEALER"
        " POSITION - SHORT."
    ),
    "PDPOSABS-SLB": (
        "ASSET-BACKED SECURITIES: STUDENT LOAN-BACKED SECURITIES: DEALER POSITION"
        " - LONG. - ASSET-BACKED SECURITIES: STUDENT LOAN-BACKED SECURITIES IN SH"
        "ORT POSITION."
    ),
    "PDPOSCSBND-BELG10": (
        "CORPORATE SECURITIES BELOW INVESTMENT GRADE BONDS NOTES AND DEBENTURES D"
        "UE IN MORE THAN 10 YEARS: DEALER POSITION - LONG. - CORPORATE SECURITIES"
        " BELOW INVESTMENT GRADE BONDS NOTES AND DEBENTURES DUE IN MORE THAN 10 Y"
        "EARS: DEALER POSITION -SHORT."
    ),
    "PDPOSCSBND-BELG13": (
        "CORPORATE SECURITIES BELOW INVESTMENT GRADE BONDS NOTES AND DEBENTURES D"
        "UE IN MORE THAN 13 MONTHS BUT LESS THAN OR EQUAL TO 5 YEARS: DEALER POSI"
        "TION -LONG. - CORPORATE SECURITIES BELOW INVESTMENT GRADE BONDS NOTES AN"
        "D DEBENTURES DUE IN MORE THAN 13 MONTHS BUT LESS THAN OR EQUAL TO 5 YEAR"
        "S: DEALER POSITION -SHORT."
    ),
    "PDPOSCSBND-BELG5L10": (
        "CORPORATE SECURITIES BELOW INVESTMENT GRADE BONDS NOTES AND DEBENTURES D"
        "UE IN MORE THAN 5 YEARS BUT LESS THAN OR EQUAL TO 10 YEARS: DEALER POSIT"
        "ION - LONG. - CORPORATE SECURITIES BELOW INVESTMENT GRADE BONDS NOTES AN"
        "D DEBENTURES DUE IN MORE THAN 5 YEARS BUT LESS THAN OR EQUAL TO 10 YEARS"
        ": DEALER POSITION -SHORT."
    ),
    "PDPOSCSBND-BELL13": (
        "CORPORATE SECURITIES BELOW INVESTMENT GRADE DUE IN LESS THAN OR EQUAL TO"
        " 13 MONTHS: DEALER POSITION - LONG. - CORPORATE SECURITIES BELOW INVESTM"
        "ENT GRADE DUE IN LESS THAN OR EQUAL TO 13 MONTHS: DEALER POSITION - SHOR"
        "T."
    ),
    "PDPOSCSBND-G10": (
        "CORPORATE SECURITIES INVESTMENT GRADE BONDS NOTES AND DEBENTURES DUE IN "
        "MORE THAN 10 YEARS: DEALER POSITION - LONG. - CORPORATE SECURITIES INVES"
        "TMENT GRADE BONDS NOTES AND DEBENTURES DUE IN MORE THAN 10 YEARS: DEALER"
        " POSITION - SHORT."
    ),
    "PDPOSCSBND-G5L10": (
        "CORPORATE SECURITIES INVESTMENT GRADE BONDS NOTES AND DEBENTURES DUE IN "
        "MORE THAN 5 YEARS BUT LESS THAN OR EQUAL TO 10 YEARS: DEALER POSITION - "
        "LONG. - CORPORATE SECURITIES INVESTMENT GRADE BONDS NOTES AND DEBENTURES"
        " DUE IN MORE THAN 5 YEARS BUT LESS THAN OR EQUAL TO 10 YEARS: DEALER POS"
        "ITION -SHORT."
    ),
    "PDPOSCSCP": (
        "CORPORATE SECURITIES: COMMERCIAL PAPER: DEALER POSITION - LONG. - CORPOR"
        "ATE SECURITIES: COMMERCIAL PAPER: DEALER POSITION - SHORT."
    ),
    "PDPOSCSBND-G13": (
        "CORPORATE SECURITIES: INVESTMENT GRADE BONDS NOTES AND DEBENTURES DUE IN"
        " MORE THAN 13 MONTHS BUT LESS THAN OR EQUAL TO 5 YEARS: DEALER POSITION "
        "- LONG. - CORPORATE SECURITIES INVESTMENT GRADE BONDS NOTES AND DEBENTUR"
        "ES DUE IN MORE THAN 13 MONTHS BUT LESS THAN OR EQUAL TO 5 YEARS: DEALER "
        "POSITION - SHORT."
    ),
    "PDPOSFGS-C": (
        "FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) - COUPONS: DEALER POSI"
        "TION - LONG - FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) - COUPON"
        "S : DEALER POSITION - SHORT"
    ),
    "PDPOSFGS-DN": (
        "FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) - DISCOUNT NOTES: DEAL"
        "ER POSITION - LONG - FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) -"
        " DISCOUNT NOTES: DEALER POSITION - SHORT"
    ),
    "PDPOSGS-BFRN": (
        "FLOATING RATE NOTES: DEALER POSITION - LONG. - FLOATING RATE NOTES: DEAL"
        "ER POSITION - SHORT."
    ),
    "PDPOSMBSFGS-OR": (
        "MORTGAGE-BACKED SECURITIES - ALL OTHER FEDERAL AGENCY AND GSE RESIDENTIA"
        "L MBS: DEALER POSITION - LONG - MORTGAGE-BACKED SECURITIES - ALL OTHER F"
        "EDERAL AGENCY AND GSE RESIDENTIAL MBS: DEALER POSITION - SHORT"
    ),
    "PDPOSMBSFGS-C": (
        "MORTGAGE-BACKED SECURITIES - FEDERAL AGENCY AND GSE CMBS: DEALER POSITIO"
        "N - LONG - MORTGAGE-BACKED SECURITIES - FEDERAL AGENCY AND GSE CMBS: DEA"
        "LER POSITION - SHORT"
    ),
    "PDPOSMBSNA-O": (
        "MORTGAGE-BACKED SECURITIES - NON-AGENCY OTHER CMBS : DEALER POSITION - L"
        "ONG - MORTGAGE-BACKED SECURITIES - NON-AGENCY OTHER CMBS : DEALER POSITI"
        "ON - SHORT"
    ),
    "PDPOSMBSNA-R": (
        "MORTGAGE-BACKED SECURITIES - NON-AGENCY RESIDENTIAL MBS : DEALER POSITIO"
        "N - LONG - MORTGAGE-BACKED SECURITIES - NON-AGENCY RESIDENTIAL MBS : DEA"
        "LER POSITION - SHORT"
    ),
    "PDPOSMBSFGS-ST": (
        "Mortgage-backed Securities: Federal Agency and GSE MBS: Specified pools "
        "- Outright Positions, Long - Mortgage-backed Securities: Federal Agency "
        "and GSE MBS: Specified pools - Outright Positions, Short"
    ),
    "PDPOSSMGO-L13": (
        "STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS DUE IN LESS THAN OR EQUAL TO "
        "13 MONTHS: DEALER POSITION - LONG. - STATE AND MUNICIPAL GOVERNMENT OBLI"
        "GATIONS DUE IN LESS THAN OR EQUAL TO 13 MONTHS: DEALER POSITION - SHORT."
    ),
    "PDPOSSMGO-G10": (
        "STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS DUE IN MORE THAN 10 YEARS: DE"
        "ALER POSITION - LONG. - STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS DUE I"
        "N MORE THAN 10 YEARS: DEALER POSITION - SHORT."
    ),
    "PDPOSSMGO-G13": (
        "STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS DUE IN MORE THAN 13 MONTHS BU"
        "T LESS THAN OR EQUAL TO 5 YEARS: DEALER POSITION - LONG. - STATE AND MUN"
        "ICIPAL GOVERNMENT OBLIGATIONS DUE IN MORE THAN 13 MONTHS BUT LESS THAN O"
        "R EQUAL TO 5 YEARS: DEALER POSITION - SHORT."
    ),
    "PDPOSSMGO-G5L10": (
        "STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS DUE IN MORE THAN 5 YEARS BUT "
        "LESS THAN OR EQUAL TO 10 YEARS: DEALER POSITION - LONG. - STATE AND MUNI"
        "CIPAL GOVERNMENT OBLIGATIONS DUE IN MORE THAN 5 YEARS BUT LESS THAN OR E"
        "QUAL TO 10 YEARS: DEALER POSITION - SHORT."
    ),
    "PDPOSSMGO-VRD": (
        "STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS VARIABLE RATE DEMAND NOTES: D"
        "EALER POSITION - LONG. - STATE AND MUNICIPAL GOVERNMENT OBLIGATIONS VARI"
        "ABLE RATE DEMAND NOTES: DEALER POSITION - SHORT."
    ),
    "PDPOSTIPS-L2": (
        "U.S. TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN LESS THAN OR "
        "EQUAL TO 2 YEARS : DEALER POSITION - LONG. - U.S. TREASURY INFLATION-PRO"
        "TECTED SECURITIES (TIPS) DUE IN LESS THAN OR EQUAL TO 2 YEARS : DEALER P"
        "OSITION - SHORT."
    ),
    "PDPOSTIPS-G11": (
        "U.S. TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN MORE THAN 11 "
        "YEARS : DEALER POSITION - LONG. - U.S. TREASURY INFLATION-PROTECTED SECU"
        "RITIES (TIPS) DUE IN MORE THAN 11 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSTIPS-G2": (
        "U.S. TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN MORE THAN 2 Y"
        "EARS BUT LESS THAN OR EQUAL TO 6 YEARS : DEALER POSITION - LONG. - U.S. "
        "TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN MORE THAN 2 YEARS "
        "BUT LESS THAN OR EQUAL TO 6 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSTIPS-G6L11": (
        "U.S. TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN MORE THAN 6 Y"
        "EARS BUT LESS THAN OR EQUAL TO 11 YEARS : DEALER POSITION - LONG. - U.S."
        " TREASURY INFLATION-PROTECTED SECURITIES (TIPS) DUE IN MORE THAN 6 YEARS"
        " BUT LESS THAN OR EQUAL TO 11 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSGSC-G11L21": (
        "U.S. TREASURY SECURITIES (EXCLUDING TIPS): COUPONS DUE IN MORE THAN 11 Y"
        "EARS BUT LESS THAN OR EQUAL TO 21 YEARS - OUTRIGHT POSITIONS LONG - U.S."
        " TREASURY SECURITIES (EXCLUDING TIPS): COUPONS DUE IN MORE THAN 11 YEARS"
        " BUT LESS THAN OR EQUAL TO 21 YEARS - OUTRIGHT POSITIONS SHORT"
    ),
    "PDPOSGSC-G21": (
        "U.S. TREASURY SECURITIES (EXCLUDING TIPS): COUPONS DUE IN MORE THAN 21 Y"
        "EARS - OUTRIGHT POSITIONS LONG - U.S. TREASURY SECURITIES (EXCLUDING TIP"
        "S): COUPONS DUE IN MORE THAN 21 YEARS - OUTRIGHT POSITIONS SHORT"
    ),
    "PDPOSGS-B": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) BILLS: DEALER POSITION - LONG. - U.S. TREASURY SECURITIES (E"
        "XCLUDING TREASURY INFLATION-PROTECTED SECURITIES (TIPS)) BILLS : DEALER "
        "POSITION - SHORT."
    ),
    "PDPOSGSC-L2": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) COUPONS DUE IN LESS THAN OR EQUAL TO 2 YEARS : DEALER POSITI"
        "ON - LONG. - U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROT"
        "ECTED SECURITIES (TIPS)) COUPONS DUE IN LESS THAN OR EQUAL TO 2 YEARS : "
        "DEALER POSITION - SHORT."
    ),
    "PDPOSGSC-G2L3": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) COUPONS DUE IN MORE THAN 2 YEARS BUT LESS THAN OR EQUAL TO 3"
        " YEARS : DEALER POSITION - LONG. - U.S. TREASURY SECURITIES (EXCLUDING T"
        "REASURY INFLATION-PROTECTED SECURITIES (TIPS)) COUPONS DUE IN MORE THAN "
        "2 YEARS BUT LESS THAN OR EQUAL TO 3 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSGSC-G3L6": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) COUPONS DUE IN MORE THAN 3 YEARS BUT LESS THAN OR EQUAL TO 6"
        " YEARS : DEALER POSITION - LONG. - U.S. TREASURY SECURITIES (EXCLUDING T"
        "REASURY INFLATION-PROTECTED SECURITIES (TIPS)) COUPONS DUE IN MORE THAN "
        "3 YEARS BUT LESS THAN OR EQUAL TO 6 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSGSC-G6L7": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) COUPONS DUE IN MORE THAN 6 YEARS BUT LESS THAN OR EQUAL TO 7"
        " YEARS : DEALER POSITION - LONG. - U.S. TREASURY SECURITIES (EXCLUDING T"
        "REASURY INFLATION-PROTECTED SECURITIES (TIPS)) COUPONS DUE IN MORE THAN "
        "6 YEARS BUT LESS THAN OR EQUAL TO 7 YEARS : DEALER POSITION - SHORT."
    ),
    "PDPOSGSC-G7L11": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS)) COUPONS DUE IN MORE THAN 7 YEARS BUT LESS THAN OR EQUAL TO 1"
        "1 YEARS : DEALER POSITION - LONG. - U.S. TREASURY SECURITIES (EXCLUDING "
        "TREASURY INFLATION-PROTECTED SECURITIES (TIPS)) COUPONS DUE IN MORE THAN"
        " 7 YEARS BUT LESS THAN OR EQUAL TO 11 YEARS : DEALER POSITION - SHORT."
    ),
}

_POSITIONING_KEYIDS_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "treasuries": ("PDPOSGST-TOT",),
    "bills": ("PDPOSGS-B",),
    "coupons": (
        "PDPOSGSC-L2",
        "PDPOSGSC-G2L3",
        "PDPOSGSC-G3L6",
        "PDPOSGSC-G6L7",
        "PDPOSGSC-G7L11",
        "PDPOSGSC-G11L21",
        "PDPOSGSC-G21",
    ),
    "notes": ("PDPOSGS-BFRN",),
    "tips": ("PDPOSTIPS-L2", "PDPOSTIPS-G2", "PDPOSTIPS-G6L11", "PDPOSTIPS-G11"),
    "mbs": (
        "PDPOSMBS-TOT",
        "PDPOSMBSFGS-TBA",
        "PDPOSMBSFGS-ST",
        "PDPOSMBSFGS-OR",
        "PDPOSMBSFGS-C",
        "PDPOSMBSNA-R",
        "PDPOSMBSNA-O",
    ),
    "cmbs": ("PDPOSMBSFGS-C", "PDPOSMBSNA-O"),
    "municipal": (
        "PDPOSSMGO-TOT",
        "PDPOSSMGO-L13",
        "PDPOSSMGO-G13",
        "PDPOSSMGO-G5L10",
        "PDPOSSMGO-G10",
        "PDPOSSMGO-VRD",
    ),
    "corporate": (
        "PDPOSCS-TOT",
        "PDPOSCSBND-L13",
        "PDPOSCSBND-G13",
        "PDPOSCSBND-G5L10",
        "PDPOSCSBND-G10",
        "PDPOSCSBND-BELL13",
        "PDPOSCSBND-BELG13",
        "PDPOSCSBND-BELG5L10",
        "PDPOSCSBND-BELG10",
        "PDPOSCSCP",
    ),
    "commercial_paper": ("PDPOSCSCP",),
    "corporate_ig": (
        "PDPOSCSBND-L13",
        "PDPOSCSBND-G13",
        "PDPOSCSBND-G5L10",
        "PDPOSCSBND-G10",
    ),
    "corporate_junk": (
        "PDPOSCSBND-BELL13",
        "PDPOSCSBND-BELG13",
        "PDPOSCSBND-BELG5L10",
        "PDPOSCSBND-BELG10",
    ),
    "abs": (
        "PDPOSABS-TOT",
        "PDPOSABS-ALB",
        "PDPOSABS-CCB",
        "PDPOSABS-SLB",
        "PDPOSABS-OAB",
    ),
}

_FAIL_DESCRIPTIONS: dict[str, str] = {
    "PDFTR-USTET": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS) : DEALER FINANCING FAILS TO RECEIVE"
    ),
    "PDFTR-UST": (
        "TREASURY INFLATION-PROTECTED SECURITIES (TIPS) : DEALER FINANCING FAILS TO RECEIVE"
    ),
    "PDFTR-FGEM": (
        "FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) : DEALER FINANCING FAILS TO RECEIVE"
    ),
    "PDFTR-FGM": "FEDERAL AGENCY AND GSE MBS : DEALER FINANCING FAILS TO RECEIVE",
    "PDFTR-OM": "OTHER MBS : DEALER FINANCING FAILS TO RECEIVE",
    "PDFTR-CS": "CORPORATE SECURITIES : DEALER FINANCING FAILS TO RECEIVE",
    "PDFTD-USTET": (
        "U.S. TREASURY SECURITIES (EXCLUDING TREASURY INFLATION-PROTECTED SECURIT"
        "IES (TIPS) : DEALER FINANCING FAILS TO DELIVER"
    ),
    "PDFTD-UST": (
        "TREASURY INFLATION-PROTECTED SECURITIES (TIPS) : DEALER FINANCING FAILS TO DELIVER"
    ),
    "PDFTD-FGEM": (
        "FEDERAL AGENCY AND GSE SECURITIES (EXCLUDING MBS) : DEALER FINANCING FAILS TO DELIVER"
    ),
    "PDFTD-FGM": "FEDERAL AGENCY AND GSE MBS : DEALER FINANCING FAILS TO DELIVER",
    "PDFTD-OM": "OTHER MBS : DEALER FINANCING FAILS TO DELIVER",
    "PDFTD-CS": "CORPORATE SECURITIES : DEALER FINANCING FAILS TO DELIVER",
}

_FAIL_KEYIDS_BY_ASSET_CLASS: dict[str, tuple[str, ...]] = {
    "all": tuple(_FAIL_DESCRIPTIONS),
    "treasuries": ("PDFTR-USTET", "PDFTD-USTET"),
    "tips": ("PDFTR-UST", "PDFTD-UST"),
    "agency": ("PDFTR-FGEM", "PDFTD-FGEM"),
    "mbs": ("PDFTR-FGM", "PDFTD-FGM", "PDFTR-OM", "PDFTD-OM"),
    "corporate": ("PDFTR-CS", "PDFTD-CS"),
}


def _request_json(
    path: str,
    *,
    params: dict[str, str] | None,
    context: FetchContext,
    partial: bool,
    requests_made: int,
) -> object:
    """Issue one governed GET and return an untrusted JSON document."""
    url = f"{NYFED_API_BASE}/{path.lstrip('/')}"
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=(_DEFAULT_TIMEOUT_SECONDS if context.timeout is None else context.timeout),
            source=SOURCE,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise NyfedProviderError(
            _http_error_code(exc.status),
            status=exc.status,
            partial=partial,
            requests_made=requests_made + exc.attempts,
        ) from None
    except Exception:
        raise NyfedProviderError(
            "NYFED_TRANSPORT_ERROR",
            partial=partial,
            requests_made=requests_made,
        ) from None

    status = getattr(response, "status_code", None)
    if type(status) is not int or not 200 <= status < 300:
        raise NyfedProviderError(
            _http_error_code(status if type(status) is int else None),
            status=status if type(status) is int else None,
            partial=partial,
            requests_made=requests_made + 1,
        ) from None
    try:
        return response.json()
    except Exception:
        raise NyfedProviderError(
            "NYFED_BAD_RESPONSE",
            status=status,
            partial=partial,
            requests_made=requests_made + 1,
        ) from None


def _http_error_code(status: int | None) -> str:
    """Map source and transport statuses without exposing response bodies."""
    if status in (401, 403):
        return "NYFED_FORBIDDEN"
    if status == 429:
        return "NYFED_RATE_LIMITED"
    if status is not None and status >= 500:
        return "NYFED_UPSTREAM_ERROR"
    if status is not None:
        return "NYFED_HTTP_ERROR"
    return "NYFED_TRANSPORT_ERROR"


def _date_params(query: _NyfedQuery) -> dict[str, str]:
    """Encode the documented inclusive NY Fed date names only when supplied."""
    params: dict[str, str] = {}
    if query.start_date is not None:
        params["startDate"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["endDate"] = query.end_date.isoformat()
    return params


def _in_date_window(day: date, query: _NyfedQuery) -> bool:
    """Apply inclusive date filtering for source endpoints without date params."""
    return not (
        (query.start_date is not None and day < query.start_date)
        or (query.end_date is not None and day > query.end_date)
    )


def _parse_reference_rates(
    document: object,
    *,
    expected_type: Literal["SOFR", "SOFRAI"],
    status: int | None,
    partial: bool = False,
    requests_made: int = 0,
) -> tuple[tuple[_SofrApiRecord, ...], tuple[_SofraiApiRecord, ...]]:
    """Validate a secured-rate response against NY Fed's typed JSON schema."""
    try:
        envelope = _ReferenceRateEnvelope.model_validate(document)
    except ValidationError:
        raise NyfedProviderError(
            "NYFED_BAD_SHAPE",
            status=status,
            partial=partial,
            requests_made=requests_made,
        ) from None
    rates = tuple(row for row in envelope.ref_rates if isinstance(row, _SofrApiRecord))
    averages = tuple(row for row in envelope.ref_rates if isinstance(row, _SofraiApiRecord))
    if (expected_type == "SOFR" and averages) or (expected_type == "SOFRAI" and rates):
        raise NyfedProviderError(
            "NYFED_BAD_SHAPE",
            status=status,
            partial=partial,
            requests_made=requests_made,
        ) from None
    return rates, averages


def _merge_optional_values(
    current: dict[str, float | None],
    incoming: dict[str, float | None],
) -> None:
    """Merge nullable repeated source fields, rejecting only contradictions."""
    for field, value in incoming.items():
        existing = current[field]
        if existing is not None and value is not None and existing != value:
            raise NyfedProviderError("NYFED_DUPLICATE_DATE_CONFLICT")
        if existing is None and value is not None:
            current[field] = value


class SOFRFetcher(Fetcher[SOFRQuery, _SofrSnapshot]):
    """Fetch daily SOFR and left-join the NY Fed SOFRAI enrichment by date."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SOFR"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_sofr",
        period="daily",
        market="us",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SOFRQuery:
        """Validate source identity and optional inclusive date bounds."""
        return SOFRQuery.model_validate(kwargs)

    def extract_data(self, params: SOFRQuery, ctx: FetchContext) -> _SofrSnapshot:
        """Fetch the separately documented SOFR and SOFRAI source records."""
        rate_document = _request_json(
            "rates/secured/sofr/search.json",
            params=_date_params(params),
            context=ctx,
            partial=False,
            requests_made=0,
        )
        rate_rows, rate_averages = _parse_reference_rates(
            rate_document,
            expected_type="SOFR",
            status=200,
        )
        average_document = _request_json(
            "rates/secured/sofrai/search.json",
            params=_date_params(params),
            context=ctx,
            partial=True,
            requests_made=1,
        )
        extra_rates, average_rows = _parse_reference_rates(
            average_document,
            expected_type="SOFRAI",
            status=200,
            partial=True,
            requests_made=1,
        )
        return _SofrSnapshot(
            rate_rows=rate_rows + extra_rates,
            average_rows=rate_averages + average_rows,
        )

    def transform_data(self, raw: _SofrSnapshot, params: SOFRQuery) -> FetchResult:
        """Copy the published percent cells unchanged and keep rates lacking averages."""
        if not isinstance(raw, _SofrSnapshot):
            raise NyfedProviderError("NYFED_BAD_SHAPE")

        rate_values: dict[date, tuple[float, dict[str, float | None]]] = {}
        for rate_row in raw.rate_rows:
            if not _in_date_window(rate_row.effective_date, params):
                continue
            fields = {
                "percentile_1": rate_row.percentile_1,
                "percentile_25": rate_row.percentile_25,
                "percentile_75": rate_row.percentile_75,
                "percentile_99": rate_row.percentile_99,
                "volume": rate_row.volume_billions,
            }
            rate_current = rate_values.get(rate_row.effective_date)
            if rate_current is None:
                rate_values[rate_row.effective_date] = (rate_row.percent, fields)
                continue
            if rate_current[0] != rate_row.percent:
                raise NyfedProviderError("NYFED_DUPLICATE_DATE_CONFLICT")
            _merge_optional_values(rate_current[1], fields)

        average_values: dict[date, dict[str, float | None]] = {}
        for average_row in raw.average_rows:
            if not _in_date_window(average_row.effective_date, params):
                continue
            fields = {
                "average_30d": average_row.average_30d,
                "average_90d": average_row.average_90d,
                "average_180d": average_row.average_180d,
                "index": average_row.index,
            }
            average_current = average_values.get(average_row.effective_date)
            if average_current is None:
                average_values[average_row.effective_date] = fields
            else:
                _merge_optional_values(average_current, fields)

        output: list[SOFR] = []
        for day in sorted(rate_values):
            rate, values = rate_values[day]
            enrichment = average_values.get(day, {})
            output.append(
                SOFR(
                    date=day,
                    rate=rate,
                    percentile_1=values["percentile_1"],
                    percentile_25=values["percentile_25"],
                    percentile_75=values["percentile_75"],
                    percentile_99=values["percentile_99"],
                    volume=values["volume"],
                    average_30d=enrichment.get("average_30d"),
                    average_90d=enrichment.get("average_90d"),
                    average_180d=enrichment.get("average_180d"),
                    index=enrichment.get("index"),
                )
            )
        return output


class PrimaryDealerFailsFetcher(Fetcher[PrimaryDealerFailsQuery, tuple[_DealerSeriesRecord, ...]]):
    """Fetch official dealer-financing fails keys with exact asset-class filters."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PrimaryDealerFails"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_primary_dealer_fails",
        period="weekly",
        market="us",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> PrimaryDealerFailsQuery:
        """Validate public filters and refuse percent values lacking a pinned denominator."""
        query = PrimaryDealerFailsQuery.model_validate(kwargs)
        if query.unit == "percent":
            raise ProviderEngineQueryError(
                "NYFED_UNSUPPORTED_FAILS_UNIT",
                invalid=("unit",),
            )
        return query

    def extract_data(
        self,
        params: PrimaryDealerFailsQuery,
        ctx: FetchContext,
    ) -> tuple[_DealerSeriesRecord, ...]:
        """Request only the manually allowlisted Table V series for this filter."""
        keys = _FAIL_KEYIDS_BY_ASSET_CLASS[params.asset_class]
        path_keys = quote("_".join(keys), safe="_-")
        document = _request_json(
            f"pd/get/{path_keys}.json",
            params=None,
            context=ctx,
            partial=False,
            requests_made=0,
        )
        return _parse_dealer_series(document, expected_keys=keys)

    def transform_data(
        self,
        raw: tuple[_DealerSeriesRecord, ...],
        params: PrimaryDealerFailsQuery,
    ) -> FetchResult:
        """Preserve the two MBS fail categories and receive/deliver identities."""
        if type(raw) is not tuple:
            raise NyfedProviderError("NYFED_BAD_SHAPE")
        rows: dict[tuple[date, str], int | float] = {}
        for source_row in raw:
            if not _in_date_window(source_row.as_of_date, params):
                continue
            identity = (source_row.as_of_date, source_row.key_id)
            value = _parse_source_number(source_row.value)
            previous = rows.get(identity)
            if previous is not None and previous != value:
                raise NyfedProviderError("NYFED_DUPLICATE_SERIES_DATE_CONFLICT")
            rows[identity] = value

        return [
            PrimaryDealerFails(
                date=day,
                symbol=key_id,
                title=_FAIL_DESCRIPTIONS[key_id],
                value=value,
            )
            for (day, key_id), value in sorted(rows.items())
        ]


class PrimaryDealerPositioningFetcher(
    Fetcher[PrimaryDealerPositioningQuery, tuple[_DealerSeriesRecord, ...]]
):
    """Fetch manually declared Table III net-position key groups."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "PrimaryDealerPositioning"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_primary_dealer_positioning",
        period="weekly",
        market="us",
        source=SOURCE,
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> PrimaryDealerPositioningQuery:
        """Validate the public category selector before any source call."""
        query = PrimaryDealerPositioningQuery.model_validate(kwargs)
        if query.category not in _POSITIONING_KEYIDS_BY_CATEGORY:
            raise ProviderEngineQueryError(
                "NYFED_UNSUPPORTED_POSITIONING_CATEGORY",
                invalid=("category",),
            )
        return query

    def extract_data(
        self,
        params: PrimaryDealerPositioningQuery,
        ctx: FetchContext,
    ) -> tuple[_DealerSeriesRecord, ...]:
        """Request only the exact source keys assigned to the selected category."""
        keys = _POSITIONING_KEYIDS_BY_CATEGORY[params.category]
        path_keys = quote("_".join(keys), safe="_-")
        document = _request_json(
            f"pd/get/{path_keys}.json",
            params=None,
            context=ctx,
            partial=False,
            requests_made=0,
        )
        return _parse_dealer_series(document, expected_keys=keys)

    def transform_data(
        self,
        raw: tuple[_DealerSeriesRecord, ...],
        params: PrimaryDealerPositioningQuery,
    ) -> FetchResult:
        """Normalize native net-position values without zero-filling absent dates."""
        if type(raw) is not tuple:
            raise NyfedProviderError("NYFED_BAD_SHAPE")
        rows: dict[tuple[date, str], int] = {}
        for source_row in raw:
            if not _in_date_window(source_row.as_of_date, params):
                continue
            identity = (source_row.as_of_date, source_row.key_id)
            value = _parse_position_number(source_row.value)
            previous = rows.get(identity)
            if previous is not None and previous != value:
                raise NyfedProviderError("NYFED_DUPLICATE_SERIES_DATE_CONFLICT")
            rows[identity] = value

        return [
            PrimaryDealerPositioning(
                date=day,
                symbol=key_id,
                value=value,
                name=key_id,
                title=_POSITIONING_DESCRIPTIONS[key_id],
            )
            for (day, key_id), value in sorted(rows.items())
        ]


def _parse_dealer_series(
    document: object,
    *,
    expected_keys: tuple[str, ...],
) -> tuple[_DealerSeriesRecord, ...]:
    """Validate the documented dealer JSON envelope and selected key identities."""
    try:
        envelope = _DealerSeriesEnvelope.model_validate(document)
    except ValidationError:
        raise NyfedProviderError("NYFED_BAD_SHAPE") from None
    if any(row.key_id not in expected_keys for row in envelope.series):
        raise NyfedProviderError("NYFED_UNEXPECTED_KEYID")
    return tuple(envelope.series)


def _parse_source_number(value: str) -> int | float:
    """Parse a finite source-native decimal without rounding integer amounts."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise NyfedProviderError("NYFED_BAD_NUMERIC_VALUE") from None
    if not parsed.is_finite():
        raise NyfedProviderError("NYFED_BAD_NUMERIC_VALUE")
    if parsed == parsed.to_integral_value():
        return int(parsed)
    result = float(parsed)
    if result in (float("inf"), float("-inf")):
        raise NyfedProviderError("NYFED_BAD_NUMERIC_VALUE")
    return result


def _parse_position_number(value: str) -> int:
    """Parse the public integer position amount without truncating fractions."""
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise NyfedProviderError("NYFED_BAD_NUMERIC_VALUE") from None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise NyfedProviderError("NYFED_BAD_NUMERIC_VALUE")
    return int(parsed)


__all__ = [
    "NyfedProviderError",
    "PrimaryDealerFailsFetcher",
    "PrimaryDealerFailsQuery",
    "PrimaryDealerPositioningFetcher",
    "PrimaryDealerPositioningQuery",
    "SOFRFetcher",
    "SOFRQuery",
]
