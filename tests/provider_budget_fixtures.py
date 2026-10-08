"""Finite request contexts for explicitly named offline provider fixtures.

These grants are local test fixtures for fake transports; they do not state
that any upstream data source has authorized live access.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)

_FIXTURE_HOSTS: dict[tuple[str, str], str] = {
    ("fred", "FredSearch"): "api.stlouisfed.org",
    ("fred", "FredSeries"): "api.stlouisfed.org",
    ("bls", "BlsSearch"): "download.bls.gov",
    ("bls", "BlsSeries"): "api.bls.gov",
    ("fmp", "EquityHistorical"): "financialmodelingprep.com",
    ("fmp", "EquityQuote"): "financialmodelingprep.com",
}


def offline_fixture_context(
    source: str,
    canonical_model: str,
    *,
    timeout: float | None = None,
) -> FetchContext:
    """Build one independent, finite fixture grant for one exact model/host."""
    identity = (source, canonical_model)
    try:
        host = _FIXTURE_HOSTS[identity]
    except KeyError as exc:
        raise ValueError("offline fixture identity is not allow-listed") from exc

    grant = RequestGrant(
        source=source,
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-transport",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    budget = RequestBudget(task_attempts=3, source_attempts=3, grants=(grant,))
    return FetchContext(timeout=timeout, request_budget=budget)
