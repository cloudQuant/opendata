"""Typed adapter for Benzinga's documented analyst Ratings endpoint."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date as date_type
from datetime import datetime as datetime_type
from datetime import time as time_type
from datetime import timezone
from typing import TYPE_CHECKING

from opendata.data.capability import Capability
from opendata.data.models.benzinga import BenzingaPriceTarget
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    ProviderEngineQueryError,
    build_query_model,
    fetch_pages,
    resolve_rows,
)
from opendata.data.providers.benzinga._source import SOURCE
from opendata.data.providers.benzinga.models.iter2 import BenzingaAPIModelFetcher, _symbols
from opendata.data.providers.benzinga.specs import PRICE_TARGET as _REQUEST_PLAN

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opendata.data.protocol import FetchContext

# The model declaration supplies request parameters, URL, credentials, and row envelope only.
# Its generated row projection is intentionally inactive; this adapter owns the typed output map.
_QUERY_MODEL = build_query_model(_REQUEST_PLAN, source=SOURCE, market=_REQUEST_PLAN.market)
_CAPABILITY = Capability(
    source=SOURCE,
    domain=_REQUEST_PLAN.domain,
    asset_class=_REQUEST_PLAN.asset_class,
    period=_REQUEST_PLAN.period,
    market=_REQUEST_PLAN.market,
    verified=False,
    notes="Official Benzinga Ratings response contract; no live validation.",
)


def _text(record: Mapping[str, object], key: str) -> str | None:
    """Read a documented text cell, treating only the published empty spelling as null."""
    value = record.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return value


def _number(record: Mapping[str, object], key: str) -> float | None:
    """Read a finite price cell whose documented JSON representation may be numeric text."""
    value = record.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{key} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{key} must be finite")
    return parsed


def _integer(record: Mapping[str, object], key: str) -> int | None:
    """Read an integer field without accepting booleans as numbers."""
    value = record.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _published_date(record: Mapping[str, object]) -> date_type | None:
    """Parse the endpoint's ISO calendar date without adding timestamp semantics."""
    value = record.get("date")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("date must be ISO text")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be ISO text") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical ISO text")
    return parsed


def _published_time(record: Mapping[str, object]) -> time_type | None:
    """Parse the documented bare clock value and preserve its timezone-unknown status."""
    value = record.get("time")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("time must be text")
    try:
        parsed = time_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("time must be ISO clock text") from exc
    if parsed.tzinfo is not None or parsed.isoformat() != value:
        raise ValueError("time must be a canonical timezone-free clock value")
    return parsed


def _updated_at(record: Mapping[str, object]) -> datetime_type | None:
    """Convert the source's integer Unix seconds to a timezone-aware UTC instant."""
    value = record.get("updated")
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("updated must be non-negative Unix seconds")
    try:
        return datetime_type.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("updated is outside the supported timestamp range") from exc


class BenzingaPriceTargetFetcher(BenzingaAPIModelFetcher):
    """Fetch bounded Ratings pages and map each official record into the typed contract."""

    canonical_model = _REQUEST_PLAN.model
    capability = _CAPABILITY
    query_model = _QUERY_MODEL
    row_model = BenzingaPriceTarget
    max_rows = 1000

    def transform_query(self, **kwargs: object) -> BaseModel:
        """Keep the declared query shape and enforce the documented bounded page window."""
        if isinstance(kwargs.get("limit"), bool):
            raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("limit",))
        params = super().transform_query(**kwargs)
        symbol = getattr(params, "symbol", None)
        if symbol is not None:
            if not isinstance(symbol, str):
                raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("symbol",))
            _symbols(symbol)
        limit = getattr(params, "limit", None)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ProviderEngineQueryError("BENZINGA_QUERY_INVALID", invalid=("limit",))
        start_date = getattr(params, "start_date", None)
        end_date = getattr(params, "end_date", None)
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ProviderEngineQueryError(
                "BENZINGA_QUERY_INVALID", invalid=("start_date", "end_date")
            )
        return params

    def extract_data(self, params: BaseModel, ctx: FetchContext) -> list[dict[str, object]]:
        """Use the existing request plan for governed fetches, then return its source rows."""
        if type(params) is not _QUERY_MODEL:
            raise ProviderEngineError("BENZINGA_QUERY_INVALID")
        pages = fetch_pages(
            _REQUEST_PLAN,
            params,
            ctx,
            transport=self.http_transport,
            source=SOURCE,
        )
        records: list[dict[str, object]] = []
        for page in pages:
            page_records = resolve_rows(page, _REQUEST_PLAN.rows_pointer, _REQUEST_PLAN)
            if any(not isinstance(record, Mapping) for record in page_records):
                raise ProviderEngineError("BENZINGA_SHAPE_INVALID")
            records.extend(page_records)
            if len(records) > self.max_rows:
                raise ProviderEngineError("BENZINGA_INCOMPLETE")
        return records

    def map_record(self, item: Mapping[str, object], params: object) -> dict[str, object]:
        """Map documented Ratings keys to the stable contract, preserving units and nulls."""
        del params
        return {
            "symbol": _text(item, "ticker"),
            "published_date": _published_date(item),
            "published_time": _published_time(item),
            "exchange": _text(item, "exchange"),
            "analyst_name": _text(item, "analyst_name"),
            "analyst_firm": _text(item, "analyst"),
            "analyst_id": _text(item, "analyst_id"),
            "company_name": _text(item, "name"),
            "currency": _text(item, "currency"),
            "action": _text(item, "action_company"),
            "action_change": _text(item, "action_pt"),
            "rating_current": _text(item, "rating_current"),
            "rating_previous": _text(item, "rating_prior"),
            "importance": _integer(item, "importance"),
            "price_target": _number(item, "pt_current"),
            "price_target_previous": _number(item, "pt_prior"),
            "adj_price_target": _number(item, "adjusted_pt_current"),
            "previous_adj_price_target": _number(item, "adjusted_pt_prior"),
            # The documented Ratings record has no source-backed price_when_posted field.
            "price_when_posted": None,
            "notes": _text(item, "notes"),
            "url_news": _text(item, "url_news"),
            # url_calendar is the ticker's analyst-ratings page in the official response sample.
            "url_analyst": _text(item, "url_calendar"),
            "id": _text(item, "id"),
            "last_updated": _updated_at(item),
        }
