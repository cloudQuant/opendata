"""FMP query errors remain distinct from errors in source responses."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from opendata.data.models.fmp_financials import BalanceSheet
from opendata.data.protocol import FetchContext
from opendata.data.providers._engine.http_json import (
    ProviderEngineError,
    ProviderEngineQueryError,
)
from opendata.data.providers.fmp.models import _client as fmp_client
from opendata.data.providers.fmp.models import iter2_financials as financials
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryValidationError,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query


def _context(canonical_model: str | None) -> FetchContext:
    assert canonical_model is not None
    grant = RequestGrant(
        source="fmp",
        canonical_model=canonical_model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline FMP query error classification test",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts={"financialmodelingprep.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return FetchContext(
        timeout=1.0,
        request_budget=RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,)),
    )


class _BadJsonResponseClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/json"},
            text="{}",
        )


def test_fmp_query_validation_is_not_misclassified_as_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FMPQueryValidationError.__mro__[:5] == (
        FMPQueryValidationError,
        ProviderEngineQueryError,
        FMPProviderError,
        ProviderEngineError,
        RuntimeError,
    )
    # The classification property this pins: a bad FMP query is still a query error, i.e. the
    # query branch of the engine's error face is matched before the provider-error branch, so
    # the service layer reports ProviderModelQueryValidationError rather than an upstream fault.
    assert FMPQueryValidationError.__mro__.index(
        ProviderEngineQueryError
    ) < FMPQueryValidationError.__mro__.index(FMPProviderError)

    fetcher = financials.BalanceSheetFetcher()
    capability = fetcher.capability
    descriptor = SimpleNamespace(
        source="fmp",
        model="BalanceSheet",
        domain=capability.domain,
        verified=capability.verified,
        full_capability_identity=(
            capability.asset_class,
            capability.domain,
            capability.period,
            capability.market,
            capability.source,
        ),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_resolve_fetcher",
        lambda *_args: (descriptor, fetcher),
    )
    monkeypatch.setattr(
        provider_model_query,
        "_reviewed_query_contract",
        lambda _domain: (None, BalanceSheet),
    )
    monkeypatch.setenv("FMP_API_KEY", "offline-test-key")
    client = _BadJsonResponseClient()
    monkeypatch.setattr(fmp_client, "get_shared_http_client", lambda: client)

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    with pytest.raises(FMPProviderError) as direct:
        fetcher.fetch(
            ctx=_context(fetcher.canonical_model),
            symbol="AAPL",
            start_date="2025-01-01",
        )
    assert isinstance(direct.value, FMPQueryValidationError)
    assert isinstance(direct.value, ProviderEngineQueryError)
    assert direct.value.code == "FMP_BAD_QUERY"
    assert direct.value.status is None
    assert direct.value.partial is False
    assert direct.value.requests_made == 0
    assert direct.value.url is None
    assert direct.value.rejected == direct.value.missing == direct.value.invalid == ()
    assert str(direct.value) == "FMP_BAD_QUERY"
    assert client.calls == []

    with pytest.raises(provider_model_query.ProviderModelQueryValidationError):
        asyncio.run(
            provider_model_query.query_provider_model(
                registry=object(),
                source="fmp",
                model="BalanceSheet",
                query={"symbol": "AAPL", "start_date": "2025-01-01"},
                principal=Principal(),
                ctx=_context(fetcher.canonical_model),
            )
        )
    assert client.calls == []

    with pytest.raises(provider_model_query.ProviderModelQueryUpstreamError):
        asyncio.run(
            provider_model_query.query_provider_model(
                registry=object(),
                source="fmp",
                model="BalanceSheet",
                query={"symbol": "AAPL", "period": "annual"},
                principal=Principal(),
                ctx=_context(fetcher.canonical_model),
            )
        )
    assert len(client.calls) == 1
