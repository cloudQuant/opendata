"""Fixed, default-deny process handler for bounded Sina FZB captures."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from opendata.data.http_client import BoundedHttpResponse, GovernedHttpClient, HttpClientConfig
from opendata.data.protocol import FetchContext
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SINA_FZB_CANONICAL_MODEL,
    SINA_FZB_HOST,
    SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS,
    SINA_SOURCE,
    SinaFzbAdmissionError,
    SinaFzbQuery,
    SinaFzbTransportError,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_wire import (
    _fetch_sina_fzb_bounded_response,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

__all__ = [
    "SINA_FZB_PROCESS_OPERATION_ID",
    "SINA_FZB_PROCESS_PROTOCOL",
    "fetch_sina_fzb_capture",
]

SINA_FZB_PROCESS_OPERATION_ID = "sina.fzb.capture.v1"
"""Fixed operation key registered only by an explicitly built executor."""

SINA_FZB_PROCESS_PROTOCOL = "sina-fzb-capture-v1"
"""Versioned JSON protocol for one canonical Sina FZB symbol capture."""

_UNKNOWN_RIGHTS_EVIDENCE = "wave12-root-freeze-decision: offline implementation only"
_UNKNOWN_RIGHTS_CONDITION = "source-use rights and response retention remain unresolved"
_UTC = timezone.utc


def fetch_sina_fzb_capture(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Fetch one bounded FZB body under the fixed child protocol.

    The production handler always builds an UNKNOWN source grant. The grant
    rejects admission before transport while source-use rights remain open.

    Args:
        parameters: Exact ``protocol`` and canonical ``symbol`` JSON fields.

    Returns:
        The fixed result envelope containing the same bounded body in Base64.

    Raises:
        SinaFzbAdmissionError: If the default-deny source grant blocks access.
        SinaFzbTransportError: If bounded transport or result validation fails.
        SinaFzbWireError: If the request object does not match the protocol.
    """
    query = _validated_query(parameters)
    session_owner = GovernedHttpClient(
        HttpClientConfig(max_attempts=1),
        raw_response_cache=None,
    )
    session = session_owner._get_session()
    try:
        # Remove ambient authentication and environment-derived transport
        # behavior before binding this fresh session to the dedicated client.
        session.trust_env = False
        session.auth = None
        session.proxies.clear()
        session.headers.pop("Authorization", None)
        context = _default_denied_context()
        client = GovernedHttpClient(
            HttpClientConfig(max_attempts=1),
            session=session,
            raw_response_cache=None,
        )
        return _capture_sina_fzb_response(
            {"protocol": SINA_FZB_PROCESS_PROTOCOL, "symbol": query.symbol},
            context=context,
            client=client,
        )
    finally:
        session.close()


def _capture_sina_fzb_response(
    parameters: Mapping[str, JsonValue],
    *,
    context: FetchContext,
    client: GovernedHttpClient,
) -> dict[str, JsonValue]:
    """Use the governed W9 helper once and encode its bounded response.

    This private in-process seam accepts explicit trusted dependencies for
    offline fake-transport tests. No dependency selector crosses process JSON.

    Args:
        parameters: Exact child protocol request object.
        context: Explicit governed request scope for this call.
        client: Explicit governed HTTP client for this call.

    Returns:
        The exact bounded capture envelope expected by the parent decoder.

    Raises:
        SinaFzbAdmissionError: If the supplied request scope is not admitted.
        SinaFzbTransportError: If the bounded response is invalid.
        SinaFzbWireError: If the request object does not match the protocol.
    """
    query = _validated_query(parameters)
    if type(context) is not FetchContext or type(client) is not GovernedHttpClient:
        raise SinaFzbAdmissionError("admission_denied")

    response = _fetch_sina_fzb_bounded_response(query, context=context, client=client)
    if type(response) is not BoundedHttpResponse:
        raise SinaFzbTransportError("transport_failed")
    if type(response.status_code) is not int:
        raise SinaFzbTransportError("transport_failed")
    if response.status_code != 200:
        raise SinaFzbTransportError("http_status")
    if type(response.body) is not bytes:
        raise SinaFzbTransportError("transport_failed")
    if len(response.body) > MAX_RAW_RESPONSE_BYTES:
        raise SinaFzbTransportError("response_too_large")

    retrieved_at_utc = _validated_retrieved_at(response.retrieved_at)
    return {
        "protocol": SINA_FZB_PROCESS_PROTOCOL,
        "query": {"symbol": query.symbol},
        "retrieved_at_utc": retrieved_at_utc,
        "status_code": 200,
        "body_b64": base64.b64encode(response.body).decode("ascii"),
    }


def _validated_query(parameters: Mapping[str, JsonValue]) -> SinaFzbQuery:
    """Validate the exact two-field process request without coercion."""
    if type(parameters) is not dict or len(parameters) != 2:
        raise SinaFzbWireError("invalid_envelope")
    keys = tuple(parameters.keys())
    if any(type(key) is not str for key in keys):
        raise SinaFzbWireError("invalid_envelope")
    if frozenset(keys) != frozenset(("protocol", "symbol")):
        raise SinaFzbWireError("invalid_envelope")
    protocol = parameters["protocol"]
    symbol = parameters["symbol"]
    if type(protocol) is not str or protocol != SINA_FZB_PROCESS_PROTOCOL:
        raise SinaFzbWireError("invalid_envelope")
    if type(symbol) is not str:
        raise SinaFzbWireError("invalid_query")
    return SinaFzbQuery(symbol)


def _default_denied_context() -> FetchContext:
    """Create a one-attempt request scope with no source authority."""
    grant = RequestGrant(
        source=SINA_SOURCE,
        canonical_model=SINA_FZB_CANONICAL_MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.UNKNOWN,
        rights_evidence=_UNKNOWN_RIGHTS_EVIDENCE,
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset((SINA_FZB_HOST,)),
        expires_at=datetime.max.replace(tzinfo=_UTC),
        conditions=(_UNKNOWN_RIGHTS_CONDITION,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    return FetchContext(
        timeout=SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS,
        request_budget=budget,
        operation=RequestOperation.QUERY,
    )


def _validated_retrieved_at(value: object) -> str:
    """Render one aware zero-offset instant without reusing caller tzinfo."""
    if type(value) is not datetime or value.tzinfo is None:
        raise SinaFzbTransportError("transport_failed")
    try:
        offset = value.utcoffset()
    except Exception:
        raise SinaFzbTransportError("transport_failed") from None
    if type(offset) is not timedelta or offset != timedelta(0):
        raise SinaFzbTransportError("transport_failed")
    try:
        normalized = value.astimezone(_UTC)
    except Exception:
        raise SinaFzbTransportError("transport_failed") from None
    if type(normalized) is not datetime or normalized.tzinfo is not _UTC:
        raise SinaFzbTransportError("transport_failed")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
