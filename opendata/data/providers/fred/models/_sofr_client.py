"""Thin SOFR adapter for the shared governed FRED observation pager."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from opendata.data.http_client import get_shared_http_client
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._fixed_observation_pages import (
    FixedObservationPagerSpec,
    fetch_fixed_observation_pages,
    validate_fixed_observation_pages,
    validate_fixed_query,
)
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery

_SOURCE = "fred"
_CANONICAL_MODEL = "SOFR"
_HOST = "api.stlouisfed.org"
_URL = f"https://{_HOST}/fred/series/observations"
_MAX_PAGE_BODY_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BODY_BYTES = 256 * 1024 * 1024
_MAX_JSON_DEPTH = 64


class _SofrQueryLimitError(_client.FredProviderError):
    """The complete response cannot fit within the SOFR query's declared limits."""

    def __init__(self, limit_kind: str, maximum: int) -> None:
        super().__init__("FRED_QUERY_LIMIT", url=_URL)
        self.limit_kind = limit_kind
        self.maximum = maximum
        self.partial = False


def _raise_query_limit(limit_kind: str, maximum: int) -> NoReturn:
    raise _SofrQueryLimitError(limit_kind, maximum) from None


def _pager_spec() -> FixedObservationPagerSpec[FredSofrQuery]:
    """Build an immutable call-local spec from the existing SOFR test seams."""
    return FixedObservationPagerSpec(
        query_type=FredSofrQuery,
        source=_SOURCE,
        canonical_model=_CANONICAL_MODEL,
        host=_HOST,
        url=_URL,
        max_page_body_bytes=_MAX_PAGE_BODY_BYTES,
        max_total_body_bytes=_MAX_TOTAL_BODY_BYTES,
        max_json_depth=_MAX_JSON_DEPTH,
        get_http_client=get_shared_http_client,
        require_api_key=_client.require_api_key,
        json_loads=json.loads,
        raise_query_limit=_raise_query_limit,
    )


def _validate_query(query: FredSofrQuery) -> FredSofrQuery:
    """Preserve the SOFR query validation seam."""
    return validate_fixed_query(query, _pager_spec())


def _validate_pages(
    pages: tuple[dict[str, Any], ...],
    query: FredSofrQuery,
) -> tuple[dict[str, Any], ...]:
    """Preserve the SOFR final-validation seam used by existing callers."""
    return validate_fixed_observation_pages(pages, query, spec=_pager_spec())


def validate_sofr_pages(
    pages: tuple[dict[str, Any], ...],
    query: FredSofrQuery,
) -> tuple[dict[str, Any], ...]:
    """Purely validate a complete page chain and flatten its original rows."""
    return _validate_pages(pages, query)


def fetch_sofr_pages(
    query: FredSofrQuery,
    *,
    timeout: float | None = None,
) -> tuple[dict[str, Any], ...]:
    """Fetch the complete bounded set of FRED SOFR page envelopes."""
    return fetch_fixed_observation_pages(
        query,
        spec=_pager_spec(),
        timeout=timeout,
        final_validate=_validate_pages,
    )


__all__ = ("fetch_sofr_pages", "validate_sofr_pages")
