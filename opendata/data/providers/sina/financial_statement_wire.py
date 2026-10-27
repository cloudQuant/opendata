"""Bounded, source-qualified transport for Sina's raw fzb report response."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opendata.data.http_client import (
    BoundedHttpResponse,
    FailureCategory,
    GovernedHttpClient,
    HttpFetchError,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_FZB_CANONICAL_MODEL,
    SINA_FZB_HOST,
    SINA_FZB_HTTP_CONNECT_TIMEOUT_SECONDS,
    SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS,
    SINA_FZB_HTTP_READ_TIMEOUT_SECONDS,
    SINA_FZB_PAGE,
    SINA_FZB_PAGE_SIZE,
    SINA_FZB_QUERY_TYPE,
    SINA_FZB_STATEMENT_CODE,
    SINA_FZB_URL,
    SINA_SOURCE,
    SinaFzbAdmissionError,
    SinaFzbFetchedSnapshot,
    SinaFzbQuery,
    SinaFzbTransportError,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_parser import parse_sina_fzb_response
from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestOperation,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    request_execution_scope,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ)", re.ASCII)

__all__ = ("fetch_sina_fzb_raw",)


def _paper_code(symbol: str) -> str:
    """Convert one validated canonical query symbol to Sina's request identity."""
    if type(symbol) is not str or _SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise SinaFzbWireError(code="invalid_query")
    market = "sh" if symbol.endswith(".SH") else "sz"
    return f"{market}{symbol[:6]}"


def _request_timeout(context: FetchContext) -> tuple[float, float]:
    """Cap connect/read socket timeouts and honor a shorter context timeout."""
    context_timeout = context.timeout
    if context_timeout is None:
        return SINA_FZB_HTTP_CONNECT_TIMEOUT_SECONDS, SINA_FZB_HTTP_READ_TIMEOUT_SECONDS
    return (
        min(SINA_FZB_HTTP_CONNECT_TIMEOUT_SECONDS, float(context_timeout)),
        min(SINA_FZB_HTTP_READ_TIMEOUT_SECONDS, float(context_timeout)),
    )


def _map_http_error(error: HttpFetchError) -> SinaFzbTransportError:
    """Map a governed HTTP failure to a fixed, payload-safe Sina error code."""
    if error.category is FailureCategory.BODY_TOO_LARGE:
        return SinaFzbTransportError(code="response_too_large")
    if error.category is FailureCategory.TIMEOUT:
        return SinaFzbTransportError(code="deadline_exceeded")
    if error.status is not None and error.status != 200:
        return SinaFzbTransportError(code="http_status")
    return SinaFzbTransportError(code="transport_failed")


@contextmanager
def _sina_fzb_bounded_response_scope(
    query: SinaFzbQuery,
    *,
    context: FetchContext,
    client: GovernedHttpClient,
) -> Iterator[BoundedHttpResponse]:
    """Yield one bounded Sina response while its original request scope stays active.

    The caller must provide an exact Sina/financial-statement/query grant for
    ``quotes.sina.cn`` in ``context.request_budget``. The response is streamed
    through the governed client with an application-level byte cap and is never
    cached. The one monotonic deadline and cancellation signal remain active
    across transport and any work performed by the context manager's caller.
    The overall timeout is cooperative: connect and read timeouts are separate
    socket operation limits, and a blocking read may return after the overall
    budget before the client can observe and report its expiry.

    Args:
        query: Validated canonical six-digit Shanghai or Shenzhen symbol.
        context: Explicit query context carrying a finite request budget and
            optional cancellation/deadline controls.
        client: Explicit governed HTTP client; no shared client is created.

    Yields:
        The response body, actual HTTP status, and UTC retrieval timestamp.

    Raises:
        SinaFzbAdmissionError: If the budget is missing, the operation is not a
            query, or the exact scoped source/model/host grant is not allowed.
        SinaFzbTransportError: If bounded transport fails or HTTP status is not
            exactly 200.
        SinaFzbWireError: If the query symbol is invalid.
    """
    try:
        call_started = time.monotonic()
        if type(query) is not SinaFzbQuery:
            raise SinaFzbWireError(code="invalid_query")
        if type(context) is not FetchContext or type(client) is not GovernedHttpClient:
            raise SinaFzbAdmissionError(code="admission_denied")
        if context.request_budget is None:
            raise SinaFzbAdmissionError(code="admission_required")
        if context.operation is not RequestOperation.QUERY:
            raise SinaFzbAdmissionError(code="admission_denied")

        paper_code = _paper_code(query.symbol)
        deadline = context._deadline_monotonic
        if deadline is None and context.timeout is not None:
            deadline = call_started + float(context.timeout)
        timeout = _request_timeout(context)
        overall_timeout = min(
            SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS
            if context.timeout is None
            else float(context.timeout),
            SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS,
        )
        params = {
            "paperCode": paper_code,
            "source": SINA_FZB_STATEMENT_CODE,
            "type": SINA_FZB_QUERY_TYPE,
            "page": SINA_FZB_PAGE,
            "num": SINA_FZB_PAGE_SIZE,
        }

        with request_execution_scope(
            source=SINA_SOURCE,
            canonical_model=SINA_FZB_CANONICAL_MODEL,
            operation=RequestOperation.QUERY,
            budget=context.request_budget,
            deadline=deadline,
            cancellation=context._thread_cancel_event,
        ):
            scopes = current_request_scopes()
            check_scope_liveness(scopes)
            authorize_request(scopes, SINA_SOURCE, SINA_FZB_HOST)
            response: BoundedHttpResponse = client.get_bounded(
                SINA_FZB_URL,
                max_body_bytes=MAX_RAW_RESPONSE_BYTES,
                overall_timeout=overall_timeout,
                params=params,
                headers={"Accept-Encoding": "identity"},
                timeout=timeout,
                source=SINA_SOURCE,
            )
            check_scope_liveness(scopes)
            if response.status_code != 200:
                raise SinaFzbTransportError(code="http_status")
            yield response
            check_scope_liveness(scopes)
    except RequestExecutionDeadlineError:
        raise SinaFzbTransportError(code="deadline_exceeded") from None
    except (
        RequestAttemptLimitError,
        RequestAuthorizationError,
        RequestBudgetScopeError,
    ):
        raise SinaFzbAdmissionError(code="admission_denied") from None
    except RequestExecutionCancelledError:
        raise SinaFzbTransportError(code="cancelled") from None
    except HttpFetchError as error:
        raise _map_http_error(error) from None


def _fetch_sina_fzb_bounded_response(
    query: SinaFzbQuery,
    *,
    context: FetchContext,
    client: GovernedHttpClient,
) -> BoundedHttpResponse:
    """Fetch one bounded Sina response without parsing it."""
    with _sina_fzb_bounded_response_scope(query, context=context, client=client) as response:
        return response


def fetch_sina_fzb_raw(
    query: SinaFzbQuery,
    *,
    context: FetchContext,
    client: GovernedHttpClient,
) -> SinaFzbFetchedSnapshot:
    """Fetch and parse one bounded raw Sina balance-sheet response.

    The caller must provide an exact Sina/financial-statement/query grant for
    ``quotes.sina.cn`` in ``context.request_budget``. The response is streamed
    through the governed client with an application-level byte cap and is never
    cached. The returned retrieval timestamp records transport completion and
    remains separate from source publication and update timestamps. The overall
    timeout is cooperative: connect and read timeouts are separate socket
    operation limits, and a blocking read may return after the overall budget
    before the client can observe and report its expiry.

    Args:
        query: Validated canonical six-digit Shanghai or Shenzhen symbol.
        context: Explicit query context carrying a finite request budget and
            optional cancellation/deadline controls.
        client: Explicit governed HTTP client; no shared client is created.

    Returns:
        The query-bound source-native reports and UTC transport retrieval time.

    Raises:
        SinaFzbAdmissionError: If the budget is missing, the operation is not a
            query, or the exact scoped source/model/host grant is not allowed.
        SinaFzbTransportError: If bounded transport fails or HTTP status is not
            exactly 200.
        SinaFzbWireError: If the response bytes do not match the frozen wire
            contract.
    """
    with _sina_fzb_bounded_response_scope(query, context=context, client=client) as response:
        reports = parse_sina_fzb_response(response.body, query=query)
        return SinaFzbFetchedSnapshot(
            query=query,
            retrieved_at_utc=response.retrieved_at,
            reports=reports,
        )
