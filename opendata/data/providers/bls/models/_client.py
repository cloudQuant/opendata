"""Governed transport and strict parsing for BLS public data files and API."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

BLS_DOWNLOAD_BASE_URL = "https://download.bls.gov/pub/time.series"
BLS_API_V1_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_API_V2_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
MAX_CATALOG_BYTES = 32 * 1024 * 1024
_SURVEY_RE = re.compile(r"^[a-z]{2}$")


class BlsProviderError(RuntimeError):
    """Stable, credential-safe BLS model failures."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
        incomplete: bool = False,
    ) -> None:
        """Store only stable fields; never copy upstream body or key text."""
        self.code = code
        self.status = status
        self.url = _safe_endpoint(url) if url is not None else None
        self.incomplete = incomplete
        detail = "" if status is None else f" status={status}"
        detail += "" if self.url is None else f" url={self.url}"
        super().__init__(code + detail)


class BlsRequestBudgetError(BlsProviderError):
    """The full requested series/year matrix exceeds its explicit budget."""

    def __init__(self, *, required_requests: int, max_requests: int) -> None:
        """Report the exact request count without sending a partial query."""
        self.required_requests = required_requests
        self.max_requests = max_requests
        super().__init__("BLS_REQUEST_BUDGET_EXCEEDED", incomplete=True)


@dataclass(frozen=True)
class BlsRawCatalogPage:
    """Parsed source rows for one bounded catalog window."""

    items: tuple[dict[str, object], ...]
    total: int
    offset: int
    limit: int


def _safe_endpoint(url: str) -> str:
    """Remove any query and fragment from a URL used in an error message."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _http_get(url: str, timeout: float | None) -> tuple[int, bytes]:
    """Use the shared governed HTTP client for a BLS bulk-file GET."""
    try:
        response = get_shared_http_client().get(
            url,
            timeout=timeout if timeout is not None else 30.0,
            source="bls",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise BlsProviderError("BLS_HTTP_ERROR", status=exc.status, url=exc.url) from None
    except Exception:
        raise BlsProviderError("BLS_HTTP_ERROR", url=url) from None
    return response.status_code, response.content


def _http_post_json(
    url: str,
    payload: dict[str, object],
    timeout: float | None,
) -> tuple[int, str]:
    """Use the shared governed client for one BLS JSON POST."""
    try:
        response = get_shared_http_client().request(
            "POST",
            url,
            headers={"Content-Type": "application/json"},
            timeout=timeout if timeout is not None else 30.0,
            source="bls",
            json_body=payload,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise BlsProviderError(
            "BLS_HTTP_ERROR", status=exc.status, url=exc.url, incomplete=True
        ) from None
    except Exception:
        # An unexpected transport exception can contain the request body.
        raise BlsProviderError("BLS_HTTP_ERROR", url=url, incomplete=True) from None
    return response.status_code, response.text


def fetch_catalog(
    *,
    survey: str,
    search_text: str | None,
    offset: int,
    limit: int,
    timeout: float | None,
) -> BlsRawCatalogPage:
    """Read a survey's full `.series` TSV, then return a bounded match page."""
    if _SURVEY_RE.fullmatch(survey) is None:
        raise BlsProviderError("BLS_INVALID_SURVEY")
    url = f"{BLS_DOWNLOAD_BASE_URL}/{survey}/{survey}.series"
    status, body = _http_get(url, timeout)
    if status != 200:
        raise BlsProviderError("BLS_HTTP_ERROR", status=status, url=url)
    if not isinstance(body, bytes):
        raise BlsProviderError("BLS_CATALOG_BAD_RESPONSE", status=status, url=url)
    if len(body) > MAX_CATALOG_BYTES:
        raise BlsProviderError("BLS_CATALOG_TOO_LARGE", status=status, url=url)
    if not body:
        raise BlsProviderError("BLS_CATALOG_EMPTY", status=status, url=url)
    if body.startswith(b"\xef\xbb\xbf"):
        raise BlsProviderError("BLS_CATALOG_BOM", status=status, url=url)

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise BlsProviderError("BLS_CATALOG_BAD_RESPONSE", status=status, url=url) from None
    if text.lstrip().startswith("<"):
        raise BlsProviderError("BLS_CATALOG_HTML", status=status, url=url)

    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t", strict=True)
        raw_headers = next(reader, None)
        if not raw_headers:
            raise BlsProviderError("BLS_CATALOG_BAD_TSV", status=status, url=url)
        headers = [header.strip().lower() for header in raw_headers]
        if (
            any(not header for header in headers)
            or len(set(headers)) != len(headers)
            or "series_id" not in headers
        ):
            raise BlsProviderError("BLS_CATALOG_BAD_HEADER", status=status, url=url)

        id_index = headers.index("series_id")
        title_index = headers.index("series_title") if "series_title" in headers else None
        source_metadata = {"source_file": f"{survey}.series", "source_url": url}
        parsed_by_id: dict[str, dict[str, object]] = {}
        for row in reader:
            if not row or all(not field.strip() for field in row):
                continue
            if len(row) != len(headers):
                raise BlsProviderError("BLS_CATALOG_BAD_TSV", status=status, url=url)
            values = [field.strip() for field in row]
            if any(_has_control(value) for value in values):
                raise BlsProviderError("BLS_CATALOG_BAD_TSV", status=status, url=url)
            series_id = values[id_index]
            if not series_id or _has_control(series_id):
                raise BlsProviderError("BLS_CATALOG_MISSING_ID", status=status, url=url)
            title = values[title_index] if title_index is not None else ""
            dimensions = {
                header: values[index]
                for index, header in enumerate(headers)
                if header not in {"series_id", "series_title"}
            }
            record: dict[str, object] = {
                "series_id": series_id,
                "survey": survey.upper(),
                "title": title or None,
                "frequency": _first_nonempty(dimensions, ("frequency",)),
                "units": _first_nonempty(dimensions, ("units", "unit", "measure_unit")),
                "dimensions": dimensions,
                "source_metadata": source_metadata,
                # BLS provides no vintage/as-of value for this catalog file.
                "catalog_as_of": None,
            }
            previous = parsed_by_id.get(series_id)
            if previous is not None and previous != record:
                raise BlsProviderError("BLS_CATALOG_DUPLICATE_CONFLICT", status=status, url=url)
            parsed_by_id[series_id] = record
    except BlsProviderError:
        raise
    except (csv.Error, StopIteration, ValueError):
        raise BlsProviderError("BLS_CATALOG_BAD_TSV", status=status, url=url) from None

    if not parsed_by_id:
        raise BlsProviderError("BLS_CATALOG_EMPTY", status=status, url=url)

    needle = search_text.casefold() if search_text is not None else None
    matches = [
        item
        for item in parsed_by_id.values()
        if needle is None
        or needle in str(item["series_id"]).casefold()
        or (item["title"] is not None and needle in str(item["title"]).casefold())
    ]
    total = len(matches)
    return BlsRawCatalogPage(
        items=tuple(matches[offset : offset + limit]),
        total=total,
        offset=offset,
        limit=limit,
    )


def fetch_observations(
    *,
    series_ids: tuple[str, ...],
    start_year: int,
    end_year: int,
    max_requests: int,
    timeout: float | None,
) -> list[dict[str, object]]:
    """Fetch the complete series/year matrix within official BLS limits."""
    api_key = _api_key_from_environment()
    api_version: Literal["v1", "v2"] = "v2" if api_key is not None else "v1"
    series_limit, year_limit, daily_limit = (50, 20, 500) if api_key is not None else (25, 10, 25)
    series_batches = list(_chunks(series_ids, series_limit))
    year_windows = list(_year_windows(start_year, end_year, year_limit))
    required_requests = len(series_batches) * len(year_windows)
    if required_requests > max_requests or required_requests > daily_limit:
        raise BlsRequestBudgetError(
            required_requests=required_requests,
            max_requests=min(max_requests, daily_limit),
        )

    url = BLS_API_V2_URL if api_key is not None else BLS_API_V1_URL
    observations_by_key: dict[tuple[str, str, str], dict[str, object]] = {}
    for batch_ids in series_batches:
        for window_start, window_end in year_windows:
            payload: dict[str, object] = {
                "seriesid": list(batch_ids),
                "startyear": str(window_start),
                "endyear": str(window_end),
            }
            if api_key is not None:
                payload["registrationkey"] = api_key
            status, body = _http_post_json(url, payload, timeout)
            if status != 200:
                raise BlsProviderError("BLS_HTTP_ERROR", status=status, url=url, incomplete=True)
            document = _decode_json(body, url=url)
            response_rows = _parse_api_response(
                document,
                expected_ids=batch_ids,
                start_year=window_start,
                end_year=window_end,
                api_version=api_version,
                url=url,
            )
            for row in response_rows:
                key = (str(row["series_id"]), str(row["year"]), str(row["period"]))
                previous = observations_by_key.get(key)
                if previous is not None and previous != row:
                    raise BlsProviderError("BLS_OBSERVATION_CONFLICT", url=url, incomplete=True)
                if previous is None:
                    observations_by_key[key] = row
    return list(observations_by_key.values())


def _api_key_from_environment() -> str | None:
    """Read only the explicitly named BLS key; blank values select v1."""
    value = os.environ.get("BLS_API_KEY")
    if value is None or not value.strip():
        return None
    return value.strip()


def _chunks(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    """Split a tuple into nonempty fixed-size request groups."""
    return [values[index : index + size] for index in range(0, len(values), size)]


def _year_windows(start_year: int, end_year: int, max_years: int) -> list[tuple[int, int]]:
    """Split inclusive years without overlap."""
    windows: list[tuple[int, int]] = []
    current = start_year
    while current <= end_year:
        window_end = min(end_year, current + max_years - 1)
        windows.append((current, window_end))
        current = window_end + 1
    return windows


def _decode_json(body: str, *, url: str) -> dict[str, Any]:
    """Decode an API JSON object without retaining raw upstream text in errors."""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True) from None
    if not isinstance(document, dict):
        raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
    return document


def _parse_api_response(
    document: dict[str, Any],
    *,
    expected_ids: tuple[str, ...],
    start_year: int,
    end_year: int,
    api_version: Literal["v1", "v2"],
    url: str,
) -> list[dict[str, object]]:
    """Validate the official POST response shape and flatten observations."""
    status = document.get("status")
    messages = document.get("message")
    if not isinstance(messages, list) or any(not isinstance(message, str) for message in messages):
        raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
    if any(_is_invalid_series_message(message) for message in messages):
        raise BlsProviderError("BLS_INVALID_SERIES_ID", url=url, incomplete=True)
    if status != "REQUEST_SUCCEEDED":
        raise BlsProviderError("BLS_API_STATUS", url=url, incomplete=True)
    if messages:
        # Unknown source messages cannot be treated as a valid empty window.
        raise BlsProviderError("BLS_API_MESSAGE", url=url, incomplete=True)

    results = document.get("Results")
    result_groups: list[Any] | tuple[dict[str, Any], ...]
    if isinstance(results, dict):
        result_groups = (results,)
    elif isinstance(results, list):
        result_groups = results
    else:
        raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
    raw_series: dict[str, list[dict[str, Any]]] = {}
    for result_group in result_groups:
        if not isinstance(result_group, dict) or not isinstance(result_group.get("series"), list):
            raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
        for series in result_group["series"]:
            if not isinstance(series, dict):
                raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
            series_id = series.get("seriesID")
            data = series.get("data")
            if not isinstance(series_id, str) or not series_id or not isinstance(data, list):
                raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
            if series_id not in expected_ids or any(not isinstance(item, dict) for item in data):
                raise BlsProviderError("BLS_BAD_RESPONSE", url=url, incomplete=True)
            previous_data = raw_series.get(series_id)
            if previous_data is not None and previous_data != data:
                raise BlsProviderError("BLS_SERIES_CONFLICT", url=url, incomplete=True)
            raw_series[series_id] = data

    if set(raw_series) != set(expected_ids):
        raise BlsProviderError("BLS_MISSING_SERIES", url=url, incomplete=True)

    rows: list[dict[str, object]] = []
    for series_id in expected_ids:
        seen_observations: dict[tuple[str, str], dict[str, Any]] = {}
        for raw_observation in raw_series[series_id]:
            year = raw_observation.get("year")
            period = raw_observation.get("period")
            if (
                not isinstance(year, str)
                or re.fullmatch(r"\d{4}", year) is None
                or not start_year <= int(year) <= end_year
                or not isinstance(period, str)
                or not period.strip()
            ):
                raise BlsProviderError("BLS_BAD_OBSERVATION", url=url, incomplete=True)
            identity = (year, period)
            previous_observation = seen_observations.get(identity)
            if previous_observation is not None and previous_observation != raw_observation:
                raise BlsProviderError("BLS_OBSERVATION_CONFLICT", url=url, incomplete=True)
            if previous_observation is not None:
                continue
            seen_observations[identity] = raw_observation
            period_name = raw_observation.get("periodName")
            value = raw_observation.get("value")
            footnotes = raw_observation.get("footnotes")
            latest = raw_observation.get("latest")
            if (
                not isinstance(period_name, str)
                or not period_name.strip()
                or not isinstance(value, str)
                or not isinstance(footnotes, list)
                or any(not isinstance(note, dict) for note in footnotes)
                or not _valid_latest(latest)
            ):
                raise BlsProviderError("BLS_BAD_OBSERVATION", url=url, incomplete=True)
            for note in footnotes:
                if any(
                    key in note and note[key] is not None and not isinstance(note[key], str)
                    for key in ("code", "text")
                ):
                    raise BlsProviderError("BLS_BAD_OBSERVATION", url=url, incomplete=True)
            rows.append(
                {
                    "series_id": series_id,
                    "year": year,
                    "period": period,
                    "period_name": period_name,
                    "value": value,
                    "footnotes": footnotes,
                    "latest": _parse_latest(latest),
                    "api_version": api_version,
                }
            )
    return rows


def _is_invalid_series_message(message: str) -> bool:
    """Recognize the invalid-ID message documented by the BLS FAQ."""
    normalized = message.casefold()
    return "invalid series for series" in normalized or "series does not exist" in normalized


def _valid_latest(value: object) -> bool:
    """Accept the v2 latest marker only when it is a documented boolean form."""
    return value is None or isinstance(value, bool) or value in ("true", "false")


def _parse_latest(value: object) -> bool | None:
    """Map a present latest marker without inferring it from observation order."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return value == "true"


def _has_control(value: str) -> bool:
    """Check for characters that can break catalog TSV record boundaries."""
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _first_nonempty(values: dict[str, str], keys: tuple[str, ...]) -> str | None:
    """Select a normalized catalog attribute from known survey headers."""
    for key in keys:
        value = values.get(key, "").strip()
        if value:
            return value
    return None
