"""Offline parser and factory checks for source-policy request profiles."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import pytest

from opendata.data.request_budget import (
    MAX_BOUNDED_SOURCE_ATTEMPTS,
    MAX_BOUNDED_TASK_ATTEMPTS,
    MAX_SOURCE_ATTEMPTS,
    MAX_TASK_ATTEMPTS,
    GrantDecision,
    RequestBudgetProfile,
    RequestOperation,
)
from opendata.data.source_policy import (
    SourcePolicyConfigurationError,
    build_source_policy_context,
    parse_source_policy_document,
)


class SimilarProfile(str, Enum):
    """A string enum with a matching value but the wrong enum type."""

    LIVE_SMOKE = "live_smoke"


def _policy(
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    profile: str | None = None,
    task_attempts: int = MAX_TASK_ATTEMPTS,
    source_attempts: int = MAX_SOURCE_ATTEMPTS,
) -> dict[str, object]:
    policy: dict[str, object] = {
        "policy_id": f"profile-{operation.value}",
        "source": "fred",
        "canonical_model": "FredSearch",
        "operation": operation.value,
        "principal_user_id": 17,
        "product": "offline request-policy fixture",
        "purpose": "verify profile parsing and context creation",
        "decision": GrantDecision.ALLOWED.value,
        "rights_evidence": "synthetic-offline-reference",
        "allowed_hosts": ["api.stlouisfed.org"],
        "expires_at": "2099-01-01T00:00:00Z",
        "conditions": [],
        "task_attempts": task_attempts,
        "source_attempts": source_attempts,
    }
    if profile is not None:
        policy["profile"] = profile
    return policy


def _document(policy: dict[str, object]):
    body = json.dumps(
        {"version": 1, "policies": [policy]},
        separators=(",", ":"),
    ).encode("utf-8")
    return parse_source_policy_document(body)


def _context(document: Any, operation: RequestOperation):
    return build_source_policy_context(
        document,
        source="fred",
        canonical_model="FredSearch",
        operation=operation,
        principal_user_id=17,
    )


def test_version_one_policy_without_profile_keeps_live_smoke_defaults() -> None:
    document = _document(_policy())
    policy = document.policies[0]
    context = _context(document, RequestOperation.QUERY)

    assert policy.profile is RequestBudgetProfile.LIVE_SMOKE
    assert policy.task_attempts == MAX_TASK_ATTEMPTS
    assert policy.source_attempts == MAX_SOURCE_ATTEMPTS
    assert context.request_budget is not None
    assert context.request_budget.profile is RequestBudgetProfile.LIVE_SMOKE
    assert context.request_budget.grants[0].profile is RequestBudgetProfile.LIVE_SMOKE


@pytest.mark.parametrize("operation", list(RequestOperation))
def test_bounded_profile_survives_json_parsing_and_factory_for_each_operation(
    operation: RequestOperation,
) -> None:
    document = _document(
        _policy(
            operation=operation,
            profile=RequestBudgetProfile.BOUNDED_REQUEST.value,
            task_attempts=19,
            source_attempts=11,
        )
    )
    context = _context(document, operation)

    assert document.policies[0].profile is RequestBudgetProfile.BOUNDED_REQUEST
    assert context.operation is operation
    assert context.request_budget is not None
    assert context.request_budget.profile is RequestBudgetProfile.BOUNDED_REQUEST
    assert context.request_budget.task_attempt_limit == 19
    assert context.request_budget.source_attempt_limit == 11
    assert context.request_budget.grants[0].profile is RequestBudgetProfile.BOUNDED_REQUEST


def test_bounded_profile_accepts_project_caps_and_rejects_values_above_them() -> None:
    document = _document(
        _policy(
            profile=RequestBudgetProfile.BOUNDED_REQUEST.value,
            task_attempts=MAX_BOUNDED_TASK_ATTEMPTS,
            source_attempts=MAX_BOUNDED_SOURCE_ATTEMPTS,
        )
    )
    context = _context(document, RequestOperation.QUERY)

    assert context.request_budget is not None
    assert context.request_budget.task_attempt_limit == MAX_BOUNDED_TASK_ATTEMPTS
    assert context.request_budget.source_attempt_limit == MAX_BOUNDED_SOURCE_ATTEMPTS

    for field, value in (
        ("task_attempts", MAX_BOUNDED_TASK_ATTEMPTS + 1),
        ("source_attempts", MAX_BOUNDED_SOURCE_ATTEMPTS + 1),
    ):
        with pytest.raises(SourcePolicyConfigurationError, match="invalid source policy"):
            _document(
                _policy(
                    profile=RequestBudgetProfile.BOUNDED_REQUEST.value,
                    **{field: value},
                )
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [("task_attempts", 11), ("source_attempts", 4)],
)
def test_absent_profile_still_rejects_quotas_above_live_smoke_limits(
    field: str,
    value: int,
) -> None:
    with pytest.raises(SourcePolicyConfigurationError, match="invalid source policy"):
        _document(_policy(**{field: value}))


@pytest.mark.parametrize("profile", ["BOUNDED_REQUEST", "unknown", True, None, 1, []])
def test_json_profile_must_be_an_exact_known_value(profile: object) -> None:
    policy = _policy()
    policy["profile"] = profile
    with pytest.raises(SourcePolicyConfigurationError, match="invalid source policy"):
        _document(policy)


def test_direct_policy_model_rejects_strings_and_enum_lookalikes() -> None:
    from opendata.data.source_policy import SourceUsePolicy

    raw = _policy()
    raw.update(
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        allowed_hosts=("api.stlouisfed.org",),
        conditions=(),
    )
    for profile in ("live_smoke", SimilarProfile.LIVE_SMOKE):
        with pytest.raises(ValueError):
            SourceUsePolicy.model_validate({**raw, "profile": profile}, strict=True)


@pytest.mark.parametrize("mutated_profile", ["live_smoke", SimilarProfile.LIVE_SMOKE])
def test_integrity_rejects_string_and_enum_lookalike_alias_mutations(
    mutated_profile: object,
) -> None:
    document = _document(_policy())
    object.__setattr__(document.policies[0], "profile", mutated_profile)

    with pytest.raises(SourcePolicyConfigurationError, match="invalid source policy"):
        _context(document, RequestOperation.QUERY)


def test_integrity_still_rejects_equal_boolean_quota_mutation() -> None:
    document = _document(_policy(task_attempts=1))
    object.__setattr__(document.policies[0], "task_attempts", True)

    with pytest.raises(SourcePolicyConfigurationError, match="invalid source policy"):
        _context(document, RequestOperation.QUERY)
