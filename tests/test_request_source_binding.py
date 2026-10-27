"""Trusted source-attempt binding validation and scope propagation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetScopeError,
    RequestExecutionScope,
    RequestOperation,
    SourceAttemptBinding,
    current_request_scopes,
    request_execution_scope,
)


def make_binding(
    *,
    grant_id: str = "grant-sina-fzb",
    expected_revision: int = 4,
    principal_scope: str = "worker:sina-fzb",
    run_key: str | None = "scheduled:2026-10-27T01:00:00Z",
) -> SourceAttemptBinding:
    """Build a synthetic trusted binding for local scope tests."""
    return SourceAttemptBinding(
        grant_id=grant_id,
        expected_revision=expected_revision,
        principal_scope=principal_scope,
        run_key=run_key,
    )


def test_binding_accepts_valid_synthetic_values_and_is_frozen() -> None:
    binding = make_binding()
    assert binding.grant_id == "grant-sina-fzb"
    assert binding.expected_revision == 4
    assert binding.principal_scope == "worker:sina-fzb"
    assert binding.run_key == "scheduled:2026-10-27T01:00:00Z"
    with pytest.raises((AttributeError, FrozenInstanceError)):
        binding.grant_id = "other-grant"

    without_run_key = make_binding(run_key=None)
    assert without_run_key.run_key is None


def test_binding_accepts_revision_boundaries() -> None:
    assert make_binding(expected_revision=1).expected_revision == 1
    assert make_binding(expected_revision=2_147_483_647).expected_revision == 2_147_483_647


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("grant_id", ""),
        ("grant_id", " leading"),
        ("grant_id", "bad/slash"),
        ("grant_id", "x" * 129),
        ("grant_id", 7),
        ("principal_scope", "scope with spaces"),
        ("principal_scope", "雪"),
        ("principal_scope", None),
        ("run_key", "bad?query"),
        ("run_key", "x" * 129),
        ("run_key", 0),
    ],
)
def test_binding_rejects_invalid_identifiers(field: str, value: object) -> None:
    arguments: dict[str, object] = {
        "grant_id": "grant-sina-fzb",
        "expected_revision": 4,
        "principal_scope": "worker:sina-fzb",
        "run_key": "scheduled:2026-10-27T01:00:00Z",
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=field):
        SourceAttemptBinding(**arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize("revision", [True, False, 0, -1, 2_147_483_648, 1.0, "4"])
def test_binding_requires_exact_bounded_revision(revision: object) -> None:
    with pytest.raises(ValueError, match="expected_revision"):
        SourceAttemptBinding(
            grant_id="grant-sina-fzb",
            expected_revision=revision,  # type: ignore[arg-type]
            principal_scope="worker:sina-fzb",
        )


def test_outer_scope_carries_the_trusted_binding() -> None:
    binding = make_binding()
    budget = RequestBudget()
    with request_execution_scope(
        source="sina_fzb",
        canonical_model="financial_statement_process",
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=binding,
    ) as scope:
        assert scope is not None
        assert scope.source_attempt_binding is binding
        assert current_request_scopes() == (scope,)


def test_nested_scope_omission_inherits_binding_and_budget() -> None:
    binding = make_binding()
    budget = RequestBudget()
    with (
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
        ),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=None,
        ) as nested,
    ):
        assert nested is not None
        assert nested.budget is budget
        assert nested.source_attempt_binding is binding
        assert current_request_scopes()[-1] is nested


def test_nested_scope_may_repeat_the_exact_same_binding() -> None:
    binding = make_binding()
    budget = RequestBudget()
    with (
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=binding,
        ),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=make_binding(),
        ) as nested,
    ):
        assert nested is not None
        assert nested.source_attempt_binding == binding


def test_nested_scope_cannot_replace_binding() -> None:
    budget = RequestBudget()
    with (
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=make_binding(),
        ),
        pytest.raises(RequestBudgetScopeError, match="cannot add or replace"),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=make_binding(expected_revision=5),
        ),
    ):
        pytest.fail("a nested scope replaced the active source-attempt binding")


def test_nested_scope_cannot_add_binding_to_unbound_parent() -> None:
    budget = RequestBudget()
    with (
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestBudgetScopeError, match="cannot add or replace"),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=make_binding(),
        ),
    ):
        pytest.fail("a nested scope added a source-attempt binding")


def test_binding_participates_in_scope_equality() -> None:
    budget = RequestBudget()
    left = RequestExecutionScope(
        source="sina_fzb",
        canonical_model="financial_statement_process",
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=make_binding(),
    )
    right = RequestExecutionScope(
        source="sina_fzb",
        canonical_model="financial_statement_process",
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=make_binding(expected_revision=5),
    )
    assert left != right


def test_inconsistent_inherited_bindings_are_rejected() -> None:
    budget = RequestBudget()
    common = {
        "source": "sina_fzb",
        "canonical_model": "financial_statement_process",
        "operation": RequestOperation.QUERY,
        "budget": budget,
    }
    inherited = (
        RequestExecutionScope(**common, source_attempt_binding=make_binding()),  # type: ignore[arg-type]
        RequestExecutionScope(
            **common,
            source_attempt_binding=make_binding(expected_revision=5),  # type: ignore[arg-type]
        ),
    )
    with (
        pytest.raises(RequestBudgetScopeError, match="inconsistent"),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=budget,
            inherited_scopes=inherited,
        ),
    ):
        pytest.fail("inconsistent inherited bindings were accepted")


def test_reused_context_rejects_a_different_inherited_stack() -> None:
    budget = RequestBudget()
    with request_execution_scope(
        source="sina_fzb",
        canonical_model="financial_statement_process",
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=make_binding(),
    ):
        conflicting_inherited = (
            RequestExecutionScope(
                source="sina_fzb",
                canonical_model="financial_statement_process",
                operation=RequestOperation.QUERY,
                budget=budget,
                source_attempt_binding=make_binding(expected_revision=5),
            ),
        )
        with (
            pytest.raises(RequestBudgetScopeError, match="inheritance is inconsistent"),
            request_execution_scope(
                source="sina_fzb",
                canonical_model="financial_statement_process",
                operation=RequestOperation.QUERY,
                budget=budget,
                inherited_scopes=conflicting_inherited,
            ),
        ):
            pytest.fail("a conflicting inherited scope replaced the active context")


def test_scope_rejects_non_binding_values() -> None:
    with (
        pytest.raises(ValueError, match="SourceAttemptBinding"),
        request_execution_scope(
            source="sina_fzb",
            canonical_model="financial_statement_process",
            operation=RequestOperation.QUERY,
            budget=RequestBudget(),
            source_attempt_binding=object(),  # type: ignore[arg-type]
        ),
    ):
        pytest.fail("an arbitrary request value was accepted as a trusted binding")


def test_binding_is_not_silently_dropped_without_a_canonical_scope() -> None:
    with (
        pytest.raises(RequestAuthorizationError, match="canonical request scope"),
        request_execution_scope(
            source="sina_fzb",
            canonical_model=None,
            operation=RequestOperation.QUERY,
            budget=None,
            source_attempt_binding=make_binding(),
        ),
    ):
        pytest.fail("a binding was accepted without creating a canonical scope")
