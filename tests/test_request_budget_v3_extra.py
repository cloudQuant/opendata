"""Pure boundary checks for trusted V3 request-budget pins.

These tests exercise only in-memory value validation and ContextVar scopes.
They do not construct a transport client, open a database, or perform I/O.
"""

from __future__ import annotations

from typing import Any

import pytest

from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V1,
    SOURCE_ATTEMPT_SCOPE_V3,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetScopeError,
    RequestExecutionScope,
    RequestOperation,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    current_request_scopes,
    request_execution_scope,
    validate_source_attempt_scope_v3,
)


def _binding(**overrides: Any) -> SourceAttemptBinding:
    values: dict[str, Any] = {
        "grant_id": "grant-v3-boundary",
        "expected_revision": 1,
        "principal_scope": "principal-v3-boundary",
        "run_key": "run-v3-boundary",
        "expected_rights_evidence_reference": "evidence:v3-boundary",
    }
    values.update(overrides)
    return SourceAttemptBinding(**values)


def _pin(**overrides: Any) -> SourceAttemptScopeV3Pin:
    values: dict[str, Any] = {
        "capture_intent_id": "intent-v3-boundary",
        "request_summary_sha256": "a" * 64,
        "request_summary_allowed_fields": ("stock_code",),
        "expected_https_host": "fuyao.test",
        "expected_path": "/api/financial-statement",
        "expected_method": "GET",
        "source_policy_id": "policy-v3-boundary",
        "source_policy_revision": "revision-1",
        "source_policy_sha256": "b" * 64,
        "budget_binding_sha256": "c" * 64,
        "budget_quota_key": "quota-v3-boundary",
        "budget_policy_revision": 1,
        "budget_policy_sha256": "d" * 64,
    }
    values.update(overrides)
    return SourceAttemptScopeV3Pin(**values)


def _scope(
    *,
    binding: SourceAttemptBinding | None = None,
    pin: SourceAttemptScopeV3Pin | None = None,
    version: int = SOURCE_ATTEMPT_SCOPE_V3,
) -> RequestExecutionScope:
    return RequestExecutionScope(
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        budget=RequestBudget(),
        source_attempt_binding=binding if binding is not None else _binding(),
        source_attempt_scope_version=version,
        source_attempt_scope_v3_pin=pin if pin is not None else _pin(),
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expected_rights_evidence_reference": " "}, "expected_rights_evidence_reference"),
        ({"expected_rights_evidence_reference": "x" * 513}, "expected_rights_evidence_reference"),
        ({"expected_revision": True}, "expected_revision"),
        ({"expected_revision": 0}, "expected_revision"),
    ],
)
def test_binding_rejects_unusable_rights_references_and_revisions(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _binding(**change)


def test_binding_integrity_rejects_a_valid_looking_post_construction_change() -> None:
    binding = _binding()
    object.__setattr__(binding, "principal_scope", "different-principal")

    with pytest.raises(RequestAuthorizationError, match="integrity"):
        binding.validate_integrity()


def test_binding_integrity_rejects_a_malformed_post_construction_change() -> None:
    binding = _binding()
    object.__setattr__(binding, "principal_scope", "principal with spaces")

    with pytest.raises(RequestAuthorizationError, match="invalid"):
        binding.validate_integrity()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"request_summary_sha256": "A" * 64}, "lowercase SHA-256"),
        ({"budget_policy_revision": True}, "budget_policy_revision"),
        ({"request_summary_allowed_fields": ["stock_code"]}, "exact field names"),
        (
            {"request_summary_allowed_fields": ("stock_code", "stock_code")},
            "must not contain duplicates",
        ),
        (
            {"request_summary_allowed_fields": ("z_field", "a_field")},
            "sorted canonical order",
        ),
        ({"expected_https_host": "Fuyao.test"}, "canonical lowercase host"),
        ({"expected_https_host": "fuyao..test"}, "canonical lowercase host"),
        ({"expected_path": "/api/a/../b"}, "dot segments"),
        ({"expected_path": "/api/a%2Fb"}, "exact ASCII absolute path"),
        ({"expected_method": "POST"}, "exact GET method"),
    ],
)
def test_v3_pin_rejects_ambiguous_route_and_budget_metadata(
    change: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _pin(**change)


def test_v3_pin_integrity_rejects_mutation_even_when_the_new_host_is_valid() -> None:
    pin = _pin()
    object.__setattr__(pin, "expected_https_host", "other-fuyao.test")

    with pytest.raises(RequestAuthorizationError, match="integrity"):
        pin.validate_integrity()


def test_scope_validator_accepts_one_complete_exact_v3_scope() -> None:
    scope = _scope()

    binding, pin, model, operation = validate_source_attempt_scope_v3((scope,))

    assert binding is scope.source_attempt_binding
    assert pin is scope.source_attempt_scope_v3_pin
    assert model == "financial_statement"
    assert operation is RequestOperation.QUERY


def test_scope_validator_rejects_missing_or_untyped_scopes() -> None:
    with pytest.raises(RequestAuthorizationError, match="missing"):
        validate_source_attempt_scope_v3(())

    with pytest.raises(RequestAuthorizationError, match="type"):
        validate_source_attempt_scope_v3((object(),))  # type: ignore[arg-type]


def test_scope_validator_requires_a_run_key_and_exact_rights_reference() -> None:
    incomplete = _scope(binding=_binding(run_key=None, expected_rights_evidence_reference=None))

    with pytest.raises(RequestAuthorizationError, match="binding is incomplete"):
        validate_source_attempt_scope_v3((incomplete,))


def test_v3_scope_version_cannot_be_downgraded_when_nested() -> None:
    binding = _binding()
    pin = _pin()
    with (
        request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=RequestBudget(),
            source_attempt_binding=binding,
            source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
            source_attempt_scope_v3_pin=pin,
        ),
        pytest.raises(RequestBudgetScopeError, match="cannot replace its gate version"),
        request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=None,
            source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V1,
        ),
    ):
        pytest.fail("a nested V3 scope must not downgrade to V1")

    assert current_request_scopes() == ()


def test_nested_v3_scope_inherits_omitted_version_and_pin() -> None:
    binding = _binding()
    pin = _pin()
    with (
        request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=RequestBudget(),
            source_attempt_binding=binding,
            source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
            source_attempt_scope_v3_pin=pin,
        ),
        request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=None,
        ),
    ):
        nested = current_request_scopes()[-1]
        assert nested.source_attempt_scope_version == SOURCE_ATTEMPT_SCOPE_V3
        assert nested.source_attempt_binding is binding
        assert nested.source_attempt_scope_v3_pin is pin

    assert current_request_scopes() == ()


def test_v1_scope_does_not_accept_a_v3_pin_without_explicit_version() -> None:
    with (
        pytest.raises(RequestBudgetScopeError, match="requires the explicit V3 scope version"),
        request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=RequestBudget(),
            source_attempt_scope_v3_pin=_pin(),
        ),
    ):
        pytest.fail("a V1 scope must not silently carry V3 pins")
