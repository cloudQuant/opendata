"""Governed fixed-window pagination for strict FRED observation queries."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date as date_type
from typing import TYPE_CHECKING, Any, Generic, NoReturn, Protocol, TypeVar, cast

from pydantic import ValidationError

from opendata.data.http_client import HttpFetchError
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestExecutionScope,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    validate_scope_grants,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

_QueryT = TypeVar("_QueryT", bound=FredSeriesQuery)
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_REQUIRED_PAGE_FIELDS = frozenset(
    {
        "count",
        "offset",
        "limit",
        "units",
        "output_type",
        "file_type",
        "order_by",
        "sort_order",
        "realtime_start",
        "realtime_end",
        "observation_start",
        "observation_end",
        "observations",
    }
)
_OBSERVATION_FIELDS = frozenset({"date", "value", "realtime_start", "realtime_end"})


class _ResponseLike(Protocol):
    @property
    def status_code(self) -> object: ...

    @property
    def content(self) -> object: ...


class _HttpClientLike(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        timeout: float | None,
        source: str,
    ) -> _ResponseLike: ...


@dataclass(frozen=True, slots=True)
class FixedObservationPagerSpec(Generic[_QueryT]):
    """Immutable per-call identity, bounds, and dependency callbacks."""

    query_type: type[_QueryT]
    source: str
    canonical_model: str
    host: str
    url: str
    max_page_body_bytes: int
    max_total_body_bytes: int
    max_json_depth: int
    get_http_client: Callable[[], _HttpClientLike]
    require_api_key: Callable[[], object]
    json_loads: Callable[..., object]
    raise_query_limit: Callable[[str, int], NoReturn]


class _BadJsonError(ValueError):
    """Decoded JSON is not a safe ordinary JSON value."""


class _BodyLimitError(ValueError):
    """A response body or serialized page exceeds a fixed safety bound."""


def _raise_provider_error(
    spec: FixedObservationPagerSpec[Any],
    code: str,
    *,
    status: int | None = None,
) -> NoReturn:
    raise FredProviderError(code, status=status, url=spec.url) from None


def _raise_query_limit(
    spec: FixedObservationPagerSpec[Any],
    limit_kind: str,
    maximum: int,
) -> NoReturn:
    spec.raise_query_limit(limit_kind, maximum)


def validate_fixed_query(query: object, spec: FixedObservationPagerSpec[_QueryT]) -> _QueryT:
    """Require the exact query class and rebuild it from validated data."""
    if not isinstance(query, FredSeriesQuery) or type(query) is not spec.query_type:
        raise RequestAuthorizationError(
            f"FRED {spec.canonical_model} transport requires the exact query model"
        )
    try:
        payload = query.model_dump(mode="python")
        validated = spec.query_type.model_validate(payload)
    except (AttributeError, RecursionError, TypeError, ValueError, ValidationError):
        raise RequestAuthorizationError(
            f"FRED {spec.canonical_model} query is no longer valid"
        ) from None
    if type(validated) is not spec.query_type:
        raise RequestAuthorizationError(
            f"FRED {spec.canonical_model} transport requires the exact query model"
        )
    return validated


def _validate_active_scopes(
    spec: FixedObservationPagerSpec[Any],
) -> tuple[RequestExecutionScope, ...]:
    """Authorize every enclosing scope before credentials or transport access."""
    scopes = current_request_scopes()
    if not scopes:
        raise RequestAuthorizationError(
            f"FRED {spec.canonical_model} requests require an authorized execution scope"
        )
    if any(
        scope.source != spec.source or scope.canonical_model != spec.canonical_model
        for scope in scopes
    ):
        raise RequestAuthorizationError(
            f"FRED {spec.canonical_model} requests require the exact "
            f"{spec.source}/{spec.canonical_model} scope"
        )
    validate_scope_grants(scopes)
    authorize_request(scopes, spec.source, spec.host)
    return scopes


def _check_scopes_live(
    spec: FixedObservationPagerSpec[Any],
    scopes: tuple[RequestExecutionScope, ...],
) -> None:
    """Recheck cancellation, deadlines, grant identity, operation, and host."""
    check_scope_liveness(scopes)
    authorize_request(scopes, spec.source, spec.host)


def _canonical_date(value: object) -> date_type:
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        raise _BadJsonError("date is not canonical YYYY-MM-DD text")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise _BadJsonError("date is not a valid calendar date") from exc
    if parsed.isoformat() != value:
        raise _BadJsonError("date is not canonical YYYY-MM-DD text")
    return parsed


def _check_json_depth_text(text: str, *, maximum_depth: int) -> None:
    """Reject excessive container depth before calling json.loads."""
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > maximum_depth:
                raise _BadJsonError("JSON nesting exceeds the fixed depth limit")
        elif character in "]}":
            depth -= 1


def _ensure_json_tree(
    value: object,
    *,
    ancestors: set[int],
    container_depth: int,
    maximum_depth: int,
) -> None:
    """Require strict JSON-native values, finite numbers, and bounded depth."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _BadJsonError("JSON numbers must be finite")
        return
    if type(value) not in (dict, list):
        raise _BadJsonError("metadata contains a non-JSON value")
    depth = container_depth + 1
    if depth > maximum_depth:
        raise _BadJsonError("JSON nesting exceeds the fixed depth limit")
    identity = id(value)
    if identity in ancestors:
        raise _BadJsonError("JSON values must not contain cycles")
    ancestors.add(identity)
    try:
        if isinstance(value, dict):
            for key, item in value.items():
                if type(key) is not str:
                    raise _BadJsonError("JSON object keys must be strings")
                _ensure_json_tree(
                    item,
                    ancestors=ancestors,
                    container_depth=depth,
                    maximum_depth=maximum_depth,
                )
        elif isinstance(value, list):
            for item in value:
                _ensure_json_tree(
                    item,
                    ancestors=ancestors,
                    container_depth=depth,
                    maximum_depth=maximum_depth,
                )
        else:  # pragma: no cover - exact JSON container types are checked above
            raise _BadJsonError("metadata contains a non-JSON value")
    finally:
        ancestors.remove(identity)


def _serialized_page_size(page: object, spec: FixedObservationPagerSpec[Any]) -> int:
    try:
        _ensure_json_tree(
            page,
            ancestors=set(),
            container_depth=0,
            maximum_depth=spec.max_json_depth,
        )
        encoded = json.dumps(
            page,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, UnicodeError, ValueError):
        raise _BadJsonError("page metadata is not strictly JSON serializable") from None
    if len(encoded) > spec.max_page_body_bytes:
        raise _BodyLimitError("page exceeds the fixed body limit")
    return len(encoded)


def _reject_constant(value: str) -> NoReturn:
    raise _BadJsonError(f"non-finite JSON constant {value!r} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _BadJsonError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_page(
    body: bytes,
    *,
    status: int,
    spec: FixedObservationPagerSpec[Any],
) -> dict[str, Any]:
    try:
        text = body.decode("utf-8")
        _check_json_depth_text(text, maximum_depth=spec.max_json_depth)
        document = spec.json_loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if type(document) is not dict:
            raise _BadJsonError("page envelope must be a JSON object")
        _serialized_page_size(document, spec)
    except _BodyLimitError:
        _raise_provider_error(spec, "FRED_RESPONSE_LIMIT", status=status)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    return cast("dict[str, Any]", document)


def _request_page(
    params: dict[str, str],
    *,
    timeout: float | None,
    scopes: tuple[RequestExecutionScope, ...],
    total_body_bytes: int,
    spec: FixedObservationPagerSpec[Any],
) -> tuple[dict[str, Any], int, int]:
    """Fetch and decode one page through the central governed HTTP client."""
    _check_scopes_live(spec, scopes)
    try:
        response = spec.get_http_client().get(
            spec.url,
            params=params,
            timeout=timeout,
            source=spec.source,
        )
    except HttpFetchError as exc:
        _raise_provider_error(spec, "FRED_HTTP_ERROR", status=exc.status)
    _check_scopes_live(spec, scopes)

    status = response.status_code
    if type(status) is not int:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE")
    if status != 200:
        _raise_provider_error(spec, "FRED_HTTP_ERROR", status=status)
    body = response.content
    if type(body) is not bytes:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    body_size = len(body)
    if (
        body_size > spec.max_page_body_bytes
        or total_body_bytes + body_size > spec.max_total_body_bytes
    ):
        _raise_provider_error(spec, "FRED_RESPONSE_LIMIT", status=status)
    # GovHTTP may buffer response.content before this check; the bound applies
    # to parsing and retained pages, not to transport-layer streaming memory.
    page = _decode_page(body, status=status, spec=spec)
    _check_scopes_live(spec, scopes)
    return page, body_size, status


def _validate_page_header(
    page: dict[str, Any],
    query: FredSeriesQuery,
    *,
    requested_offset: int,
    requested_limit: int,
    expected_count: int | None,
    spec: FixedObservationPagerSpec[Any],
    status: int | None = None,
) -> tuple[int, list[Any]]:
    if type(page) is not dict or not _REQUIRED_PAGE_FIELDS.issubset(page):
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)

    count = page["count"]
    offset = page["offset"]
    limit = page["limit"]
    if (
        type(count) is not int
        or count < 0
        or type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or limit < 1
    ):
        _raise_provider_error(spec, "FRED_PAGINATION_ERROR", status=status)
    if offset != requested_offset or limit != requested_limit:
        _raise_provider_error(spec, "FRED_PAGINATION_ERROR", status=status)
    if expected_count is not None and count != expected_count:
        _raise_provider_error(spec, "FRED_PAGINATION_ERROR", status=status)

    if (
        type(page["units"]) is not str
        or page["units"] != query.transform_units
        or type(page["output_type"]) is not int
        or page["output_type"] != query.output_type
        or page["file_type"] != "json"
        or page["order_by"] != "observation_date"
        or page["sort_order"] != query.sort_order
    ):
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)

    try:
        realtime_start = _canonical_date(page["realtime_start"])
        realtime_end = _canonical_date(page["realtime_end"])
        observation_start = _canonical_date(page["observation_start"])
        observation_end = _canonical_date(page["observation_end"])
    except _BadJsonError:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    if realtime_start > realtime_end or observation_start > observation_end:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)

    if query.start_date is not None and observation_start != query.start_date:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    if query.end_date is not None and observation_end != query.end_date:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    if query.as_of is not None:
        if realtime_start != query.as_of or realtime_end != query.as_of:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    else:
        if query.realtime_start is not None and realtime_start != query.realtime_start:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
        if query.realtime_end is not None and realtime_end != query.realtime_end:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    if "series_id" in page and (
        type(page["series_id"]) is not str or page["series_id"] != query.series_id
    ):
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)

    observations = page["observations"]
    if type(observations) is not list:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
    return count, observations


def _page_window(page: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the four fixed query-window dates echoed by a validated page."""
    return (
        page["realtime_start"],
        page["realtime_end"],
        page["observation_start"],
        page["observation_end"],
    )


def _validate_observation(
    row: object,
    spec: FixedObservationPagerSpec[Any],
) -> tuple[dict[str, str], date_type, tuple[str, str, str]]:
    if type(row) is not dict or set(row) != _OBSERVATION_FIELDS:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE")
    if any(type(value) is not str for value in row.values()):
        _raise_provider_error(spec, "FRED_BAD_RESPONSE")
    try:
        observation_date = _canonical_date(row["date"])
        realtime_start = _canonical_date(row["realtime_start"])
        realtime_end = _canonical_date(row["realtime_end"])
    except _BadJsonError:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE")
    if realtime_start > realtime_end:
        _raise_provider_error(spec, "FRED_BAD_RESPONSE")
    return row, observation_date, (row["date"], row["realtime_start"], row["realtime_end"])


def _validate_rows(
    rows: Sequence[object],
    query: FredSeriesQuery,
    spec: FixedObservationPagerSpec[Any],
) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    previous_date: date_type | None = None
    by_identity: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw_row in rows:
        row, observation_date, identity = _validate_observation(raw_row, spec)
        if previous_date is not None:
            if query.sort_order == "asc" and observation_date < previous_date:
                _raise_provider_error(spec, "FRED_BAD_RESPONSE")
            if query.sort_order == "desc" and observation_date > previous_date:
                _raise_provider_error(spec, "FRED_BAD_RESPONSE")
        previous_date = observation_date
        prior = by_identity.get(identity)
        if prior is not None and prior != row:
            _raise_provider_error(spec, "FRED_PAGINATION_CONFLICT")
        if prior is None:
            by_identity[identity] = row
        result.append(dict(row))
    return tuple(result)


def _page_count(remaining: int, limit: int) -> int:
    return max(1, (remaining + limit - 1) // limit)


def validate_fixed_observation_pages(
    pages: object,
    query: object,
    *,
    spec: FixedObservationPagerSpec[_QueryT],
) -> tuple[dict[str, str], ...]:
    """Purely validate a complete page chain and flatten its original rows."""
    if type(pages) is not tuple or not pages:
        _raise_provider_error(spec, "FRED_PAGINATION_ERROR")

    validated_query = validate_fixed_query(query, spec)
    page_limit = min(validated_query.page_size, validated_query.max_records)
    serialized_total = 0
    for page in pages:
        try:
            page_size = _serialized_page_size(page, spec)
        except _BodyLimitError:
            _raise_provider_error(spec, "FRED_RESPONSE_LIMIT")
        except _BadJsonError:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE")
        serialized_total += page_size
        if serialized_total > spec.max_total_body_bytes:
            _raise_provider_error(spec, "FRED_RESPONSE_LIMIT")

    first_count, _ = _validate_page_header(
        pages[0],
        validated_query,
        requested_offset=validated_query.offset,
        requested_limit=page_limit,
        expected_count=None,
        spec=spec,
    )
    first_window = _page_window(pages[0])
    remaining = max(0, first_count - validated_query.offset)
    if remaining > validated_query.max_records:
        _raise_query_limit(spec, "max_records", validated_query.max_records)
    required_pages = _page_count(remaining, page_limit)
    if required_pages > validated_query.max_pages:
        _raise_query_limit(spec, "max_pages", validated_query.max_pages)
    if len(pages) != required_pages:
        _raise_provider_error(spec, "FRED_PAGINATION_ERROR")

    all_rows: list[object] = []
    for page_index, page in enumerate(pages):
        requested_offset = validated_query.offset + page_index * page_limit
        count, observations = _validate_page_header(
            page,
            validated_query,
            requested_offset=requested_offset,
            requested_limit=page_limit,
            expected_count=first_count,
            spec=spec,
        )
        if _page_window(page) != first_window:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE")
        expected_length = min(page_limit, max(0, count - requested_offset))
        if len(observations) != expected_length:
            _raise_provider_error(spec, "FRED_PAGINATION_ERROR")
        for observation in observations:
            _validate_observation(observation, spec)
        all_rows.extend(observations)
    return _validate_rows(all_rows, validated_query, spec)


def _request_params(
    query: FredSeriesQuery,
    api_key: str,
    *,
    limit: int,
    offset: int,
) -> dict[str, str]:
    params = {
        "series_id": query.series_id,
        "api_key": api_key,
        "file_type": "json",
        "units": query.transform_units,
        "output_type": str(query.output_type),
        "order_by": "observation_date",
        "sort_order": query.sort_order,
        "aggregation_method": query.aggregation_method,
        "limit": str(limit),
        "offset": str(offset),
    }
    if query.frequency is not None:
        params["frequency"] = query.frequency
    if query.vintage_dates is not None:
        params["vintage_dates"] = ",".join(item.isoformat() for item in query.vintage_dates)
    if query.start_date is not None:
        params["observation_start"] = query.start_date.isoformat()
    if query.end_date is not None:
        params["observation_end"] = query.end_date.isoformat()
    if query.as_of is not None:
        params["realtime_start"] = query.as_of.isoformat()
        params["realtime_end"] = query.as_of.isoformat()
    else:
        if query.realtime_start is not None:
            params["realtime_start"] = query.realtime_start.isoformat()
        if query.realtime_end is not None:
            params["realtime_end"] = query.realtime_end.isoformat()
    return params


def fetch_fixed_observation_pages(
    query: object,
    *,
    spec: FixedObservationPagerSpec[_QueryT],
    timeout: float | None,
    final_validate: Callable[[tuple[dict[str, Any], ...], _QueryT], object],
) -> tuple[dict[str, Any], ...]:
    """Fetch all bounded FRED pages after exact query and scope admission."""
    validated_query = validate_fixed_query(query, spec)
    scopes = _validate_active_scopes(spec)
    api_key = spec.require_api_key()
    if type(api_key) is not str or not api_key.strip():
        _raise_provider_error(spec, "FRED_API_KEY_MISSING")

    page_limit = min(validated_query.page_size, validated_query.max_records)
    pages: list[dict[str, Any]] = []
    total_body_bytes = 0
    expected_count: int | None = None
    expected_window: tuple[str, str, str, str] | None = None
    required_pages: int | None = None
    page_index = 0

    while required_pages is None or page_index < required_pages:
        offset = validated_query.offset + page_index * page_limit
        params = _request_params(validated_query, api_key, limit=page_limit, offset=offset)
        page, body_size, status = _request_page(
            params,
            timeout=timeout,
            scopes=scopes,
            total_body_bytes=total_body_bytes,
            spec=spec,
        )
        total_body_bytes += body_size
        count, observations = _validate_page_header(
            page,
            validated_query,
            requested_offset=offset,
            requested_limit=page_limit,
            expected_count=expected_count,
            spec=spec,
            status=status,
        )
        page_window = _page_window(page)
        if expected_window is None:
            expected_window = page_window
        elif page_window != expected_window:
            _raise_provider_error(spec, "FRED_BAD_RESPONSE", status=status)
        if expected_count is None:
            expected_count = count
            remaining = max(0, expected_count - validated_query.offset)
            if remaining > validated_query.max_records:
                _raise_query_limit(spec, "max_records", validated_query.max_records)
            required_pages = _page_count(remaining, page_limit)
            if required_pages > validated_query.max_pages:
                _raise_query_limit(spec, "max_pages", validated_query.max_pages)
        expected_length = min(page_limit, max(0, count - offset))
        if len(observations) != expected_length:
            _raise_provider_error(spec, "FRED_PAGINATION_ERROR", status=status)
        for observation in observations:
            _validate_observation(observation, spec)
        pages.append(page)
        page_index += 1
        _check_scopes_live(spec, scopes)

    result = tuple(pages)
    final_validate(result, validated_query)
    _check_scopes_live(spec, scopes)
    return result
