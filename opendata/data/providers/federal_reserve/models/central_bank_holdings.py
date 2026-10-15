"""New York Fed SOMA summary, security detail, and WAM holdings products."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import quote

from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationInfo, field_validator

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_macro import FederalReserveCentralBankHolding
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.federal_reserve._iter2_client import (
    FederalReserveSourceError,
    get_json,
)
from opendata.data.providers.federal_reserve.models._iter2_common import (
    parse_decimal,
    parse_float,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_SOMA_BASE = "https://markets.newyorkfed.org/api/soma"
_SOMA_SUMMARY_URL = f"{_SOMA_BASE}/summary.json"
_SOMA_LATEST_URL = f"{_SOMA_BASE}/asofdates/latest.json"
_SUMMARY_KEYS = {
    "asOfDate",
    "mbs",
    "cmbs",
    "tips",
    "frn",
    "tipsInflationCompensation",
    "notesbonds",
    "bills",
    "agencies",
    "total",
}
_SUMMARY_FIELDS = {
    "mbs": "mbs",
    "cmbs": "cmbs",
    "tips": "tips",
    "frn": "frn",
    "tipsInflationCompensation": "tips_inflation_compensation",
    "notesbonds": "notes_and_bonds",
    "bills": "bills",
    "agencies": "agencies",
    "total": "total",
}
_DETAIL_BASE_KEYS = {"asOfDate", "cusip", "securityType"}
_DETAIL_BOND_KEYS = {
    "maturityDate",
    "issuer",
    "spread",
    "coupon",
    "parValue",
    "inflationCompensation",
    "percentOutstanding",
    "changeFromPriorWeek",
    "changeFromPriorYear",
}
_DETAIL_MBS_KEYS = {"securityDescription", "term", "currentFaceValue"}
_TREASURY_SECURITY_TYPES = {"Bills", "FRNs", "NotesBonds", "TIPS"}
_TREASURY_QUERY_TYPES = {
    "all_treasury": "all",
    "bills": "bills",
    "notesbonds": "notesbonds",
    "frn": "frn",
    "tips": "tips",
}
_AGENCY_QUERY_TYPES = {
    "all_agency": "all",
    "agency_debts": "agency debts",
    "mbs": "mbs",
    "cmbs": "cmbs",
}
_HOLDING_TYPES: tuple[str, ...] = (
    "all_agency",
    "agency_debts",
    "mbs",
    "cmbs",
    "all_treasury",
    "bills",
    "notesbonds",
    "frn",
    "tips",
)


class CentralBankHoldingsQuery(QueryParams):
    """Selectors published for the NY Fed SOMA summary and detail export."""

    source: Literal["federal_reserve"] = "federal_reserve"
    market: Literal["us"] = "us"
    symbol: None = None
    start_date: None = None
    end_date: None = None
    date: date_type | None = None
    holding_type: (
        Literal[
            "all_agency",
            "agency_debts",
            "mbs",
            "cmbs",
            "all_treasury",
            "bills",
            "notesbonds",
            "frn",
            "tips",
        ]
        | None
    ) = "all_treasury"
    summary: StrictBool | None = False
    cusip: StrictStr | None = None
    wam: StrictBool | None = False
    monthly: StrictBool | None = False
    max_records: StrictInt = Field(default=200_000, ge=1, le=200_000)

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, value: object) -> object:
        """Require an exact ISO calendar date, without a time component."""
        if value is None or type(value) is date_type:
            return value
        if isinstance(value, datetime):
            raise ValueError("date must not include a time")
        if not isinstance(value, str):
            raise ValueError("date must be a calendar date")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be a canonical YYYY-MM-DD string") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be a canonical YYYY-MM-DD string")
        return parsed

    @field_validator("cusip")
    @classmethod
    def _validate_cusip(cls, value: str | None) -> str | None:
        """Reject blank selectors and path delimiters before constructing source URLs."""
        if value is not None and (not value or value != value.strip() or "/" in value):
            raise ValueError("cusip must be a non-empty source identifier")
        return value

    @field_validator("monthly")
    @classmethod
    def _validate_monthly_type(cls, value: bool | None, info: ValidationInfo) -> bool | None:
        """Monthly output is published only for Treasury holdings."""
        if not value:
            return value
        holding_type = info.data.get("holding_type", "all_treasury")
        wam = info.data.get("wam", False)
        summary = info.data.get("summary", False)
        if not summary and not wam and holding_type not in _TREASURY_QUERY_TYPES:
            raise ValueError("monthly is only supported for Treasury holding types")
        return value


class FederalReserveCentralBankHoldingsFetcher(Fetcher[CentralBankHoldingsQuery, object]):
    """Parse NY Fed SOMA data and express monetary outputs in the documented $Thousands."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CentralBankHoldings"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_central_bank_holdings",
        period="w",
        market="us",
        source="federal_reserve",
        verified=False,
    )
    #: The contract ``domains.yaml`` declares for this domain -- verified by
    #: ``contract_model('federal_reserve_central_bank_holdings') ->
    #: opendata.data.models.federal_reserve_macro.FederalReserveCentralBankHolding``, the class
    #: every reader path here already builds; the governed query service revalidates rows against
    #: that contract rather than against this fetcher's own import.
    row_model: ClassVar[type[ContractModel]] = FederalReserveCentralBankHolding

    def transform_query(self, **kwargs: object) -> CentralBankHoldingsQuery:
        """Validate official date, holding-type, summary, CUSIP, WAM, and monthly selectors."""
        return CentralBankHoldingsQuery.model_validate(kwargs)

    def extract_data(self, params: CentralBankHoldingsQuery, ctx: FetchContext) -> object:
        """Select the official summary, detail, monthly, or weighted-average-maturity endpoint."""
        if type(params) is not CentralBankHoldingsQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        if params.summary:
            return get_json(_SOMA_SUMMARY_URL, ctx.timeout)

        holding_type = params.holding_type or "all_treasury"
        if params.wam:
            observation_date = params.date or self._latest_date(ctx.timeout)
            encoded_date = observation_date.isoformat()
            treasury = get_json(f"{_SOMA_BASE}/tsy/wam/all/asof/{encoded_date}.json", ctx.timeout)
            agency = get_json(
                f"{_SOMA_BASE}/agency/wam/agency%20debts/asof/{encoded_date}.json",
                ctx.timeout,
            )
            return (treasury, agency)

        if params.monthly:
            return get_json(f"{_SOMA_BASE}/tsy/get/monthly.json", ctx.timeout)

        if params.cusip is not None:
            route = "agency" if holding_type in _AGENCY_QUERY_TYPES else "tsy"
            safe_cusip = quote(params.cusip, safe="")
            return get_json(f"{_SOMA_BASE}/{route}/get/cusip/{safe_cusip}.json", ctx.timeout)

        observation_date = params.date or self._latest_date(ctx.timeout)
        encoded_date = observation_date.isoformat()
        if holding_type in _AGENCY_QUERY_TYPES:
            source_type = _AGENCY_QUERY_TYPES[holding_type]
            if source_type == "all":
                url = f"{_SOMA_BASE}/agency/get/asof/{encoded_date}.json"
            else:
                safe_type = quote(source_type, safe="")
                url = f"{_SOMA_BASE}/agency/get/{safe_type}/asof/{encoded_date}.json"
        else:
            source_type = _TREASURY_QUERY_TYPES[holding_type]
            if source_type == "all":
                url = f"{_SOMA_BASE}/tsy/get/asof/{encoded_date}.json"
            else:
                url = f"{_SOMA_BASE}/tsy/get/{source_type}/asof/{encoded_date}.json"
        return get_json(url, ctx.timeout)

    def transform_data(
        self, raw: object, params: CentralBankHoldingsQuery
    ) -> tuple[FederalReserveCentralBankHolding, ...]:
        """Validate each official response form and preserve all monetary scale exactly."""
        if type(params) is not CentralBankHoldingsQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        if params.summary:
            return self._summary_rows(raw, params)
        if params.wam:
            if not isinstance(raw, tuple) or len(raw) != 2:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_SHAPE")
            rows = (
                self._wam_row(raw[0], params, "Treasury"),
                self._wam_row(raw[1], params, "Agency"),
            )
            return tuple(sorted(rows, key=lambda row: (row.date, row.security_type or "")))
        return self._detail_rows(raw, params)

    @staticmethod
    def _latest_date(timeout: float | None) -> date_type:
        """Resolve the latest published as-of date from the NY Fed date endpoint."""
        document = get_json(_SOMA_LATEST_URL, timeout)
        root = document.get("soma")
        if not isinstance(root, dict) or set(root) != {"asOfDates"}:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_LATEST_SHAPE")
        dates = root.get("asOfDates")
        if not isinstance(dates, list) or len(dates) != 1 or not isinstance(dates[0], str):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_LATEST_SHAPE")
        return _source_date(dates[0])

    @staticmethod
    def _summary_rows(
        raw: object, params: CentralBankHoldingsQuery
    ) -> tuple[FederalReserveCentralBankHolding, ...]:
        """Read weekly historical summary rows and convert source dollars to $Thousands."""
        root = _soma_root(raw, "summary")
        source_rows = root.get("summary")
        if not isinstance(source_rows, list):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_SHAPE")
        result: list[FederalReserveCentralBankHolding] = []
        seen: set[date_type] = set()
        for item in source_rows:
            if not isinstance(item, dict) or set(item) != _SUMMARY_KEYS:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_SUMMARY_ROW")
            observation_date = _source_date(item["asOfDate"])
            if observation_date in seen:
                raise FederalReserveSourceError("FEDERAL_RESERVE_DUPLICATE_SOMA_ROW")
            seen.add(observation_date)
            if params.date is not None and observation_date != params.date:
                continue
            normalized: dict[str, object] = {"date": observation_date}
            for source_key, model_key in _SUMMARY_FIELDS.items():
                normalized[model_key] = _amount_thousands(item[source_key], nullable=True)
            result.append(FederalReserveCentralBankHolding.model_validate(normalized))
            _check_record_limit(len(result), params.max_records)
        result.sort(key=lambda row: row.date)
        return tuple(result)

    @staticmethod
    def _detail_rows(
        raw: object, params: CentralBankHoldingsQuery
    ) -> tuple[FederalReserveCentralBankHolding, ...]:
        """Normalize per-security detail rows, preserving source identifiers and unit fields."""
        root = _soma_root(raw, "holdings")
        source_rows = root.get("holdings")
        if not isinstance(source_rows, list):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_SHAPE")
        holding_type = params.holding_type or "all_treasury"
        allowed_types = _allowed_security_types(holding_type)
        result: list[FederalReserveCentralBankHolding] = []
        seen: set[tuple[date_type, str, str | None]] = set()
        for item in source_rows:
            normalized = _normalize_detail_row(item)
            if normalized["security_type"] not in allowed_types:
                continue
            if params.date is not None and normalized["date"] != params.date:
                continue
            row = FederalReserveCentralBankHolding.model_validate(normalized)
            key = (row.date, row.security_type or "", row.cusip)
            if key in seen:
                raise FederalReserveSourceError("FEDERAL_RESERVE_DUPLICATE_SOMA_DETAIL")
            seen.add(key)
            result.append(row)
            _check_record_limit(len(result), params.max_records)
        result.sort(key=lambda row: (row.date, row.security_type or "", row.cusip or ""))
        return tuple(result)

    @staticmethod
    def _wam_row(
        raw: object, params: CentralBankHoldingsQuery, group: Literal["Treasury", "Agency"]
    ) -> FederalReserveCentralBankHolding:
        """Normalize the NY Fed WAM scalar and its source-reported security-type selector."""
        if not isinstance(raw, dict) or set(raw) != {"soma"}:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_SHAPE")
        root = raw.get("soma")
        if not isinstance(root, dict):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_SHAPE")
        if set(root) != {"asOfDate", "wam", "securityTypes"}:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_ROW")
        observation_date = _source_date(root["asOfDate"])
        if params.date is not None and observation_date != params.date:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_DATE")
        security_types = root["securityTypes"]
        if (
            not isinstance(security_types, list)
            or not security_types
            or any(not isinstance(value, str) for value in security_types)
        ):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_ROW")
        if group == "Treasury" and set(security_types) != _TREASURY_SECURITY_TYPES:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_ROW")
        if group == "Agency" and security_types != ["Agency Debts"]:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_WAM_ROW")
        wam = parse_float(root["wam"], nullable=False)
        return FederalReserveCentralBankHolding.model_validate(
            {
                "date": observation_date,
                "security_type": group,
                "wam": wam,
                "security_types": tuple(security_types),
            }
        )


def _soma_root(raw: object, response_key: str) -> dict[str, object]:
    """Require the documented NY Fed JSON root without accepting a wrong selector response."""
    if not isinstance(raw, dict) or set(raw) != {"soma"}:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_SHAPE")
    root = raw.get("soma")
    if not isinstance(root, dict) or set(root) != {response_key}:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_SHAPE")
    return root


def _normalize_detail_row(raw: object) -> dict[str, object]:
    """Validate one security-type-specific API row and map its fields to the public model."""
    if not isinstance(raw, dict):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")
    security_type = raw.get("securityType")
    if not isinstance(security_type, str):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")
    expected_keys = _DETAIL_BASE_KEYS
    if security_type in _TREASURY_SECURITY_TYPES | {"Agency Debts"}:
        expected_keys = expected_keys | _DETAIL_BOND_KEYS
    elif security_type == "MBS":
        expected_keys = expected_keys | _DETAIL_MBS_KEYS | {"isAggregated"}
    elif security_type == "CMBS":
        expected_keys = expected_keys | _DETAIL_MBS_KEYS
    else:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")
    if set(raw) != expected_keys:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")

    normalized: dict[str, object] = {
        "date": _source_date(raw["asOfDate"]),
        "security_type": security_type,
        "cusip": _optional_text(raw["cusip"]),
    }
    if security_type in {"MBS", "CMBS"}:
        normalized.update(
            {
                "description": _optional_text(raw["securityDescription"]),
                "term": _optional_text(raw["term"]),
                "face_value": _amount_thousands(raw["currentFaceValue"], nullable=True),
            }
        )
        if security_type == "MBS":
            normalized["is_aggregated"] = _optional_aggregated(raw["isAggregated"])
    else:
        normalized.update(
            {
                "issuer": _optional_text(raw["issuer"]),
                "maturity_date": _optional_source_date(raw["maturityDate"]),
                "par_value": _amount_thousands(raw["parValue"], nullable=True),
                "coupon": _optional_decimal(raw["coupon"]),
                "spread": _optional_decimal(raw["spread"]),
                "percent_outstanding": _optional_decimal(raw["percentOutstanding"]),
                "change_prior_week": _amount_thousands(raw["changeFromPriorWeek"], nullable=True),
                "change_prior_year": _amount_thousands(raw["changeFromPriorYear"], nullable=True),
            }
        )
        # The public CentralBankHoldings data contract does not expose this NY Fed detail field.
        parse_decimal(raw["inflationCompensation"], nullable=True)
    return normalized


def _allowed_security_types(holding_type: str) -> set[str]:
    """Map public holding-type selectors to the security types actually returned by NY Fed."""
    if holding_type in _AGENCY_QUERY_TYPES:
        return {
            "all_agency": {"Agency Debts", "MBS", "CMBS"},
            "agency_debts": {"Agency Debts"},
            "mbs": {"MBS"},
            "cmbs": {"CMBS"},
        }[holding_type]
    if holding_type in _TREASURY_QUERY_TYPES:
        if holding_type == "all_treasury":
            return set(_TREASURY_SECURITY_TYPES)
        return {
            "bills": {"Bills"},
            "notesbonds": {"NotesBonds"},
            "frn": {"FRNs"},
            "tips": {"TIPS"},
        }[holding_type]
    raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_HOLDING_TYPE")


def _source_date(value: object) -> date_type:
    """Parse a source date with exact ISO text validation."""
    if not isinstance(value, str):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE") from None
    if parsed.isoformat() != value:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
    return parsed


def _optional_source_date(value: object) -> date_type | None:
    """Keep the source's documented blank maturity cells null."""
    if value is None or value == "":
        return None
    return _source_date(value)


def _optional_text(value: object) -> str | None:
    """Normalize only source blank-string cells to null."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip():
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")
    return value


def _optional_aggregated(value: object) -> Literal["Y"] | None:
    """Accept only the NY Fed's documented aggregated marker or an empty cell."""
    if value is None or value == "":
        return None
    if value == "Y":
        return "Y"
    raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_SOMA_DETAIL_ROW")


def _optional_decimal(value: object) -> Decimal | None:
    """Parse an optional non-monetary numeric detail field without rescaling it."""
    return parse_decimal(value, nullable=True)


def _amount_thousands(value: object, *, nullable: bool) -> Decimal | None:
    """Convert exact source dollars to the published holdings contract's $Thousands."""
    amount = parse_decimal(value, nullable=nullable)
    return None if amount is None else amount / Decimal(1000)


def _check_record_limit(count: int, maximum: int) -> None:
    """Reject oversized source results rather than silently truncating them."""
    if count > maximum:
        raise FederalReserveSourceError("FEDERAL_RESERVE_TOO_MANY_RECORDS")


__all__ = ["FederalReserveCentralBankHoldingsFetcher", "CentralBankHoldingsQuery"]
