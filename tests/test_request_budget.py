"""Finite trusted-code request grants and shared attempt accounting."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetScopeError,
    RequestGrant,
    RequestOperation,
    authorize_request,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
)


def make_grant(
    *,
    source: str = "fred",
    model: str = "FredSearch",
    operation: RequestOperation = RequestOperation.QUERY,
    decision: GrantDecision = GrantDecision.ALLOWED,
    task_attempts: int = 2,
    source_attempts: int = 2,
    hosts: object = ("api.stlouisfed.org",),
    conditions: object = (),
    expires_at: datetime | None = None,
) -> RequestGrant:
    """Build a short-lived, fixture-only trusted-code grant."""
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="test-evidence:offline-fixture",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts=hosts,  # type: ignore[arg-type]
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
        conditions=conditions,  # type: ignore[arg-type]
    )


@contextmanager
def active_scope(
    budget: RequestBudget,
    *,
    source: str = "fred",
    model: str | None = "FredSearch",
    operation: RequestOperation = RequestOperation.QUERY,
):
    """Install a scope for direct budget-policy probes."""
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=operation,
        budget=budget,
    ) as scope:
        yield scope


def test_default_budget_is_zero_and_has_no_authority() -> None:
    budget = RequestBudget()
    assert budget.task_attempt_limit == 0
    assert budget.source_attempt_limit == 0
    assert budget.attempts_used == 0
    with (
        active_scope(budget),
        pytest.raises(RequestAuthorizationError, match="no exact request grant"),
    ):
        authorize_request(current_request_scopes(), "fred", "api.stlouisfed.org")


@pytest.mark.parametrize("value", [True, -1, 11, 1.0, float("nan"), float("inf")])
def test_budget_task_quota_rejects_nonfinite_noninteger_and_out_of_range(value: object) -> None:
    with pytest.raises(ValueError, match="task_attempts"):
        RequestBudget(task_attempts=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, -1, 4, 1.0, float("nan"), float("inf")])
def test_budget_source_quota_rejects_nonfinite_noninteger_and_out_of_range(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="source_attempts"):
        RequestBudget(source_attempts=value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_attempts", True),
        ("task_attempts", 11),
        ("task_attempts", float("nan")),
        ("source_attempts", False),
        ("source_attempts", 4),
        ("source_attempts", -1),
    ],
)
def test_grant_quotas_are_strict_integers(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "source": "fred",
        "canonical_model": "FredSearch",
        "operation": RequestOperation.QUERY,
        "decision": GrantDecision.ALLOWED,
        "rights_evidence": "evidence://test",
        "task_attempts": 1,
        "source_attempts": 1,
        "allowed_hosts": ("api.stlouisfed.org",),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=1),
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=field):
        RequestGrant(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("grant", "source", "model", "operation", "host"),
    [
        (None, "fred", "FredSearch", RequestOperation.QUERY, "api.stlouisfed.org"),
        (GrantDecision.DENIED, "fred", "FredSearch", RequestOperation.QUERY, "api.stlouisfed.org"),
        (GrantDecision.UNKNOWN, "fred", "FredSearch", RequestOperation.QUERY, "api.stlouisfed.org"),
        ("condition", "fred", "FredSearch", RequestOperation.QUERY, "api.stlouisfed.org"),
        ("allowed", "other", "FredSearch", RequestOperation.QUERY, "api.stlouisfed.org"),
        ("allowed", "fred", "OtherModel", RequestOperation.QUERY, "api.stlouisfed.org"),
        ("allowed", "fred", "FredSearch", RequestOperation.STORE, "api.stlouisfed.org"),
        ("allowed", "fred", "FredSearch", RequestOperation.QUERY, "other.example"),
    ],
)
def test_grants_fail_closed_for_missing_or_mismatched_authority(
    grant: object,
    source: str,
    model: str,
    operation: RequestOperation,
    host: str,
) -> None:
    if grant is None:
        grants: tuple[RequestGrant, ...] = ()
    elif grant is GrantDecision.DENIED or grant is GrantDecision.UNKNOWN:
        grants = (make_grant(decision=grant),)  # type: ignore[arg-type]
    elif grant == "condition":
        grants = (make_grant(conditions=("issuer review pending",)),)
    else:
        grants = (make_grant(),)
    budget = RequestBudget(task_attempts=2, source_attempts=2, grants=grants)
    with (
        active_scope(budget, source=source, model=model, operation=operation),
        pytest.raises(RequestAuthorizationError),
    ):
        authorize_request(current_request_scopes(), source, host)


def test_expired_grant_is_rejected() -> None:
    grant = make_grant(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    budget = RequestBudget(task_attempts=2, source_attempts=2, grants=(grant,))
    with active_scope(budget), pytest.raises(RequestAuthorizationError, match="expired"):
        authorize_request(current_request_scopes(), "fred", "api.stlouisfed.org")


def test_mutable_inputs_are_copied_and_object_setattr_tampering_is_detected() -> None:
    hosts = {"api.stlouisfed.org"}
    conditions: list[str] = []
    grant = make_grant(hosts=hosts, conditions=conditions)
    grants = [grant]
    budget = RequestBudget(task_attempts=2, source_attempts=2, grants=grants)
    hosts.add("other.example")
    conditions.append("unresolved")
    grants.clear()
    assert grant.allowed_hosts == frozenset({"api.stlouisfed.org"})
    assert not grant.conditions
    assert budget.grants == (grant,)

    object.__setattr__(grant, "allowed_hosts", frozenset({"other.example"}))
    with active_scope(budget), pytest.raises(RequestAuthorizationError, match="integrity"):
        authorize_request(current_request_scopes(), "fred", "api.stlouisfed.org")


def test_nested_budget_cannot_replace_the_active_budget() -> None:
    outer = RequestBudget()
    inner = RequestBudget(task_attempts=1, source_attempts=1)
    with (
        active_scope(outer),
        pytest.raises(RequestBudgetScopeError, match="cannot replace"),
        request_execution_scope(
            source="fred",
            canonical_model="FredSearch",
            operation=RequestOperation.QUERY,
            budget=inner,
        ),
    ):
        pytest.fail("a stricter active budget was replaced")


def test_source_attempt_count_is_shared_across_models() -> None:
    budget = RequestBudget(
        task_attempts=3,
        source_attempts=2,
        grants=(
            make_grant(model="FredSearch", task_attempts=3, source_attempts=2),
            make_grant(model="FredSeries", task_attempts=3, source_attempts=2),
        ),
    )
    for model in ("FredSearch", "FredSeries"):
        with active_scope(budget, model=model):
            reserve_scoped_attempt(current_request_scopes(), "fred", "api.stlouisfed.org")
    assert budget.attempts_used == 2
    assert budget.attempts_used_for("fred") == 2
    with active_scope(budget, model="FredSearch"), pytest.raises(RequestAttemptLimitError):
        reserve_scoped_attempt(current_request_scopes(), "fred", "api.stlouisfed.org")
