"""Offline checks for conservative and explicitly bounded request profiles."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, cast

import pytest
import requests

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.request_budget import (
    MAX_BOUNDED_SOURCE_ATTEMPTS,
    MAX_BOUNDED_TASK_ATTEMPTS,
    MAX_SOURCE_ATTEMPTS,
    MAX_TASK_ATTEMPTS,
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
    authorize_request,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
)


class SimilarProfile(str, Enum):
    """A string enum with the same value, but deliberately not the API enum."""

    LIVE_SMOKE = "live_smoke"


def _grant(
    *,
    model: str = "FredSearch",
    profile: RequestBudgetProfile = RequestBudgetProfile.LIVE_SMOKE,
    task_attempts: int = 2,
    source_attempts: int = 2,
) -> RequestGrant:
    return _new_grant(
        model=model,
        profile=profile,
        task_attempts=task_attempts,
        source_attempts=source_attempts,
    )


def _new_grant(
    *,
    model: str,
    profile: object,
    task_attempts: object,
    source_attempts: object,
) -> RequestGrant:
    return RequestGrant(
        source="fred",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture",
        task_attempts=task_attempts,  # type: ignore[arg-type]
        source_attempts=source_attempts,  # type: ignore[arg-type]
        allowed_hosts=("api.stlouisfed.org",),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,  # type: ignore[arg-type]
    )


@contextmanager
def _scope(budget: RequestBudget, *, model: str = "FredSearch"):
    with request_execution_scope(
        source="fred",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        budget=budget,
    ):
        yield


@pytest.mark.parametrize(
    ("profile", "task_limit", "source_limit"),
    [
        (RequestBudgetProfile.LIVE_SMOKE, MAX_TASK_ATTEMPTS, MAX_SOURCE_ATTEMPTS),
        (
            RequestBudgetProfile.BOUNDED_REQUEST,
            MAX_BOUNDED_TASK_ATTEMPTS,
            MAX_BOUNDED_SOURCE_ATTEMPTS,
        ),
    ],
)
def test_profiles_accept_their_declared_budget_and_grant_boundaries(
    profile: RequestBudgetProfile,
    task_limit: int,
    source_limit: int,
) -> None:
    grant = _grant(
        profile=profile,
        task_attempts=task_limit,
        source_attempts=source_limit,
    )
    budget = RequestBudget(
        profile=profile,
        task_attempts=task_limit,
        source_attempts=source_limit,
        grants=(grant,),
    )

    assert budget.profile is profile
    assert budget.task_attempt_limit == task_limit
    assert budget.source_attempt_limit == source_limit
    assert budget.grants[0].profile is profile


@pytest.mark.parametrize(
    ("profile", "task_limit", "source_limit"),
    [
        (RequestBudgetProfile.LIVE_SMOKE, MAX_TASK_ATTEMPTS, MAX_SOURCE_ATTEMPTS),
        (
            RequestBudgetProfile.BOUNDED_REQUEST,
            MAX_BOUNDED_TASK_ATTEMPTS,
            MAX_BOUNDED_SOURCE_ATTEMPTS,
        ),
    ],
)
def test_profiles_reject_values_above_their_local_caps(
    profile: RequestBudgetProfile,
    task_limit: int,
    source_limit: int,
) -> None:
    with pytest.raises(ValueError, match="task_attempts"):
        RequestBudget(profile=profile, task_attempts=task_limit + 1)
    with pytest.raises(ValueError, match="source_attempts"):
        RequestBudget(profile=profile, source_attempts=source_limit + 1)
    with pytest.raises(ValueError, match="task_attempts"):
        _grant(profile=profile, task_attempts=task_limit + 1)
    with pytest.raises(ValueError, match="source_attempts"):
        _grant(profile=profile, source_attempts=source_limit + 1)


@pytest.mark.parametrize("profile", ["live_smoke", True, SimilarProfile.LIVE_SMOKE])
def test_budget_and_grant_require_the_exact_profile_enum(profile: object) -> None:
    with pytest.raises(ValueError, match="profile"):
        RequestBudget(profile=profile)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="profile"):
        _new_grant(
            model="FredSearch",
            profile=profile,
            task_attempts=1,
            source_attempts=1,
        )


def test_budget_and_grant_profiles_must_match_without_implicit_promotion() -> None:
    live_grant = _grant()
    bounded_grant = _grant(
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
        task_attempts=10,
        source_attempts=10,
    )

    with pytest.raises(ValueError, match="profiles must match"):
        RequestBudget(
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
            task_attempts=10,
            source_attempts=10,
            grants=(live_grant,),
        )
    with pytest.raises(ValueError, match="profiles must match"):
        RequestBudget(task_attempts=2, source_attempts=2, grants=(bounded_grant,))


@pytest.mark.parametrize("target", ["grant", "budget"])
def test_integrity_rejects_string_alias_mutation_of_profile(target: str) -> None:
    grant = _grant()
    budget = RequestBudget(task_attempts=2, source_attempts=2, grants=(grant,))
    object.__setattr__(
        grant if target == "grant" else budget,
        "profile" if target == "grant" else "_profile",
        "live_smoke",
    )

    with (
        _scope(budget),
        pytest.raises(RequestAuthorizationError, match="integrity"),
    ):
        authorize_request(current_request_scopes(), "fred", "api.stlouisfed.org")


@pytest.mark.parametrize("target", ["grant", "budget"])
def test_integrity_rechecks_quota_type_after_equality_alias_mutation(target: str) -> None:
    grant = _grant(task_attempts=1, source_attempts=1)
    budget = RequestBudget(task_attempts=1, source_attempts=1, grants=(grant,))
    object.__setattr__(
        grant if target == "grant" else budget,
        "task_attempts" if target == "grant" else "_task_attempt_limit",
        True,
    )

    with (
        _scope(budget),
        pytest.raises(RequestAuthorizationError, match="integrity"),
    ):
        authorize_request(current_request_scopes(), "fred", "api.stlouisfed.org")


def test_bounded_profile_uses_minimum_local_and_grant_quotas_across_models() -> None:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grants = (
        _grant(model="FredSearch", profile=profile, task_attempts=8, source_attempts=8),
        _grant(model="FredSeries", profile=profile, task_attempts=8, source_attempts=8),
    )
    budget = RequestBudget(
        profile=profile,
        task_attempts=3,
        source_attempts=2,
        grants=grants,
    )

    for model in ("FredSearch", "FredSeries"):
        with _scope(budget, model=model):
            policy = authorize_request(
                current_request_scopes(),
                "fred",
                "api.stlouisfed.org",
            )
            assert policy is not None
            assert (policy.task_attempts, policy.source_attempts) == (3, 2)
            reserve_scoped_attempt(current_request_scopes(), "fred", "api.stlouisfed.org")

    assert budget.attempts_used == 2
    assert budget.attempts_used_for("fred") == 2
    with _scope(budget), pytest.raises(RequestAttemptLimitError):
        reserve_scoped_attempt(current_request_scopes(), "fred", "api.stlouisfed.org")


class _RecordingSession:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = iter(statuses or [])
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = requests.Response()
        response.status_code = next(self.statuses, 200)
        response._content = b"{}"
        response.url = url
        return response


def _client(session: _RecordingSession, *, max_attempts: int = 3) -> GovernedHttpClient:
    return GovernedHttpClient(
        HttpClientConfig(
            max_attempts=max_attempts,
            backoff_base=0,
            backoff_jitter=0,
            rate_limit_per_host=None,
        ),
        session=cast("requests.Session", session),
        sleep=lambda _delay: None,
        raw_response_cache=None,
    )


def test_bounded_profile_sends_seven_governed_pages_with_explicit_grant() -> None:
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = _grant(
        profile=profile,
        task_attempts=MAX_BOUNDED_TASK_ATTEMPTS,
        source_attempts=MAX_BOUNDED_SOURCE_ATTEMPTS,
    )
    budget = RequestBudget(
        profile=profile,
        task_attempts=MAX_BOUNDED_TASK_ATTEMPTS,
        source_attempts=MAX_BOUNDED_SOURCE_ATTEMPTS,
        grants=(grant,),
    )
    session = _RecordingSession()
    client = _client(session)

    with _scope(budget):
        for page in range(7):
            client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"page": str(page)},
                source="fred",
            )

    assert [call["params"]["page"] for call in session.calls] == [str(i) for i in range(7)]  # type: ignore[index]
    assert budget.attempts_used == 7
    assert budget.attempts_used_for("fred") == 7


def test_live_smoke_rejects_fourth_page_before_transport() -> None:
    grant = _grant(task_attempts=MAX_TASK_ATTEMPTS, source_attempts=MAX_SOURCE_ATTEMPTS)
    budget = RequestBudget(
        task_attempts=MAX_TASK_ATTEMPTS,
        source_attempts=MAX_SOURCE_ATTEMPTS,
        grants=(grant,),
    )
    session = _RecordingSession()
    client = _client(session)

    with _scope(budget):
        for page in range(3):
            client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"page": str(page)},
                source="fred",
            )
        with pytest.raises(RequestAttemptLimitError):
            client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"page": "3"},
                source="fred",
            )

    assert len(session.calls) == 3
    assert budget.attempts_used == 3
    assert budget.attempts_used_for("fred") == 3


def test_retry_failures_consume_attempts_without_refund() -> None:
    grant = _grant(task_attempts=MAX_TASK_ATTEMPTS, source_attempts=MAX_SOURCE_ATTEMPTS)
    budget = RequestBudget(
        task_attempts=MAX_TASK_ATTEMPTS,
        source_attempts=MAX_SOURCE_ATTEMPTS,
        grants=(grant,),
    )
    session = _RecordingSession([503, 503, 503, 200])
    client = _client(session, max_attempts=3)

    with _scope(budget):
        with pytest.raises(HttpFetchError) as error:
            client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                source="fred",
            )
        assert error.value.category is FailureCategory.UPSTREAM_ERROR
        assert error.value.attempts == 3
        with pytest.raises(RequestAttemptLimitError):
            client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                source="fred",
            )

    assert len(session.calls) == 3
    assert budget.attempts_used == 3
    assert budget.attempts_used_for("fred") == 3
