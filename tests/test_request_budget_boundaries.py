"""Offline boundary vectors for request-budget grants and scope lifetimes."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestGrant,
    RequestOperation,
    authorize_request,
    capture_request_scopes,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
    scoped_host_slot,
    validate_scope_grants,
)


def make_grant(**overrides: object) -> RequestGrant:
    """Build an offline grant with a future expiry unless a test overrides it."""
    values: dict[str, object] = {
        "source": "quotes",
        "canonical_model": "SpotQuote",
        "operation": RequestOperation.QUERY,
        "decision": GrantDecision.ALLOWED,
        "rights_evidence": "offline-test-evidence:request-budget-boundaries",
        "task_attempts": 2,
        "source_attempts": 2,
        "allowed_hosts": ("api.example.test",),
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    values.update(overrides)
    return RequestGrant(**values)


def make_budget(*grants: RequestGrant, task_attempts: int = 4) -> RequestBudget:
    """Create a small local budget for direct authorization checks."""
    return RequestBudget(
        task_attempts=task_attempts,
        source_attempts=3,
        grants=grants,
    )


def make_scope(
    budget: RequestBudget,
    *,
    source: str = "quotes",
    model: str | None = "SpotQuote",
    operation: RequestOperation = RequestOperation.QUERY,
    deadline: float | None = None,
    cancellation: object | None = None,
) -> RequestExecutionScope:
    """Build one direct execution scope without changing ContextVar state."""
    return RequestExecutionScope(
        source=source,
        canonical_model=model,
        operation=operation,
        budget=budget,
        deadline=deadline,
        cancellation=cancellation,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"source": ""}, "source"),
        ({"source": " quotes"}, "source"),
        ({"canonical_model": ""}, "canonical_model"),
        ({"canonical_model": "SpotQuote "}, "canonical_model"),
        ({"operation": "query"}, "operation"),
        ({"decision": "ALLOWED"}, "decision"),
        ({"rights_evidence": "  "}, "rights_evidence"),
        ({"profile": "live_smoke"}, "profile"),
    ],
)
def test_grant_rejects_noncanonical_identity_and_untyped_authority_tags(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_grant(**change)


@pytest.mark.parametrize(
    "host",
    [
        "",
        " api.example.test",
        "api.example.test/path",
        "https://api.example.test",
        "api.example.test:invalid",
        "api.example.test:443",
        None,
    ],
)
def test_grant_hosts_must_be_exact_https_hostnames(host: object) -> None:
    with pytest.raises(ValueError, match="allowed_hosts"):
        make_grant(allowed_hosts=(host,))


def test_grant_host_collections_reject_strings_and_noniterables() -> None:
    with pytest.raises(ValueError, match="iterable of exact hostnames"):
        make_grant(allowed_hosts="api.example.test")
    with pytest.raises(ValueError, match="exact HTTPS hostnames"):
        make_grant(allowed_hosts=None)
    with pytest.raises(ValueError, match="at least one exact HTTPS host"):
        make_grant(allowed_hosts=())


@pytest.mark.parametrize(
    ("conditions", "message"),
    [
        ("pending", "iterable of strings"),
        (None, "iterable of strings"),
        ((" ",), "nonempty strings"),
        ((None,), "nonempty strings"),
    ],
)
def test_grant_conditions_reject_ambiguous_or_empty_values(
    conditions: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        make_grant(conditions=conditions)


class MissingOffset(tzinfo):
    """Represent a malformed tzinfo object that claims awareness without an offset."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None


class BrokenConversion(datetime):
    """A datetime whose UTC normalization reports an overflow."""

    def astimezone(self, tz: tzinfo | None = None) -> datetime:
        raise OverflowError("offline invalid timestamp vector")


def test_grant_expiry_requires_valid_aware_datetime_and_normalizes_to_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_grant(expires_at=datetime(2030, 1, 1))
    with pytest.raises(ValueError, match="valid timezone-aware"):
        make_grant(expires_at=datetime(2030, 1, 1, tzinfo=MissingOffset()))
    with pytest.raises(ValueError, match="valid timezone-aware"):
        make_grant(expires_at=BrokenConversion(2030, 1, 1, tzinfo=timezone.utc))

    source_time = datetime(2030, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    grant = make_grant(expires_at=source_time)
    assert grant.expires_at == datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("field", "value"),
    [("operation", "query"), ("decision", "ALLOWED")],
)
def test_integrity_seal_rejects_runtime_replacement_of_enum_fields(
    field: str, value: object
) -> None:
    grant = make_grant()
    budget = make_budget(grant)
    object.__setattr__(grant, field, value)

    with pytest.raises(RequestAuthorizationError, match="integrity"):
        authorize_request((make_scope(budget),), "quotes", "api.example.test")


@pytest.mark.parametrize(
    ("grants", "message"),
    [
        ("grant", "iterable of RequestGrant"),
        (None, "iterable of RequestGrant"),
        ((object(),), "only RequestGrant"),
    ],
)
def test_budget_rejects_malformed_grant_collections(grants: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RequestBudget(task_attempts=1, source_attempts=1, grants=grants)


def test_budget_rejects_duplicate_and_profile_mismatched_grants() -> None:
    grant = make_grant()
    with pytest.raises(ValueError, match="duplicate"):
        make_budget(grant, grant)

    bounded = make_grant(profile=RequestBudgetProfile.BOUNDED_REQUEST)
    with pytest.raises(ValueError, match="profiles must match"):
        make_budget(bounded)


def test_allowed_models_from_one_source_share_the_aggregate_source_quota() -> None:
    first = make_grant(canonical_model="SpotQuote", source_attempts=1)
    second = make_grant(canonical_model="DailyQuote", source_attempts=2)
    with pytest.raises(ValueError, match="share an aggregate source quota"):
        make_budget(first, second)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"decision": GrantDecision.DENIED}, "not explicitly ALLOWED"),
        ({"decision": GrantDecision.UNKNOWN}, "not explicitly ALLOWED"),
        ({"conditions": ("issuer review pending",)}, "unresolved conditions"),
        (
            {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)},
            "expired",
        ),
    ],
)
def test_nonallowed_conditional_and_expired_grants_authorize_nothing(
    change: dict[str, object], message: str
) -> None:
    budget = make_budget(make_grant(**change))
    with pytest.raises(RequestAuthorizationError, match=message):
        validate_scope_grants((make_scope(budget),))
    assert budget.attempts_used == 0


@pytest.mark.parametrize(
    ("source", "model", "operation", "host", "message"),
    [
        ("quotes", "OtherModel", RequestOperation.QUERY, "api.example.test", "no exact"),
        ("quotes", None, RequestOperation.QUERY, "api.example.test", "canonical model"),
        ("quotes", "SpotQuote", RequestOperation.STORE, "api.example.test", "no exact"),
        ("quotes", "SpotQuote", RequestOperation.QUERY, "other.example.test", "outside"),
    ],
)
def test_request_grant_matches_exact_model_operation_and_host(
    source: str,
    model: str | None,
    operation: RequestOperation,
    host: str,
    message: str,
) -> None:
    budget = make_budget(make_grant())
    scope = make_scope(budget, source=source, model=model, operation=operation)
    with pytest.raises(RequestAuthorizationError, match=message):
        authorize_request((scope,), source, host)


def test_budget_policy_is_immutable_and_integrity_seal_is_enforced() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    with pytest.raises(AttributeError, match="immutable"):
        budget.task_attempt_limit = 1

    object.__setattr__(budget, "_task_attempt_limit", 1)
    with pytest.raises(RequestAuthorizationError, match="integrity"):
        authorize_request((make_scope(budget),), "quotes", "api.example.test")


def test_budget_rejects_a_tampered_profile_that_no_longer_matches_its_grants() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    object.__setattr__(budget, "_profile", profile)
    object.__setattr__(
        budget,
        "_policy_seal",
        (profile, budget.task_attempt_limit, budget.source_attempt_limit, budget.grants),
    )

    with pytest.raises(RequestAuthorizationError, match="integrity"):
        authorize_request((make_scope(budget),), "quotes", "api.example.test")


def test_unscoped_helpers_preserve_legacy_flow_without_implicit_permission() -> None:
    entered: list[bool] = []
    assert authorize_request((), "quotes", "api.example.test") is None
    validate_scope_grants(())
    with scoped_host_slot((), "quotes", "api.example.test"):
        entered.append(True)
    reserve_scoped_attempt((), "quotes", "api.example.test")
    assert entered == [True]


def test_source_and_nested_budget_mismatches_fail_closed() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    with pytest.raises(RequestAuthorizationError, match="source differs"):
        authorize_request((make_scope(budget),), "other-source", "api.example.test")

    other_budget = make_budget()
    nested = (make_scope(budget), make_scope(other_budget))
    with pytest.raises(RequestBudgetScopeError, match="cannot replace"):
        authorize_request(nested, "quotes", "api.example.test")


def test_nested_scopes_apply_the_strictest_grant_quota_before_reservation() -> None:
    grants = (
        make_grant(canonical_model="SpotQuote", task_attempts=3, source_attempts=2),
        make_grant(canonical_model="DailyQuote", task_attempts=1, source_attempts=2),
    )
    budget = make_budget(*grants, task_attempts=4)
    scopes = (
        make_scope(budget, model="SpotQuote"),
        make_scope(budget, model="DailyQuote"),
    )
    policy = authorize_request(scopes, "quotes", "api.example.test")
    assert policy is not None
    assert (policy.task_attempts, policy.source_attempts) == (1, 2)
    reserve_scoped_attempt(scopes, "quotes", "api.example.test")
    with pytest.raises(RequestAttemptLimitError):
        reserve_scoped_attempt(scopes, "quotes", "api.example.test")
    assert budget.attempts_used == 1


def test_execution_scope_captures_inherits_and_restores_context() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    before = current_request_scopes()
    with request_execution_scope(
        source="quotes",
        canonical_model="SpotQuote",
        operation=RequestOperation.QUERY,
        budget=budget,
    ) as outer:
        assert isinstance(outer, RequestExecutionScope)
        inherited = capture_request_scopes()
        assert inherited == (outer,)
        with request_execution_scope(
            source="quotes",
            canonical_model="SpotQuote",
            operation=RequestOperation.QUERY,
            budget=None,
            inherited_scopes=inherited,
        ) as inner:
            assert isinstance(inner, RequestExecutionScope)
            assert inner.budget is budget
            assert current_request_scopes() == (outer, inner)
        assert current_request_scopes() == (outer,)
    assert current_request_scopes() == before


def test_inconsistent_scope_inheritance_is_rejected_without_leaking_context() -> None:
    budget = make_budget()
    other_budget = make_budget()
    other_scope = make_scope(other_budget)
    with (
        request_execution_scope(
            source="quotes",
            canonical_model="SpotQuote",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestBudgetScopeError, match="inheritance is inconsistent"),
        request_execution_scope(
            source="quotes",
            canonical_model="SpotQuote",
            operation=RequestOperation.QUERY,
            budget=budget,
            inherited_scopes=(other_scope,),
        ),
    ):
        pytest.fail("inconsistent inherited scope was installed")
    assert current_request_scopes() == ()


def test_missing_budget_is_zero_authority_and_legacy_scope_stays_unscoped() -> None:
    with request_execution_scope(
        source="quotes",
        canonical_model="SpotQuote",
        operation=RequestOperation.QUERY,
        budget=None,
    ) as zero_scope:
        assert isinstance(zero_scope, RequestExecutionScope)
        assert zero_scope.budget.task_attempt_limit == 0
        with pytest.raises(RequestAuthorizationError, match="no exact request grant"):
            validate_scope_grants(current_request_scopes())

    with request_execution_scope(
        source="legacy",
        canonical_model=None,
        operation=RequestOperation.QUERY,
        budget=None,
    ) as legacy_scope:
        assert legacy_scope is None
        assert current_request_scopes() == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("operation", "query", "explicit RequestOperation"),
        ("budget", object(), "must be a RequestBudget"),
    ],
)
def test_execution_scope_rejects_invalid_operation_or_budget(
    field: str, value: object, message: str
) -> None:
    arguments: dict[str, object] = {
        "source": "quotes",
        "canonical_model": "SpotQuote",
        "operation": RequestOperation.QUERY,
        "budget": make_budget(),
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=message), request_execution_scope(**arguments):
        pytest.fail("invalid scope input was accepted")


@pytest.mark.parametrize("deadline", [True, "later", float("nan")])
def test_invalid_deadlines_fail_before_grant_validation(deadline: object) -> None:
    grant = make_grant()
    budget = make_budget(grant)
    with (
        request_execution_scope(
            source="quotes",
            canonical_model="SpotQuote",
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=deadline,
        ),
        pytest.raises(RequestExecutionDeadlineError, match="deadline is invalid"),
    ):
        validate_scope_grants(current_request_scopes())


def test_cancelled_outer_scope_blocks_authorization_and_attempt_reservation() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    cancellation = threading.Event()
    cancellation.set()
    scopes = (
        make_scope(budget),
        make_scope(budget, cancellation=cancellation),
    )
    with pytest.raises(RequestExecutionCancelledError, match="cancelled"):
        reserve_scoped_attempt(scopes, "quotes", "api.example.test")
    assert budget.attempts_used == 0


def test_expired_deadline_is_distinct_from_an_invalid_deadline() -> None:
    grant = make_grant()
    budget = make_budget(grant)
    with (
        request_execution_scope(
            source="quotes",
            canonical_model="SpotQuote",
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=0.0,
        ),
        pytest.raises(RequestExecutionDeadlineError, match="deadline expired"),
    ):
        authorize_request(current_request_scopes(), "quotes", "api.example.test")


def test_host_slot_callback_failure_releases_the_shared_gate() -> None:
    budget = make_budget(make_grant())
    scopes = (make_scope(budget, deadline=time.monotonic() + 2.0),)
    callback_count = 0

    def check_caller_liveness() -> None:
        nonlocal callback_count
        callback_count += 1
        if callback_count == 2:
            raise RuntimeError("caller deadline expired")

    with (
        pytest.raises(RuntimeError, match="caller deadline expired"),
        scoped_host_slot(
            scopes,
            "quotes",
            "api.example.test",
            check_wait_liveness=check_caller_liveness,
        ),
    ):
        pytest.fail("the callback should have stopped this slot")
    assert callback_count == 2

    with scoped_host_slot(scopes, "quotes", "api.example.test"):
        pass
