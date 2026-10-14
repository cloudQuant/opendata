"""Official New York Fed Primary Dealer API extraction and row validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import StrictStr, field_validator

from opendata.data.providers.federal_reserve._iter2_client import (
    FederalReserveSourceError,
    get_json,
)
from opendata.data.providers.federal_reserve.models._iter2_common import (
    FederalReserveDateQuery,
    parse_decimal,
)

_NYFED_BASE_URL = "https://markets.newyorkfed.org/api/pd"
_KEY_ID = re.compile(r"[A-Z0-9-]{1,80}\Z")
_MAX_KEY_IDS = 40


class PrimaryDealerQuery(FederalReserveDateQuery):
    """Date-bounded query over explicit New York Fed primary-dealer series ids."""

    key_ids: tuple[StrictStr, ...]

    @field_validator("key_ids", mode="before")
    @classmethod
    def _require_key_id_sequence(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is tuple:
            return value
        raise ValueError("key_ids must be a list or tuple")

    @field_validator("key_ids")
    @classmethod
    def _validate_key_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > _MAX_KEY_IDS:
            raise ValueError("key_ids must contain between 1 and 40 series ids")
        if len(set(value)) != len(value) or any(_KEY_ID.fullmatch(item) is None for item in value):
            raise ValueError("key_ids must be unique official series identifiers")
        return value


@dataclass(frozen=True)
class RawPrimaryDealerRecord:
    """One source row augmented with its official list/timeseries description."""

    as_of_date: str
    key_id: str
    value: str
    description: str


def fetch_primary_dealer_series(
    key_ids: tuple[str, ...],
    *,
    category: Literal["fails", "position"],
    timeout: float | None,
) -> tuple[RawPrimaryDealerRecord, ...]:
    """Validate requested ids against the official definition list and fetch their history.

    The New York Fed API returns all series-break history for a requested key id. Date-window
    filtering stays in the fetcher's normalization stage so the requested range remains inclusive.
    """
    definitions = get_json(f"{_NYFED_BASE_URL}/list/timeseries.json", timeout)
    definitions_root = definitions.get("pd")
    if not isinstance(definitions_root, dict):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DEFINITIONS")
    definition_rows = definitions_root.get("timeseries")
    if not isinstance(definition_rows, list):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DEFINITIONS")

    descriptions: dict[str, set[str]] = {key_id: set() for key_id in key_ids}
    for item in definition_rows:
        if not isinstance(item, dict):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DEFINITION_ROW")
        key_id = item.get("keyid")
        description = item.get("description")
        if key_id in descriptions:
            if not isinstance(description, str) or not description.strip():
                raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DEFINITION_ROW")
            descriptions[key_id].add(description.strip())

    selected_descriptions: dict[str, str] = {}
    expected_term = "FAIL" if category == "fails" else "POSITION"
    for key_id, candidates in descriptions.items():
        if not candidates:
            raise FederalReserveSourceError("FEDERAL_RESERVE_UNKNOWN_PD_KEY_ID")
        if len(candidates) != 1:
            raise FederalReserveSourceError("FEDERAL_RESERVE_AMBIGUOUS_PD_KEY_ID")
        description = next(iter(candidates))
        if expected_term not in description.upper():
            raise FederalReserveSourceError("FEDERAL_RESERVE_PD_CATEGORY_MISMATCH")
        selected_descriptions[key_id] = description

    path_ids = "_".join(key_ids)
    document = get_json(f"{_NYFED_BASE_URL}/get/{path_ids}.json", timeout)
    data_root = document.get("pd")
    if not isinstance(data_root, dict):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DATA")
    data_rows = data_root.get("timeseries")
    if not isinstance(data_rows, list):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DATA")

    records: list[RawPrimaryDealerRecord] = []
    for item in data_rows:
        if not isinstance(item, dict):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DATA_ROW")
        as_of_date = item.get("asofdate")
        key_id = item.get("keyid")
        value = item.get("value")
        if (
            not isinstance(as_of_date, str)
            or not isinstance(key_id, str)
            or key_id not in selected_descriptions
            or not isinstance(value, str)
        ):
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DATA_ROW")
        records.append(
            RawPrimaryDealerRecord(
                as_of_date=as_of_date,
                key_id=key_id,
                value=value,
                description=selected_descriptions[key_id],
            )
        )
    return tuple(records)


def normalize_primary_dealer_records(
    raw: tuple[RawPrimaryDealerRecord, ...],
    query: PrimaryDealerQuery,
    *,
    category: Literal["fails", "position"],
) -> tuple[dict[str, object], ...]:
    """Validate source dates/amounts, apply inclusive date bounds, and reject duplicate keys."""
    requested = set(query.key_ids)
    seen: set[tuple[str, date]] = set()
    rows: list[dict[str, object]] = []
    for item in raw:
        if type(item) is not RawPrimaryDealerRecord or item.key_id not in requested:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_PD_DATA_ROW")
        try:
            observation_date = date.fromisoformat(item.as_of_date)
        except ValueError:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE") from None
        if observation_date.isoformat() != item.as_of_date:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
        identity = (item.key_id, observation_date)
        if identity in seen:
            raise FederalReserveSourceError("FEDERAL_RESERVE_DUPLICATE_PD_ROW")
        seen.add(identity)
        amount = parse_decimal(item.value)
        if query.start_date is not None and observation_date < query.start_date:
            continue
        if query.end_date is not None and observation_date > query.end_date:
            continue
        value_field = "amount_usd_millions" if category == "fails" else "position_usd_millions"
        rows.append(
            {
                "date": observation_date,
                "key_id": item.key_id,
                "description": item.description,
                value_field: amount,
            }
        )
        if len(rows) > query.max_records:
            raise FederalReserveSourceError("FEDERAL_RESERVE_TOO_MANY_RECORDS")
    rows.sort(key=lambda row: (row["date"], row["key_id"]))
    return tuple(rows)


__all__ = [
    "PrimaryDealerQuery",
    "RawPrimaryDealerRecord",
    "fetch_primary_dealer_series",
    "normalize_primary_dealer_records",
]
