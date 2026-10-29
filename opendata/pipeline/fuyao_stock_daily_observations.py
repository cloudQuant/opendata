"""Prepare a Fuyao pilot observation document for the THS daily ODS schema.

This module only validates and reshapes an already captured local document. It
does not perform network requests, persist the result, or establish when or
from whom the document was collected. Fields absent from the pilot's reviewed
document, such as ``currency`` and ``interval``, remain null in the output.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Final, NoReturn

import pandas as pd

from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.dumps import DAILY_K_COLUMNS, UNADJUSTED
from opendata.data.providers.ths.endpoints import (
    PRICES_ENDPOINT,
    millis_to_trading_date,
    shanghai_midnight_millis,
)
from opendata.pipeline.dump_import import DumpImportError, prepare_daily_k_frame

ODS_STOCK_DAILY_COLUMNS: Final[tuple[str, ...]] = (
    "thscode",
    "trade_date",
    "date_ms",
    "currency",
    "interval",
    "adjusted",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)

_OBSERVATION_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "source",
        "vendor",
        "endpoint_path",
        "request",
        "time_field",
        "time_semantics",
        "field_units",
        "rows",
    }
)
_REQUEST_KEYS = frozenset(
    {"symbol", "start_date", "end_date", "adjustment_basis", "provider_adjust_parameter"}
)
_ROW_KEYS = frozenset({"symbol", "trade_date", "adjustment_basis", "source_fields"})
_SOURCE_FIELD_KEYS = frozenset(
    {
        "date_ms",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "turnover",
    }
)
_BAR_NUMERIC_FIELDS = (
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "turnover",
)
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$", re.ASCII)
_EXPECTED_FIELD_UNITS: Final[dict[str, str]] = {
    "open_price": "provider-native value; not independently calibrated",
    "high_price": "provider-native value; not independently calibrated",
    "low_price": "provider-native value; not independently calibrated",
    "close_price": "provider-native value; not independently calibrated",
    "volume": "provider contract label: shares; not independently calibrated",
    "turnover": "provider contract label: CNY; not independently calibrated",
}
_TIME_SEMANTICS = "Unix milliseconds at Asia/Shanghai trading-date midnight"


class FuyaoStockDailyObservationError(ValueError):
    """A content-free rejection code for invalid pilot observations."""

    def __init__(self, code: str) -> None:
        """Create an error without retaining observation values."""
        self.code = code
        super().__init__(code)


def prepare_fuyao_stock_daily_observations(document: Mapping[str, object]) -> pd.DataFrame:
    """Validate a pilot document and return rows in THS ODS table order.

    The ``source_fields`` payload is mapped directly to its matching THS
    columns without scaling or unit conversion. ``adjusted`` is the reviewed
    ODS contract value derived from the document's explicit unadjusted request;
    absent ``currency`` and ``interval`` values stay null. The returned frame
    contains only the twelve data columns owned by ``ods_stock_daily_ths``;
    the ODS writer remains responsible for write-time metadata.

    Args:
        document: Parsed observation document emitted by the Fuyao pilot.

    Returns:
        A validated frame ordered by ``(thscode, trade_date)`` and matching
        the ODS table's data columns. No capture timestamp or source-authenticity
        assertion is added.

    Raises:
        FuyaoStockDailyObservationError: The document schema, request, identity,
            date, unique key, adjustment basis, numeric values, or OHLC bounds
            are invalid.
    """
    if not isinstance(document, Mapping) or not _has_exact_keys(
        document, _OBSERVATION_DOCUMENT_KEYS
    ):
        _reject("document_schema_invalid")
    _validate_document_header(document)
    request = document.get("request")
    if not isinstance(request, Mapping) or not _has_exact_keys(request, _REQUEST_KEYS):
        _reject("request_schema_invalid")

    symbol = _require_symbol(request.get("symbol"))
    start_date = _parse_iso_date(request.get("start_date"), "request_date_invalid")
    end_date = _parse_iso_date(request.get("end_date"), "request_date_invalid")
    if start_date > end_date:
        _reject("request_date_invalid")
    if request.get("adjustment_basis") != "unadjusted":
        _reject("adjustment_basis_unsupported")
    if request.get("provider_adjust_parameter") != "none":
        _reject("adjustment_basis_unsupported")

    rows = document.get("rows")
    if type(rows) is not list or not rows:
        _reject("observations_empty_or_invalid")

    source_records: list[dict[str, object]] = []
    source_values_by_date: dict[date, dict[str, int | float]] = {}
    seen_keys: set[tuple[str, date]] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not _has_exact_keys(row, _ROW_KEYS):
            _reject("observation_schema_invalid")
        if row.get("symbol") != symbol:
            _reject("observation_symbol_mismatch")
        if row.get("adjustment_basis") != "unadjusted":
            _reject("adjustment_basis_unsupported")
        trade_date = _parse_iso_date(row.get("trade_date"), "observation_date_invalid")
        if not start_date <= trade_date <= end_date:
            _reject("observation_date_outside_request")
        business_key = (symbol, trade_date)
        if business_key in seen_keys:
            _reject("observation_business_key_duplicate")
        seen_keys.add(business_key)

        source_fields = row.get("source_fields")
        if not isinstance(source_fields, Mapping) or not _has_exact_keys(
            source_fields, _SOURCE_FIELD_KEYS
        ):
            _reject("source_fields_schema_invalid")
        date_ms = _require_date_ms(source_fields.get("date_ms"), trade_date)
        bar_values = {
            field: _require_finite_number(source_fields.get(field)) for field in _BAR_NUMERIC_FIELDS
        }
        _validate_ohlc(bar_values)

        source_records.append(
            {
                "thscode": symbol,
                "currency": None,
                "interval": None,
                "adjusted": UNADJUSTED,
                "date_ms": date_ms,
                **bar_values,
            }
        )
        source_values_by_date[trade_date] = {"date_ms": date_ms, **bar_values}

    source_frame = pd.DataFrame(source_records, columns=list(DAILY_K_COLUMNS))
    try:
        prepared = prepare_daily_k_frame(source_frame)
    except (DumpImportError, TypeError, ValueError, OverflowError):
        _reject("ods_mapping_failed")

    prepared = prepared.loc[:, list(ODS_STOCK_DAILY_COLUMNS)].sort_values(
        by=["thscode", "trade_date"], kind="stable", ignore_index=True
    )
    # Preserve each provider numeric scalar rather than allowing pandas to
    # coerce mixed integer/float source values through a float64 intermediary.
    ordered_source_values = [source_values_by_date[day] for day in sorted(source_values_by_date)]
    for field in ("date_ms", *_BAR_NUMERIC_FIELDS):
        prepared[field] = pd.Series(
            [values[field] for values in ordered_source_values],
            dtype="object",
        )
    prepared.attrs["field_units"] = dict(_EXPECTED_FIELD_UNITS)
    prepared.attrs["source_authenticity_status"] = "unverified"
    prepared.attrs["capture_time_status"] = "unknown"
    return prepared


def _validate_document_header(document: Mapping[str, object]) -> None:
    """Check the pilot document's fixed, reviewed structural metadata."""
    if type(document.get("schema_version")) is not int or document.get("schema_version") != 1:
        _reject("document_schema_invalid")
    if document.get("source") != SOURCE:
        _reject("document_source_label_invalid")
    if document.get("vendor") != "Fuyao (THS) REST API":
        _reject("document_vendor_label_invalid")
    if document.get("endpoint_path") != PRICES_ENDPOINT:
        _reject("document_endpoint_invalid")
    if document.get("time_field") != "date_ms":
        _reject("document_time_field_invalid")
    if document.get("time_semantics") != _TIME_SEMANTICS:
        _reject("document_time_semantics_invalid")
    units = document.get("field_units")
    if not isinstance(units, Mapping) or dict(units) != _EXPECTED_FIELD_UNITS:
        _reject("document_field_units_invalid")


def _has_exact_keys(value: Mapping[str, object], expected: frozenset[str]) -> bool:
    """Return whether a mapping contains exactly the reviewed string keys."""
    return all(type(key) is str for key in value) and set(value) == expected


def _require_symbol(value: object) -> str:
    """Validate a THS-shaped mainland A-share symbol without normalizing it."""
    if type(value) is not str or _SYMBOL_PATTERN.fullmatch(value) is None:
        _reject("instrument_symbol_invalid")
    return value


def _parse_iso_date(value: object, error_code: str) -> date:
    """Parse one canonical ISO calendar date without accepting coercions."""
    if type(value) is not str:
        _reject(error_code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _reject(error_code)
    if parsed.isoformat() != value:
        _reject(error_code)
    return parsed


def _require_date_ms(value: object, trade_date: date) -> int:
    """Require an integer Shanghai-midnight timestamp for its stated date."""
    if type(value) is not int:
        _reject("observation_date_ms_invalid")
    try:
        derived_date = millis_to_trading_date(value)
        expected_midnight = shanghai_midnight_millis(trade_date)
    except (OverflowError, OSError, ValueError):
        _reject("observation_date_ms_invalid")
    if derived_date != trade_date or value != expected_midnight:
        _reject("observation_date_ms_mismatch")
    return value


def _require_finite_number(value: object) -> int | float:
    """Keep an original integer or finite float without coercion."""
    if type(value) is int:
        return value
    if type(value) is not float or not math.isfinite(value):
        _reject("observation_numeric_value_invalid")
    return value


def _validate_ohlc(values: Mapping[str, int | float]) -> None:
    """Require positive prices and internally consistent high/low values."""
    open_price = values["open_price"]
    high = values["high_price"]
    low = values["low_price"]
    close = values["close_price"]
    volume = values["volume"]
    turnover = values["turnover"]
    if min(open_price, high, low, close) <= 0:
        _reject("observation_price_not_positive")
    if min(open_price, high, low, close, volume, turnover) < 0:
        _reject("observation_value_negative")
    if high < max(open_price, close) or low > min(open_price, close) or low > high:
        _reject("observation_ohlc_inconsistent")


def _reject(code: str) -> NoReturn:
    """Raise one stable adapter error without echoing untrusted values."""
    raise FuyaoStockDailyObservationError(code)


__all__ = [
    "ODS_STOCK_DAILY_COLUMNS",
    "FuyaoStockDailyObservationError",
    "prepare_fuyao_stock_daily_observations",
]
