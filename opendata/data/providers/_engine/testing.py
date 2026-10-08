"""Offline fixtures for engine-driven provider models.

Nothing here authorizes live access. A grant built by :func:`fixture_context`
is a local test credential for a *synthetic* transport, mirroring
``tests/provider_budget_fixtures.py``: it names one exact source/model/operation,
is finite, expires, and states its evidence as a test fixture. Deriving it from
the declaration rather than a hand-maintained table is what lets a new provider
model be tested without editing shared code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from opendata.data.protocol import FetchContext
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)

if TYPE_CHECKING:
    from opendata.data.providers._engine.http_json import HttpResponse
    from opendata.data.providers._engine.spec import ModelSpec

RIGHTS_EVIDENCE = "test-fixture:offline-only:fake-transport"


def fixture_context(source: str, spec: ModelSpec, *, sends: int = 3) -> FetchContext:
    """Return one finite, single-identity grant context for a declared model.

    Args:
        source: Provider routing id.
        spec: The model declaration being exercised.
        sends: Bound on sends. The bounded-request profile is used because a
            declared ``max_pages`` can exceed the live-smoke per-source ceiling
            of three; the bound still has to be stated, so a fetcher that loops
            past its own declaration fails instead of hanging.

    Returns:
        A :class:`FetchContext` carrying a budget that allows exactly this
        model's QUERY operation against the declaration's own host.
    """
    host = urlsplit(spec.base_url).netloc
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=source,
        canonical_model=spec.model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence=RIGHTS_EVIDENCE,
        task_attempts=sends,
        source_attempts=sends,
        allowed_hosts=frozenset({host}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=profile,
    )
    budget = RequestBudget(
        task_attempts=sends,
        source_attempts=sends,
        grants=(grant,),
        profile=profile,
    )
    return FetchContext(timeout=5.0, request_budget=budget)


class SyntheticTransport:
    """A replaceable transport that records every request it is asked to serve.

    Responses are keyed by an exact ``(url, params)`` pair, so a test states
    precisely which request must produce which body; an unkeyed request raises
    instead of quietly returning someone else's fixture.
    """

    def __init__(
        self, responses: dict[tuple[str, frozenset[tuple[str, str]]], HttpResponse]
    ) -> None:
        """Store the keyed responses and start with an empty call log."""
        self._responses = responses
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
    ) -> HttpResponse:
        """Return the response keyed to this exact request, or fail loudly."""
        key = (url, frozenset(params.items()))
        self.calls.append(
            {"url": url, "params": dict(params), "timeout": timeout, "source": source}
        )
        try:
            return self._responses[key]
        except KeyError:
            raise AssertionError(
                f"no synthetic response is keyed for {url}?{sorted(params.items())}"
            ) from None


class FixedResponseTransport(SyntheticTransport):
    """A transport answering every request with one canned response.

    Used by rejection cases where the body is irrelevant, e.g. an upstream 429
    or a malformed document; keyed requests stay available for shape cases.
    """

    def __init__(self, response: HttpResponse) -> None:
        """Start from an empty response table and keep the single response."""
        super().__init__({})
        self._response = response

    def __call__(
        self,
        url: str,
        params: dict[str, str],
        *,
        timeout: float,
        source: str,
    ) -> HttpResponse:
        """Record the request and return the canned response."""
        self.calls.append(
            {"url": url, "params": dict(params), "timeout": timeout, "source": source}
        )
        return self._response
