"""Bounded pagination for the FRED search and observation endpoints."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import opendata.core.config as config
from opendata.data.providers.fred.models import _client
from opendata.data.providers.fred.models._client import FredProviderError

if TYPE_CHECKING:
    from collections.abc import Callable


class FredQueryLimitError(FredProviderError):
    """A complete FRED result cannot fit inside the declared query budget."""

    def __init__(self, *, url: str, limit_kind: str, maximum: int) -> None:
        self.partial = True
        self.limit_kind = limit_kind
        self.maximum = maximum
        super().__init__("FRED_QUERY_LIMIT", url=url)


def fetch_series_search_pages(
    *,
    params: dict[str, str],
    offset: int,
    page_size: int,
    max_records: int,
    max_pages: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch all requested catalog pages, failing if a declared bound is hit."""
    return _fetch_pages(
        endpoint="/fred/series/search",
        collection_key="seriess",
        params=params,
        offset=offset,
        page_size=page_size,
        max_records=max_records,
        max_pages=max_pages,
        timeout=timeout,
        identity=lambda item: json.dumps(item.get("id"), sort_keys=True),
    )


def fetch_observation_pages(
    *,
    params: dict[str, str],
    offset: int,
    page_size: int,
    max_records: int,
    max_pages: int,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch all requested observation pages, preserving real-time revisions."""
    return _fetch_pages(
        endpoint="/fred/series/observations",
        collection_key="observations",
        params=params,
        offset=offset,
        page_size=page_size,
        max_records=max_records,
        max_pages=max_pages,
        timeout=timeout,
        identity=lambda item: json.dumps(
            [item.get("date"), item.get("realtime_start"), item.get("realtime_end")],
            sort_keys=True,
        ),
    )


def _fetch_pages(
    *,
    endpoint: str,
    collection_key: str,
    params: dict[str, str],
    offset: int,
    page_size: int,
    max_records: int,
    max_pages: int,
    timeout: float | None,
    identity: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    """Read a stable count/offset result set with explicit record/page caps."""
    api_key = _client.require_api_key()
    base_url = config.get_settings().fred_api_base_url or _client.FRED_DEFAULT_BASE_URL
    url = f"{base_url.rstrip('/')}{endpoint}"
    bounded_page_size = min(page_size, max_records)
    common_params = {**params, "api_key": api_key, "file_type": "json"}

    result: list[dict[str, Any]] = []
    seen_items: dict[str, dict[str, Any]] = {}
    requested_offsets: set[int] = set()
    response_offsets: set[int] = set()
    expected_count: int | None = None
    current_offset = offset
    pages_read = 0

    while True:
        if current_offset in requested_offsets:
            raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
        requested_offsets.add(current_offset)
        request_params = {
            **common_params,
            "limit": str(bounded_page_size),
            "offset": str(current_offset),
        }
        status, body = _client._http_get(url, request_params, timeout)
        if status != 200:
            raise FredProviderError("FRED_HTTP_ERROR", status=status, url=url)
        try:
            document = json.loads(body)
        except json.JSONDecodeError as exc:
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url) from exc
        if not isinstance(document, dict):
            raise FredProviderError("FRED_BAD_RESPONSE", status=status, url=url)

        count = _nonnegative_int(document.get("count"))
        response_offset = _nonnegative_int(document.get("offset"))
        page = document.get(collection_key)
        if (
            count is None
            or response_offset is None
            or response_offset != current_offset
            or response_offset in response_offsets
            or not isinstance(page, list)
            or any(not isinstance(item, dict) for item in page)
        ):
            raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)
        response_offsets.add(response_offset)

        if "limit" in document:
            response_limit = _nonnegative_int(document["limit"])
            if response_limit != bounded_page_size:
                raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)

        if expected_count is None:
            expected_count = count
            remaining = max(0, expected_count - offset)
            if remaining > max_records:
                raise FredQueryLimitError(url=url, limit_kind="max_records", maximum=max_records)
            pages_required = (remaining + bounded_page_size - 1) // bounded_page_size
            if pages_required > max_pages:
                raise FredQueryLimitError(url=url, limit_kind="max_pages", maximum=max_pages)
        elif count != expected_count:
            raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)

        expected_page_size = min(bounded_page_size, max(0, expected_count - current_offset))
        if len(page) != expected_page_size:
            raise FredProviderError("FRED_PAGINATION_ERROR", status=status, url=url)

        pages_read += 1
        if pages_read > max_pages:
            raise FredQueryLimitError(url=url, limit_kind="max_pages", maximum=max_pages)

        for item in page:
            record = item  # The list was checked to contain only JSON objects above.
            item_key = identity(record)
            previous = seen_items.get(item_key)
            if previous is not None:
                if previous != record:
                    raise FredProviderError("FRED_PAGINATION_CONFLICT", url=url)
                continue
            seen_items[item_key] = record
            result.append(record)

        next_offset = current_offset + expected_page_size
        if next_offset >= expected_count:
            return result
        if next_offset <= current_offset:
            raise FredProviderError("FRED_PAGINATION_ERROR", url=url)
        current_offset = next_offset


def _nonnegative_int(value: object) -> int | None:
    """Accept only JSON integers, not bools or numeric strings."""
    if type(value) is not int or value < 0:
        return None
    return value
