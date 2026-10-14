"""Governed JSON-RPC transport for Deribit's public HTTP API."""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

BASE_URL = "https://www.deribit.com/api/v2"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
INSTRUMENT_REQUEST_INTERVAL_SECONDS = 1.0
_INSTRUMENT_METHOD = "public/get_instruments"
_ALLOWED_METHODS = frozenset(
    {
        "public/get_book_summary_by_currency",
        "public/get_instruments",
        "public/get_tradingview_chart_data",
        "public/ticker",
    }
)
_REQUEST_IDS = itertools.count(1)
_REQUEST_ID_LOCK = threading.Lock()
_INSTRUMENT_LIMITER_LOCK = threading.Lock()
_LAST_INSTRUMENT_REQUEST_AT = 0.0


class DeribitProviderError(RuntimeError):
    """Sanitized, stable error from a Deribit public request or response."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        rpc_code: int | None = None,
    ) -> None:
        """Store only stable codes and numeric status metadata."""
        self.code = code
        self.status = status
        self.rpc_code = rpc_code
        details = "" if status is None else f" status={status}"
        details += "" if rpc_code is None else f" rpc_code={rpc_code}"
        super().__init__(code + details)


@dataclass(frozen=True)
class RpcReply:
    """A JSON-RPC result and the server response instant from ``usOut``."""

    result: object
    observed_at: datetime


def rpc_call(
    method: str,
    params: Mapping[str, object],
    *,
    timeout: float | None = None,
) -> RpcReply:
    """Call one public method using the shared, request-governed HTTP client."""
    if method not in _ALLOWED_METHODS:
        raise DeribitProviderError("DERIBIT_METHOD_NOT_ALLOWED")
    if method == _INSTRUMENT_METHOD:
        _respect_instrument_rate_limit()

    with _REQUEST_ID_LOCK:
        request_id = next(_REQUEST_IDS)
    payload: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": dict(params),
    }
    endpoint = f"{BASE_URL}/{method}"

    try:
        response = get_shared_http_client().request(
            "POST",
            endpoint,
            headers={"Content-Type": "application/json"},
            timeout=DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            source="deribit",
            json_body=payload,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise DeribitProviderError(_http_error_code(exc.status), status=exc.status) from None
    except Exception:
        # Transport exceptions can contain request details; expose only the stable code.
        raise DeribitProviderError("DERIBIT_TRANSPORT_ERROR") from None

    status = response.status_code
    if not 200 <= status < 300:
        raise DeribitProviderError(_http_error_code(status), status=status)
    body = response.content
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise DeribitProviderError("DERIBIT_RESPONSE_TOO_LARGE", status=status)
    try:
        document: object = response.json()
    except Exception:
        raise DeribitProviderError("DERIBIT_BAD_JSON", status=status) from None
    return _parse_reply(document, request_id=request_id, status=status)


def _parse_reply(document: object, *, request_id: int, status: int) -> RpcReply:
    """Validate the JSON-RPC envelope before exposing its result to a model parser."""
    if not isinstance(document, dict):
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    if document.get("jsonrpc") != "2.0":
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    response_id = document.get("id")
    if type(response_id) is not int or response_id != request_id:
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    if "result" in document and "error" in document:
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    if "error" in document:
        error = document["error"]
        rpc_code: int | None = None
        if isinstance(error, dict):
            raw_code = error.get("code")
            if type(raw_code) is int:
                rpc_code = raw_code
        raise DeribitProviderError("DERIBIT_RPC_ERROR", status=status, rpc_code=rpc_code)
    if "result" not in document or "usOut" not in document:
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    server_time = document["usOut"]
    if type(server_time) is not int or server_time <= 0:
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status)
    seconds, microseconds = divmod(server_time, 1_000_000)
    try:
        observed_at = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=microseconds
        )
    except (OverflowError, OSError, ValueError):
        raise DeribitProviderError("DERIBIT_BAD_ENVELOPE", status=status) from None
    return RpcReply(result=document["result"], observed_at=observed_at)


def _http_error_code(status: int | None) -> str:
    """Classify HTTP failures without retaining the response body."""
    if status == 429:
        return "DERIBIT_RATE_LIMITED"
    if status in {401, 403}:
        return "DERIBIT_ACCESS_ERROR"
    if status is not None and status >= 500:
        return "DERIBIT_UPSTREAM_ERROR"
    return "DERIBIT_HTTP_ERROR"


def _respect_instrument_rate_limit() -> None:
    """Keep public/get_instruments calls at its documented sustained rate."""
    global _LAST_INSTRUMENT_REQUEST_AT
    with _INSTRUMENT_LIMITER_LOCK:
        now = time.monotonic()
        wait_for = max(
            0.0,
            _LAST_INSTRUMENT_REQUEST_AT + INSTRUMENT_REQUEST_INTERVAL_SECONDS - now,
        )
        if wait_for:
            time.sleep(wait_for)
        _LAST_INSTRUMENT_REQUEST_AT = time.monotonic()
