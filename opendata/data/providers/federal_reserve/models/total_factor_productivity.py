"""San Francisco Fed quarterly total-factor-productivity workbook."""

from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO
from typing import ClassVar, Literal

from openpyxl import load_workbook
from pydantic import StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_macro import FederalReserveTotalFactorProductivity
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers.federal_reserve._iter2_client import (
    FederalReserveSourceError,
    get_bytes,
)
from opendata.data.providers.federal_reserve.models._iter2_common import parse_float

_WORKBOOK_URL = "https://www.frbsf.org/wp-content/uploads/quarterly_tfp.xlsx?2026-10-09="
_HEADER = (
    "date",
    "dY_prod",
    "dY_inc",
    "dY",
    "dhours",
    "dLP",
    "dk",
    "dLQ_BLS_interpolated",
    "dLQ_Aaronson_Sullivan",
    "dLQ",
    "alpha",
    "dtfp",
    "dutil",
    "dtfp_util",
    "relativePrice",
    "invShare",
    "dtfp_I",
    "dtfp_C",
    "du_invest",
    "du_consumption",
    "dtfp_I_util",
    "dtfp_C_util",
)
_PERIOD_RE = re.compile(r"(?P<year>\d{4}):Q(?P<quarter>[1-4])\Z")
_VINTAGE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%Y-%m-%d",
    "%m/%d/%Y",
)
_FULL_VINTAGE_RE = re.compile(
    r"^(?P<date>.+?)[ \t]+(?P<time>\d{1,2}:\d{2}[ \t]+(?:AM|PM))"
    r"[ \t]+by[ \t]+(?P<author>[^()\r\n]+?)[ \t]+"
    r"\(Directory:[ \t]*(?P<directory>[^()\r\n]+)\)[ \t]*$",
    re.IGNORECASE,
)


class TotalFactorProductivityQuery(QueryParams):
    """Exact inclusive quarterly period range with service-injected Fed identity fields."""

    source: Literal["federal_reserve"] = "federal_reserve"
    market: Literal["us"] = "us"
    start_period: StrictStr | None = None
    end_period: StrictStr | None = None
    max_records: StrictInt = 10_000

    @field_validator("start_period", "end_period")
    @classmethod
    def _validate_period(cls, value: str | None) -> str | None:
        if value is not None and _PERIOD_RE.fullmatch(value) is None:
            raise ValueError("quarterly periods must use YYYY:Q[1-4]")
        return value

    @model_validator(mode="after")
    def _validate_period_window(self) -> TotalFactorProductivityQuery:
        if (
            self.start_period is not None
            and self.end_period is not None
            and self.end_period < self.start_period
        ):
            raise ValueError("end_period must not precede start_period")
        return self


def _parse_vintage(workbook: object) -> date:
    """Read the workbook's release date from its public readme tab."""
    try:
        readme = workbook["readme"]  # type: ignore[index]
        values = [cell for row in readme.iter_rows(values_only=True) for cell in row]
    except Exception:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_README") from None
    for value in values:
        if isinstance(value, datetime):
            # Only accept an explicit date cell on the produced-on line, not arbitrary dates.
            continue
        if isinstance(value, str) and (match := re.search(r"produced on", value, re.IGNORECASE)):
            text = value[match.end() :].strip().strip(":")
            parsed = next(
                (result for fmt in _VINTAGE_FORMATS if (result := _parse_vintage_text(text, fmt))),
                None,
            )
            if parsed is None:
                parsed = _parse_full_vintage(text)
            if parsed is None:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_VINTAGE")
            return parsed
    raise FederalReserveSourceError("FEDERAL_RESERVE_MISSING_TFP_VINTAGE")


def _parse_vintage_text(text: str, date_format: str) -> date | None:
    """Try one documented date spelling without nesting exception handling in the scan."""
    try:
        return datetime.strptime(text, date_format).date()
    except ValueError:
        return None


def _parse_full_vintage(text: str) -> date | None:
    """Parse the source readme's date, 12-hour time, author, and directory line."""
    match = _FULL_VINTAGE_RE.fullmatch(text)
    if match is None or not match.group("author").strip() or not match.group("directory").strip():
        return None

    date_text = match.group("date").strip()
    date_time = f"{date_text} {match.group('time')}"
    return next(
        (
            result
            for date_format in _VINTAGE_FORMATS
            if (
                result := _parse_vintage_text(
                    date_time,
                    f"{date_format} %I:%M %p",
                )
            )
        ),
        None,
    )


class FederalReserveTotalFactorProductivityFetcher(Fetcher[TotalFactorProductivityQuery, bytes]):
    """Read the exact quarterly table and preserve its annualized-percent units and vintage."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "TotalFactorProductivity"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_total_factor_productivity",
        period="q",
        market="us",
        source="federal_reserve",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TotalFactorProductivityQuery:
        """Validate exact quarterly-period bounds."""
        return TotalFactorProductivityQuery.model_validate(kwargs)

    def extract_data(self, params: TotalFactorProductivityQuery, ctx: FetchContext) -> bytes:
        """Retrieve the San Francisco Fed's official quarterly workbook."""
        if type(params) is not TotalFactorProductivityQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        return get_bytes(_WORKBOOK_URL, ctx.timeout)

    def transform_data(
        self, raw: bytes, params: TotalFactorProductivityQuery
    ) -> tuple[FederalReserveTotalFactorProductivity, ...]:
        """Validate workbook shape, row ordering, revision vintage, and query window."""
        if not isinstance(raw, bytes) or not raw:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_WORKBOOK")
        try:
            workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        except Exception:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_WORKBOOK") from None
        try:
            if "quarterly" not in workbook.sheetnames:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_WORKBOOK")
            vintage = _parse_vintage(workbook)
            worksheet = workbook["quarterly"]
            iterator = worksheet.iter_rows(values_only=True)
            note = next(iterator, None)
            header = next(iterator, None)
            if (
                not isinstance(note, tuple)
                or not any(
                    isinstance(cell, str)
                    and "annual rate" in cell.casefold()
                    and "percent change" in cell.casefold()
                    for cell in note
                )
                or header is None
                or tuple(header) != _HEADER
            ):
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_HEADER")
            rows = [tuple(row) for row in iterator if any(cell is not None for cell in row)]
        finally:
            workbook.close()

        result: list[FederalReserveTotalFactorProductivity] = []
        previous_period: str | None = None
        for row in rows:
            if len(row) != len(_HEADER):
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_ROW")
            period = row[0]
            if not isinstance(period, str) or _PERIOD_RE.fullmatch(period) is None:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_TFP_PERIOD")
            if previous_period is not None and period <= previous_period:
                raise FederalReserveSourceError("FEDERAL_RESERVE_UNSORTED_TFP_ROWS")
            previous_period = period
            if params.start_period is not None and period < params.start_period:
                continue
            if params.end_period is not None and period > params.end_period:
                continue
            values: dict[str, object] = {
                "period": period,
                "vintage_date": vintage,
            }
            values.update(
                {name: parse_float(value) for name, value in zip(_HEADER[1:], row[1:], strict=True)}
            )
            result.append(FederalReserveTotalFactorProductivity.model_validate(values))
            if len(result) > params.max_records:
                raise FederalReserveSourceError("FEDERAL_RESERVE_TOO_MANY_RECORDS")
        return tuple(result)


__all__ = ["FederalReserveTotalFactorProductivityFetcher", "TotalFactorProductivityQuery"]
