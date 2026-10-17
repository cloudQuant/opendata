"""Long-form H.15 nominal Treasury yield curve from the Board's official CSV package."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import TYPE_CHECKING, ClassVar

from pydantic import StrictStr, field_validator

from opendata.data.capability import Capability
from opendata.data.models.federal_reserve_clean_documents import (
    FederalReserveYieldCurveObservation,
)
from opendata.data.protocol import FetchContext, Fetcher
from opendata.data.providers.federal_reserve._iter2_client import (
    FederalReserveSourceError,
    get_bytes,
)
from opendata.data.providers.federal_reserve.models._iter2_common import (
    FederalReserveDateQuery,
    parse_decimal,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_H15_URL = (
    "https://www.federalreserve.gov/datadownload/Output.aspx?rel=H15"
    "&series=bf17364827e38702b42a58cf8eaa3f78&lastobs=&filetype=csv"
    "&label=include&layout=seriescolumn&type=package&from=&to="
)
_COLUMNS = (
    ("1M", "RIFLGFCM01_N.B"),
    ("3M", "RIFLGFCM03_N.B"),
    ("6M", "RIFLGFCM06_N.B"),
    ("1Y", "RIFLGFCY01_N.B"),
    ("2Y", "RIFLGFCY02_N.B"),
    ("3Y", "RIFLGFCY03_N.B"),
    ("5Y", "RIFLGFCY05_N.B"),
    ("7Y", "RIFLGFCY07_N.B"),
    ("10Y", "RIFLGFCY10_N.B"),
    ("20Y", "RIFLGFCY20_N.B"),
    ("30Y", "RIFLGFCY30_N.B"),
)
_HEADER = ("Time Period", *(source for _, source in _COLUMNS))
_PREAMBLE_LABELS = (
    "Series Description",
    "Unit:",
    "Multiplier:",
    "Currency:",
    "Unique Identifier:",
)
_MATURITIES = tuple(name for name, _ in _COLUMNS)


class YieldCurveQuery(FederalReserveDateQuery):
    """Inclusive date window and optional subset of the eleven published maturities."""

    maturities: tuple[StrictStr, ...] | None = None

    @field_validator("maturities", mode="before")
    @classmethod
    def _require_maturity_sequence(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("maturities must be a list or tuple")

    @field_validator("maturities")
    @classmethod
    def _validate_maturities(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if (
            not value
            or len(value) != len(set(value))
            or any(item not in _MATURITIES for item in value)
        ):
            raise ValueError("maturities must be unique published maturity labels")
        return value


class FederalReserveYieldCurveFetcher(Fetcher[YieldCurveQuery, bytes]):
    """Parse the five-row DDP preamble and expand its H.15 table to typed long records."""

    async_mode: ClassVar[str] = "bounded_thread"
    canonical_model: ClassVar[str | None] = "YieldCurve"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="federal_reserve_yield_curve",
        period="d",
        market="us",
        source="federal_reserve",
        verified=False,
    )
    #: The contract ``domains.yaml`` declares for this domain — the governed query service
    #: serializes rows against it, so :meth:`transform_data` has to build exactly this class.
    row_model: ClassVar[type[ContractModel]] = FederalReserveYieldCurveObservation

    def transform_query(self, **kwargs: object) -> YieldCurveQuery:
        """Validate the date window and optional H.15 maturity selection."""
        return YieldCurveQuery.model_validate(kwargs)

    def extract_data(self, params: YieldCurveQuery, ctx: FetchContext) -> bytes:
        """Fetch the fixed public H.15 CSV package through the governed transport."""
        if type(params) is not YieldCurveQuery:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_QUERY")
        return get_bytes(_H15_URL, ctx.timeout)

    def transform_data(
        self,
        raw: bytes,
        params: YieldCurveQuery,
    ) -> tuple[FederalReserveYieldCurveObservation, ...]:
        """Validate the released CSV shape, normalize percent units, and retain null rows."""
        if not isinstance(raw, bytes):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_CSV")
        try:
            rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        except (UnicodeDecodeError, csv.Error):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_CSV") from None
        preamble = tuple(row[0].strip() if row else "" for row in rows[:5])
        # The released package pads its fifth label ("Unique Identifier: ") ahead of the comma, so
        # the compare drops surrounding whitespace; a wrong or missing label still refuses.
        if len(rows) <= 5 or preamble != _PREAMBLE_LABELS:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_CSV_PREAMBLE")
        header = tuple(rows[5])
        if header != _HEADER:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_CSV_HEADER")

        selected = set(params.maturities or _MATURITIES)
        records: list[FederalReserveYieldCurveObservation] = []
        last_date: date | None = None
        for source_row in rows[6:]:
            if not source_row or all(cell == "" for cell in source_row):
                continue
            if len(source_row) != len(_HEADER):
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_CSV_ROW")
            date_text = source_row[0]
            try:
                observation_date = date.fromisoformat(date_text)
            except ValueError:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE") from None
            if observation_date.isoformat() != date_text:
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
            if last_date is not None and observation_date <= last_date:
                raise FederalReserveSourceError("FEDERAL_RESERVE_UNSORTED_CSV")
            last_date = observation_date
            if params.start_date is not None and observation_date < params.start_date:
                continue
            if params.end_date is not None and observation_date > params.end_date:
                continue
            for column_index, (maturity, _) in enumerate(_COLUMNS, start=1):
                if maturity not in selected:
                    continue
                raw_value = source_row[column_index]
                if raw_value == "ND":
                    rate = None
                else:
                    parsed = parse_decimal(raw_value, nullable=True)
                    rate = None if parsed is None else float(parsed / 100)
                records.append(
                    FederalReserveYieldCurveObservation(
                        date=observation_date,
                        maturity=maturity,
                        rate=rate,
                    )
                )
                if len(records) > params.max_records:
                    raise FederalReserveSourceError("FEDERAL_RESERVE_TOO_MANY_RECORDS")
        return tuple(records)


__all__ = ["FederalReserveYieldCurveFetcher", "YieldCurveQuery"]
