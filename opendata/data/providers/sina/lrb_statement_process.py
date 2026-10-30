"""Fixed one-request Sina LRB capture operation for a reviewed child process.

The operation uses the user's existing AkShare data-use authorization for one
bounded query per invocation. Its in-memory request budget is not a persistent
cross-run quota. The generic ReviewedProcessExecutor launch path inherits the
parent environment; this handler reads no dotenv file and uses no credential,
but deployment must account for unrelated variables inherited by the child.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import requests
from requests.adapters import HTTPAdapter

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina.lrb_statement_parser import (
    MAX_SINA_LRB_RAW_RESPONSE_BYTES,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetError,
    RequestBudgetProfile,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from pydantic import JsonValue

__all__ = [
    "SINA_LRB_PROCESS_OPERATION_ID",
    "SINA_LRB_PROCESS_PROTOCOL",
    "SinaLrbProcessError",
    "SinaLrbQuery",
    "build_sina_lrb_query_parameters",
    "fetch_sina_lrb_capture",
    "sina_lrb_request_summary_sha256",
]

SINA_LRB_PROCESS_OPERATION_ID = "sina.lrb.capture.v1"
"""Fixed operation key registered only by an explicitly built executor."""
SINA_LRB_PROCESS_PROTOCOL = "sina-lrb-capture-v1"
"""Versioned JSON protocol for one canonical Sina LRB symbol capture."""

SINA_SOURCE = "sina"
SINA_CANONICAL_MODEL = "financial_statement"
SINA_HOST = "quotes.sina.cn"
SINA_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
MAX_RAW_RESPONSE_BYTES = MAX_SINA_LRB_RAW_RESPONSE_BYTES
OVERALL_TIMEOUT_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 5.0
MAX_REQUESTS_PER_INVOCATION = 1
_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z", re.ASCII)
_UTC = timezone.utc

_ERROR_CODES = frozenset(
    {
        "invalid_envelope",
        "invalid_query",
        "admission_denied",
        "transport_failed",
        "http_status",
        "response_too_large",
        "deadline_exceeded",
        "cancelled",
        "request_summary_invalid",
    }
)
_REQUEST_SUMMARY_FIELDS = frozenset({"paper_code", "source", "type", "page", "num"})


class SinaLrbProcessError(RuntimeError):
    """Fixed, source-value-free failure from the bounded capture operation."""

    def __init__(self, code: str) -> None:
        """Create a stable error without retaining response or exception text."""
        self.code = code if code in _ERROR_CODES else "transport_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SinaLrbQuery:
    """Canonical six-digit Shanghai or Shenzhen instrument for one LRB request."""

    symbol: str

    def __post_init__(self) -> None:
        """Validate the exact exchange-qualified symbol without coercion."""
        if type(self.symbol) is not str or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise SinaLrbProcessError("invalid_query")


def build_sina_lrb_query_parameters(instrument_id: object) -> dict[str, str]:
    """Build the one provider query shared by transport and request provenance.

    Args:
        instrument_id: Canonical six-digit SH or SZ symbol.

    Returns:
        A new mapping of the exact wire query parameters.

    Raises:
        SinaLrbProcessError: If the instrument identifier is not canonical.
    """
    if type(instrument_id) is not str:
        raise SinaLrbProcessError("invalid_query")
    query = SinaLrbQuery(instrument_id)
    market = "sh" if query.symbol.endswith(".SH") else "sz"
    return {
        "paperCode": f"{market}{query.symbol[:6]}",
        "source": "lrb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }


def sina_lrb_request_summary_sha256(instrument_id: object) -> str:
    """Hash the same fixed wire values used by the actual Sina LRB request.

    Args:
        instrument_id: Canonical six-digit SH or SZ symbol.

    Returns:
        Lowercase SHA-256 digest for the canonical request summary.

    Raises:
        SinaLrbProcessError: If the symbol or bounded summary is invalid.
    """
    parameters = build_sina_lrb_query_parameters(instrument_id)
    summary_parameters = {
        "paper_code": parameters["paperCode"],
        "source": parameters["source"],
        "type": parameters["type"],
        "page": parameters["page"],
        "num": parameters["num"],
    }
    try:
        digest: str = request_summary_sha256(
            summary_parameters,
            allowed_fields=_REQUEST_SUMMARY_FIELDS,
        )
        return digest
    except Exception:
        raise SinaLrbProcessError("request_summary_invalid") from None


def fetch_sina_lrb_capture(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Fetch one fixed LRB page and return its bounded body through the child protocol.

    Only the symbol crosses the process boundary. Endpoint, report type,
    pagination, request limits, and source grant are fixed in trusted code. The
    result contains response bytes encoded as Base64 and no decoded row values.

    Args:
        parameters: Exact protocol and canonical symbol JSON fields.

    Returns:
        A fixed JSON result envelope containing one successful response body.

    Raises:
        SinaLrbProcessError: If the request is invalid, unauthorized, or fails
            the bounded HTTP response contract.
    """
    query = _validated_query(parameters)
    context = _authorized_request_context()
    session = _new_session()
    try:
        client = GovernedHttpClient(
            _http_config(),
            session=session,
            raw_response_cache=None,
        )
        return _capture_sina_lrb_response(
            {"protocol": SINA_LRB_PROCESS_PROTOCOL, "symbol": query.symbol},
            context=context,
            client=client,
        )
    finally:
        session.close()


def _capture_sina_lrb_response(
    parameters: Mapping[str, JsonValue],
    *,
    context: FetchContext,
    client: GovernedHttpClient,
) -> dict[str, JsonValue]:
    """Make and validate exactly one governed LRB request using explicit dependencies."""
    query = _validated_query(parameters)
    if type(context) is not FetchContext or type(client) is not GovernedHttpClient:
        raise SinaLrbProcessError("admission_denied")
    if context.request_budget is None or context.operation is not RequestOperation.QUERY:
        raise SinaLrbProcessError("admission_denied")

    query_params = build_sina_lrb_query_parameters(query.symbol)
    deadline = context._deadline_monotonic
    if deadline is None:
        deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS

    try:
        with request_execution_scope(
            source=SINA_SOURCE,
            canonical_model=SINA_CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
        ):
            response = client.get_bounded(
                SINA_URL,
                params=query_params,
                headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"},
                max_body_bytes=MAX_RAW_RESPONSE_BYTES,
                overall_timeout=OVERALL_TIMEOUT_SECONDS,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                source=SINA_SOURCE,
            )
    except RequestExecutionCancelledError:
        raise SinaLrbProcessError("cancelled") from None
    except RequestExecutionDeadlineError:
        raise SinaLrbProcessError("deadline_exceeded") from None
    except RequestBudgetError:
        raise SinaLrbProcessError("admission_denied") from None
    except HttpFetchError as error:
        raise SinaLrbProcessError(_transport_error_code(error)) from None
    except Exception:
        raise SinaLrbProcessError("transport_failed") from None

    if type(response) is not BoundedHttpResponse:
        raise SinaLrbProcessError("transport_failed")
    if type(response.status_code) is not int or response.status_code != 200:
        raise SinaLrbProcessError("http_status")
    if type(response.body) is not bytes:
        raise SinaLrbProcessError("transport_failed")
    if len(response.body) > MAX_RAW_RESPONSE_BYTES:
        raise SinaLrbProcessError("response_too_large")
    retrieved_at = _format_retrieved_at(response.retrieved_at)
    return {
        "protocol": SINA_LRB_PROCESS_PROTOCOL,
        "query": {"symbol": query.symbol},
        "retrieved_at_utc": retrieved_at,
        "status_code": 200,
        "body_b64": base64.b64encode(response.body).decode("ascii"),
    }


def _validated_query(parameters: Mapping[str, JsonValue]) -> SinaLrbQuery:
    """Accept exactly the versioned protocol and one SH/SZ symbol."""
    if type(parameters) is not dict or set(parameters) != {"protocol", "symbol"}:
        raise SinaLrbProcessError("invalid_envelope")
    protocol = parameters["protocol"]
    symbol = parameters["symbol"]
    if type(protocol) is not str or protocol != SINA_LRB_PROCESS_PROTOCOL:
        raise SinaLrbProcessError("invalid_envelope")
    if type(symbol) is not str:
        raise SinaLrbProcessError("invalid_query")
    return SinaLrbQuery(symbol)


def _authorized_request_context() -> FetchContext:
    """Declare the existing user AkShare authorization for one local invocation.

    This in-memory quota limits only this operation call. It does not reserve
    or persist quota across repeated jobs, workers, or process restarts.
    """
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=SINA_SOURCE,
        canonical_model=SINA_CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="user_authorized_akshare_data_use",
        task_attempts=MAX_REQUESTS_PER_INVOCATION,
        source_attempts=MAX_REQUESTS_PER_INVOCATION,
        allowed_hosts=frozenset((SINA_HOST,)),
        expires_at=datetime.now(_UTC) + timedelta(minutes=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=MAX_REQUESTS_PER_INVOCATION,
        source_attempts=MAX_REQUESTS_PER_INVOCATION,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(
        timeout=OVERALL_TIMEOUT_SECONDS,
        request_budget=budget,
        operation=RequestOperation.QUERY,
    )


def _http_config() -> HttpClientConfig:
    """Build fixed retry-free, rate-limited, direct HTTPS settings."""
    return HttpClientConfig(
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
        max_attempts=MAX_REQUESTS_PER_INVOCATION,
        backoff_base=0.0,
        backoff_jitter=0.0,
        rate_limit_per_host=1.0,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=0.0,
        default_headers={"Accept-Encoding": "identity"},
        proxies={},
    )


def _new_session() -> requests.Session:
    """Create a direct, unauthenticated standard Requests session with no retries."""
    session = requests.Session()
    session.trust_env = False
    session.auth = None
    session.proxies.clear()
    session.cookies.clear()
    session.headers.pop("Authorization", None)
    session.headers["Accept-Encoding"] = "identity"
    session.mount("https://", HTTPAdapter(max_retries=0))
    session.mount("http://", HTTPAdapter(max_retries=0))
    return session


def _transport_error_code(error: HttpFetchError) -> str:
    """Translate only stable typed HTTP fields into a fixed public code."""
    if type(error) is not HttpFetchError:
        return "transport_failed"
    if error.category is FailureCategory.BODY_TOO_LARGE:
        return "response_too_large"
    if error.category is FailureCategory.TIMEOUT:
        return "deadline_exceeded"
    if error.category is FailureCategory.RATE_LIMITED and error.status is None:
        return "admission_denied"
    if type(error.status) is int and not 200 <= error.status < 300:
        return "http_status"
    return "transport_failed"


def _format_retrieved_at(value: object) -> str:
    """Render one aware UTC instant in the exact child result format."""
    if type(value) is not datetime or value.tzinfo is None:
        raise SinaLrbProcessError("transport_failed")
    try:
        offset = value.utcoffset()
        if type(offset) is not timedelta or offset != timedelta(0):
            raise SinaLrbProcessError("transport_failed")
        normalized = value.astimezone(_UTC)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    except SinaLrbProcessError:
        raise
    except Exception:
        raise SinaLrbProcessError("transport_failed") from None
