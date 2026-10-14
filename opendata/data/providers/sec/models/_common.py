"""Bounded SEC transport, identity parsing, and stable provider errors."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import (
    HttpGet,
    HttpResponse,
    ProviderEngineError,
)
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestBudgetError,
    current_request_scopes,
)

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

SOURCE = "sec"
SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions"
SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts"
SEC_FRAMES_URL = "https://data.sec.gov/api/xbrl/frames"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_JSON_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_TEXT_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_XML_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TICKER_DIRECTORY_ROWS = 50_000
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TICKER_RE = re.compile(r"^[A-Za-z0-9.-]{1,20}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SEC_RATE_LOCK = threading.Lock()
_SEC_LAST_REQUEST_AT: float | None = None
_SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.2


class SecProviderError(ProviderEngineError):
    """Sanitized SEC failure code; never includes an upstream body or contact value.

    SEC refusals are attributable engine refusals: they carry the stable ``code``, the
    response ``status`` when one was observed, and the sanitized ``url`` of the request that
    produced them. SEC parsing refuses a whole source record (a corrupt XBRL observation, a
    malformed directory row), so the column-detail faces stay empty by design and the code is
    the attribution.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
        rejected: Sequence[str] = (),
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        """Store only stable fields; never copy a body, header, or contact value."""
        super().__init__(
            code,
            status=status,
            url=_safe_endpoint(url) if url is not None else None,
            rejected=rejected,
            missing=missing,
            invalid=invalid,
        )


class SecRequestAttemptLimitError(SecProviderError, RequestAttemptLimitError):
    """SEC-specific bounded request exhaustion recognized by service budgets."""


def _safe_endpoint(url: str) -> str:
    """Drop any query and fragment from a URL that a failure message may repeat."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def require_user_agent() -> str:
    """Require a recognizable product name and contact before any SEC request."""
    value = os.environ.get(SEC_USER_AGENT_ENV, "").strip()
    if (
        not 10 <= len(value) <= 200
        or _CONTROL_RE.search(value) is not None
        or _EMAIL_RE.search(value) is None
        or value.casefold().startswith("my real company name")
    ):
        raise SecProviderError("SEC_USER_AGENT_MISSING_OR_INVALID")
    return value


def ensure_request_capacity(ctx: FetchContext, request_count: int) -> None:
    """Refuse a known multi-stage fan-out before its first request."""
    if isinstance(request_count, bool) or not isinstance(request_count, int) or request_count < 0:
        raise SecProviderError("SEC_REQUEST_BOUND_INVALID")
    scopes = current_request_scopes()
    budget = ctx.request_budget
    if budget is None and scopes:
        budget = scopes[-1].budget
    if budget is None:
        return
    remaining_task = budget.task_attempt_limit - budget.attempts_used
    remaining_source = budget.source_attempt_limit - budget.attempts_used_for(SOURCE)
    if request_count > remaining_task or request_count > remaining_source:
        raise SecRequestAttemptLimitError("SEC_REQUEST_BUDGET_EXCEEDED")
    for scope in scopes:
        if scope.source != SOURCE:
            continue
        grant = next(
            (
                item
                for item in budget.grants
                if item.source == scope.source
                and item.canonical_model == scope.canonical_model
                and item.operation is scope.operation
            ),
            None,
        )
        if grant is None:
            raise SecProviderError("SEC_REQUEST_BUDGET_EXCEEDED")
        grant_remaining_task = grant.task_attempts - budget.attempts_used
        grant_remaining_source = grant.source_attempts - budget.attempts_used_for(SOURCE)
        if request_count > min(grant_remaining_task, grant_remaining_source):
            raise SecRequestAttemptLimitError("SEC_REQUEST_BUDGET_EXCEEDED")


def _seam_body(response: HttpResponse) -> bytes:
    """Return the body bytes the governed client would have handed back for one seam response.

    The governed client hands SEC parsers ``content`` bytes; the repository's offline witness
    seam has no raw-text carrier, so it supplies the same document losslessly: as ``payload``,
    as the list of a text file's own source lines, or as the JSON document re-encoded to the
    bytes a JSON response would have had.
    """
    if isinstance(response.payload, bytes):
        return response.payload
    if isinstance(response.payload, str):
        return response.payload.encode("utf-8")
    document = response.document
    if isinstance(document, bytes):
        return document
    if isinstance(document, str):
        return document.encode("utf-8")
    if isinstance(document, list) and all(isinstance(line, str) for line in document):
        return "\n".join(document).encode("utf-8")
    return json.dumps(document).encode("utf-8")


def fetch_bytes(
    ctx: FetchContext,
    url: str,
    *,
    max_bytes: int = MAX_TEXT_RESPONSE_BYTES,
    use_cache: bool = True,
    transport: HttpGet | None = None,
) -> bytes:
    """Read one bounded SEC response through the governed client or an offline witness seam.

    ``transport`` is the repository's own offline seam: a fetcher that receives one answers
    every request from it instead of the governed HTTP client, and no request leaves the
    process. The shared client path is unchanged.
    """
    if type(use_cache) is not bool:
        raise TypeError("use_cache must be a bool")
    headers = {"User-Agent": require_user_agent(), "Accept-Encoding": "gzip, deflate"}
    if transport is not None:
        # A seam answer never reaches SEC's servers, so the pacing cap does not apply to it.
        response = transport(
            url,
            {},
            timeout=ctx.timeout or DEFAULT_TIMEOUT_SECONDS,
            source=SOURCE,
            headers=headers,
        )
        status = response.status
        body = _seam_body(response)
    else:
        try:
            _pace_sec_request()
            response = get_shared_http_client().get(
                url,
                headers=headers,
                timeout=ctx.timeout or DEFAULT_TIMEOUT_SECONDS,
                source=SOURCE,
                use_cache=use_cache,
            )
        except RequestBudgetError:
            raise
        except HttpFetchError as exc:
            raise SecProviderError(_http_error_code(exc.status), status=exc.status) from None
        except Exception:
            raise SecProviderError("SEC_TRANSPORT_ERROR") from None
        status = cast("int", getattr(response, "status_code", None))
        if isinstance(status, bool) or not isinstance(status, int):
            raise SecProviderError("SEC_TRANSPORT_ERROR")
        body = cast("bytes", getattr(response, "content", None))
    if not 200 <= status < 300:
        raise SecProviderError(_http_error_code(status), status=status)
    if not isinstance(body, bytes):
        raise SecProviderError("SEC_BAD_RESPONSE")
    if not body:
        raise SecProviderError("SEC_EMPTY_RESPONSE")
    if len(body) > max_bytes:
        raise SecProviderError("SEC_RESPONSE_TOO_LARGE", status=status)
    return body


def _pace_sec_request() -> None:
    """Apply a process-wide SEC start-rate cap across www.sec.gov and data.sec.gov."""
    global _SEC_LAST_REQUEST_AT
    with _SEC_RATE_LOCK:
        now = time.monotonic()
        if _SEC_LAST_REQUEST_AT is not None:
            wait = _SEC_MIN_REQUEST_INTERVAL_SECONDS - (now - _SEC_LAST_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
        _SEC_LAST_REQUEST_AT = time.monotonic()


def fetch_json(
    ctx: FetchContext, url: str, *, transport: HttpGet | None = None
) -> Mapping[str, Any]:
    """Fetch one SEC JSON object, enforcing a response-size and top-level shape bound."""
    body = fetch_bytes(ctx, url, max_bytes=MAX_JSON_RESPONSE_BYTES, transport=transport)
    try:
        document: object = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise SecProviderError("SEC_BAD_JSON") from None
    if not isinstance(document, Mapping):
        raise SecProviderError("SEC_BAD_JSON_SHAPE")
    return document


def parse_ticker_directory(document: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Parse the SEC company-ticker JSON object into exact identity rows."""
    rows: list[dict[str, str]] = []
    if "fields" in document or "data" in document:
        fields = document.get("fields")
        data = document.get("data")
        if (
            not isinstance(fields, list)
            or any(not isinstance(field, str) for field in fields)
            or not isinstance(data, list)
            or not {"cik", "name", "ticker"}.issubset(fields)
        ):
            raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
        if len(fields) != len(set(fields)):
            raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
        positions = {
            field: fields.index(field)
            for field in ("cik", "name", "ticker", "exchange")
            if field in fields
        }
        for raw in data:
            if not isinstance(raw, list) or len(raw) != len(fields):
                raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
            cik = _canonical_cik(raw[positions["cik"]])
            name = _required_text(raw[positions["name"]])
            ticker = _required_text(raw[positions["ticker"]]).upper()
            if _TICKER_RE.fullmatch(ticker) is None:
                raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
            exchange = ""
            if "exchange" in positions:
                raw_exchange = raw[positions["exchange"]]
                exchange = (
                    ""
                    if raw_exchange is None or raw_exchange == ""
                    else _required_text(raw_exchange)
                )
            rows.append({"cik": cik, "ticker": ticker, "name": name, "exchange": exchange})
            if len(rows) > MAX_TICKER_DIRECTORY_ROWS:
                raise SecProviderError("SEC_TICKER_DIRECTORY_TOO_LARGE")
    else:
        for key, raw in document.items():
            if not isinstance(key, str) or not isinstance(raw, Mapping):
                raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
            cik_value = raw.get("cik_str", raw.get("cik"))
            cik = _canonical_cik(cik_value)
            name = _required_text(raw.get("title", raw.get("name")))
            ticker = _required_text(raw.get("ticker")).upper()
            if _TICKER_RE.fullmatch(ticker) is None:
                raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
            exchange_value = raw.get("exchange")
            exchange = "" if exchange_value is None else _required_text(exchange_value)
            rows.append({"cik": cik, "ticker": ticker, "name": name, "exchange": exchange})
            if len(rows) > MAX_TICKER_DIRECTORY_ROWS:
                raise SecProviderError("SEC_TICKER_DIRECTORY_TOO_LARGE")
    if not rows:
        raise SecProviderError("SEC_TICKER_DIRECTORY_EMPTY")
    if any(_CONTROL_RE.search(value) is not None for row in rows for value in row.values()):
        raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
    return tuple(sorted(rows, key=lambda row: (row["ticker"], row["cik"], row["name"])))


def company_ticker_rows(
    ctx: FetchContext, *, transport: HttpGet | None = None
) -> tuple[dict[str, str], ...]:
    """Fetch and parse the SEC's documented ticker/CIK/name association file."""
    return parse_ticker_directory(fetch_json(ctx, SEC_COMPANY_TICKERS_URL, transport=transport))


def exchange_ticker_rows(
    ctx: FetchContext, *, transport: HttpGet | None = None
) -> tuple[dict[str, str], ...]:
    """Fetch and parse the SEC's documented ticker/CIK/exchange association file."""
    return parse_ticker_directory(
        fetch_json(ctx, SEC_COMPANY_TICKERS_EXCHANGE_URL, transport=transport)
    )


def resolve_ticker(rows: Sequence[Mapping[str, str]], ticker: str) -> Mapping[str, str]:
    """Resolve an exact ticker and fail on absent or conflicting SEC associations."""
    matches = [row for row in rows if row["ticker"].casefold() == ticker.casefold()]
    if not matches:
        raise SecProviderError("SEC_TICKER_NOT_FOUND")
    ciks = {row["cik"] for row in matches}
    if len(ciks) != 1:
        raise SecProviderError("SEC_TICKER_AMBIGUOUS")
    return matches[0]


def canonical_cik(value: object) -> str:
    """Validate a CIK and return the SEC's ten-digit URL form."""
    return _canonical_cik(value)


def _canonical_cik(value: object) -> str:
    if isinstance(value, bool):
        raise SecProviderError("SEC_BAD_CIK")
    if isinstance(value, int):
        digits = str(value)
    elif isinstance(value, str):
        digits = value.strip()
    else:
        raise SecProviderError("SEC_BAD_CIK")
    if not digits.isdigit() or not 1 <= len(digits) <= 10 or int(digits) <= 0:
        raise SecProviderError("SEC_BAD_CIK")
    return digits.zfill(10)


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or _CONTROL_RE.search(value) is not None:
        raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
    return value.strip()


def _http_error_code(status: int | None) -> str:
    if status == 404:
        return "SEC_NOT_FOUND"
    if status == 403:
        return "SEC_FORBIDDEN"
    if status == 429:
        return "SEC_RATE_LIMITED"
    if status is not None and status >= 500:
        return "SEC_UPSTREAM_UNAVAILABLE"
    return "SEC_HTTP_ERROR"
