"""Actual-send budget gate for the yfinance curl transport."""

from __future__ import annotations

from opendata.data.providers.yfinance._source import SOURCE
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestExecutionScope,
    authorize_request,
    check_scope_liveness,
    reserve_scoped_attempt,
)

YFINANCE_ALLOWED_HOSTS = frozenset(
    {
        "consent.yahoo.com",
        "fc.yahoo.com",
        "finance.yahoo.com",
        "guce.yahoo.com",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
    }
)


def reserve_yfinance_send(scopes: tuple[RequestExecutionScope, ...], host: str) -> None:
    """Authorize and reserve one exact-host transport send immediately before it."""
    if host not in YFINANCE_ALLOWED_HOSTS:
        raise RequestAuthorizationError("HTTPS host is outside yfinance's transport allowlist")
    if not scopes:
        raise RequestAuthorizationError("yfinance transport requires an active request scope")
    check_scope_liveness(scopes)
    authorize_request(scopes, SOURCE, host)
    reserve_scoped_attempt(scopes, SOURCE, host)


__all__ = ["YFINANCE_ALLOWED_HOSTS", "reserve_yfinance_send"]
