from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestOperation,
    current_request_scopes,
    request_execution_scope,
    reserve_scoped_attempt,
    validate_scope_grants,
)
from opendata.data.source_policy import (
    SourcePolicyConfigurationError,
    SourcePolicyDocument,
    SourceUsePolicy,
    build_source_policy_context,
    parse_source_policy_document,
)


@pytest.fixture(autouse=True)
def block_socket_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden in source policy tests")

    monkeypatch.setattr(socket.socket, "connect", reject_connect)
    monkeypatch.setattr(socket, "create_connection", reject_connect)


def policy_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "policy_id": "synthetic-policy-01",
        "source": "fred",
        "canonical_model": "SOFR",
        "operation": RequestOperation.QUERY,
        "principal_user_id": 17,
        "product": "financing research",
        "purpose": "review financing cost history",
        "decision": GrantDecision.ALLOWED,
        "rights_evidence": "synthetic-evidence-reference-01",
        "allowed_hosts": ("api.stlouisfed.org",),
        "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "conditions": (),
        "task_attempts": 4,
        "source_attempts": 2,
    }
    data.update(overrides)
    return data


def make_document(*policies: SourceUsePolicy) -> SourcePolicyDocument:
    if not policies:
        policies = (SourceUsePolicy(**policy_data()),)
    return SourcePolicyDocument(version=1, policies=policies)


def build_context(
    document: SourcePolicyDocument,
    **overrides: Any,
):
    identity: dict[str, Any] = {
        "source": "fred",
        "canonical_model": "SOFR",
        "operation": RequestOperation.QUERY,
        "principal_user_id": 17,
    }
    identity.update(overrides)
    return build_source_policy_context(document, **identity)


def test_source_policy_round_trips_python_and_json_without_mutable_collections() -> None:
    original_policy = SourceUsePolicy(
        **policy_data(
            allowed_hosts=["API.STLOUISFED.ORG"],
            conditions=["synthetic condition"],
        )
    )
    document = SourcePolicyDocument(version=1, policies=[original_policy])

    python_round_trip = SourcePolicyDocument.model_validate(
        document.model_dump(mode="python"), strict=True
    )
    json_round_trip = parse_source_policy_document(document.model_dump_json().encode("utf-8"))

    assert python_round_trip == document
    assert json_round_trip == document
    assert isinstance(document.policies, tuple)
    assert isinstance(original_policy.allowed_hosts, tuple)
    assert isinstance(original_policy.conditions, tuple)
    assert build_context(document).operation is RequestOperation.QUERY
    assert build_context(document).request_budget.grants[0].allowed_hosts == frozenset(
        {"api.stlouisfed.org"}
    )


def test_json_enums_are_exact_values_and_datetime_requires_canonical_offset() -> None:
    base = SourceUsePolicy(**policy_data()).model_dump(mode="json")
    document_json = json.dumps({"version": 1, "policies": [base]}).encode()
    assert (
        parse_source_policy_document(document_json).policies[0].operation is RequestOperation.QUERY
    )

    for invalid_expiry in (
        "2099-01-01",
        "2099-01-01T00:00:00",
        "2099-01-01T00:00:00+0000",
        "2099-01-01T00:00:00+00:00:00",
        "4102444800",
    ):
        invalid = {"version": 1, "policies": [{**base, "expires_at": invalid_expiry}]}
        with pytest.raises(SourcePolicyConfigurationError) as error:
            parse_source_policy_document(json.dumps(invalid).encode())
        assert str(error.value) == "invalid source policy configuration"
        assert error.value.__cause__ is None

    for key, value in (("operation", "Query"), ("operation", 1), ("decision", "allowed")):
        invalid = {"version": 1, "policies": [{**base, key: value}]}
        with pytest.raises(
            SourcePolicyConfigurationError, match="^invalid source policy configuration$"
        ):
            parse_source_policy_document(json.dumps(invalid).encode())


def test_all_operations_and_decisions_are_preserved_without_implicit_grants() -> None:
    for operation in RequestOperation:
        for decision in GrantDecision:
            policy = SourceUsePolicy(
                **policy_data(
                    operation=operation,
                    decision=decision,
                    allowed_hosts=("api.stlouisfed.org",)
                    if decision is GrantDecision.ALLOWED
                    else (),
                )
            )
            context = build_source_policy_context(
                SourcePolicyDocument(version=1, policies=(policy,)),
                source="fred",
                canonical_model="SOFR",
                operation=operation,
                principal_user_id=17,
            )
            assert context.operation is operation
            assert context.request_budget is not None
            assert len(context.request_budget.grants) == 1
            assert context.request_budget.grants[0].decision is decision
            serialized = SourcePolicyDocument(version=1, policies=(policy,)).model_dump_json()
            json_copy = parse_source_policy_document(serialized.encode("utf-8"))
            assert json_copy.policies[0].operation is operation
            assert json_copy.policies[0].decision is decision

    missing_decision = policy_data()
    missing_decision.pop("decision")
    with pytest.raises(ValidationError):
        SourceUsePolicy(**missing_decision)


def test_exact_policy_identity_miss_returns_fresh_zero_grant_context() -> None:
    document = make_document()
    wrong_identities = (
        {"source": "bls"},
        {"canonical_model": "OTHER"},
        {"operation": RequestOperation.STORE},
        {"principal_user_id": 18},
    )
    for wrong in wrong_identities:
        context = build_context(document, **wrong)
        assert context.request_budget is not None
        assert context.request_budget.grants == ()
        assert context.request_budget.task_attempt_limit == 0
        assert context.request_budget.source_attempt_limit == 0
        assert context.timeout == 30.0

    first = build_context(document)
    second = build_context(document)
    assert first.request_budget is not second.request_budget
    assert first.request_budget is not None and second.request_budget is not None
    assert first.request_budget.grants[0] is not second.request_budget.grants[0]


def test_policy_grants_keep_denied_unknown_expired_and_conditional_states() -> None:
    now_expired = datetime(2000, 1, 1, tzinfo=timezone.utc)
    cases = (
        policy_data(decision=GrantDecision.DENIED, allowed_hosts=()),
        policy_data(decision=GrantDecision.UNKNOWN, allowed_hosts=()),
        policy_data(expires_at=now_expired),
        policy_data(conditions=("needs review",)),
    )
    for data in cases:
        policy = SourceUsePolicy(**data)
        context = build_context(SourcePolicyDocument(version=1, policies=(policy,)))
        budget = context.request_budget
        assert budget is not None and len(budget.grants) == 1
        assert budget.grants[0].decision is policy.decision
        assert budget.grants[0].conditions == policy.conditions
        with (
            request_execution_scope(
                source="fred",
                canonical_model="SOFR",
                operation=RequestOperation.QUERY,
                budget=budget,
            ),
            pytest.raises(RequestAuthorizationError),
        ):
            validate_scope_grants(current_request_scopes())


def test_allowed_grant_uses_request_budget_host_and_attempt_validation() -> None:
    policy = SourceUsePolicy(**policy_data())
    document = SourcePolicyDocument(version=1, policies=(policy,))
    context_a = build_context(document)
    context_b = build_context(document)
    assert context_a.request_budget is not None and context_b.request_budget is not None

    with request_execution_scope(
        source="fred",
        canonical_model="SOFR",
        operation=RequestOperation.QUERY,
        budget=context_a.request_budget,
    ):
        scopes = current_request_scopes()
        validate_scope_grants(scopes)
        reserve_scoped_attempt(scopes, "fred", "api.stlouisfed.org")
        assert context_a.request_budget.attempts_used == 1
        assert context_b.request_budget.attempts_used == 0


def test_request_grant_host_rules_reject_wildcards_urls_and_invalid_hosts() -> None:
    for host in ("*", "*.example.com", "https://api.example.com", "api.example.com:443"):
        with pytest.raises(ValidationError):
            SourceUsePolicy(**policy_data(allowed_hosts=(host,)))

    uppercase_host = SourceUsePolicy(**policy_data(allowed_hosts=("API.EXAMPLE.COM",)))
    context = build_source_policy_context(
        SourcePolicyDocument(version=1, policies=(uppercase_host,)),
        source="fred",
        canonical_model="SOFR",
        operation=RequestOperation.QUERY,
        principal_user_id=17,
    )
    assert context.request_budget is not None
    assert context.request_budget.grants[0].allowed_hosts == frozenset({"api.example.com"})


def test_source_use_policy_rejects_nonexact_ids_text_and_integer_types() -> None:
    for field in (
        "policy_id",
        "source",
        "canonical_model",
        "product",
        "purpose",
        "rights_evidence",
    ):
        with pytest.raises(ValidationError):
            SourceUsePolicy(**policy_data(**{field: "synthetic\ncontrol"}))

    for field, value in (
        ("source", " auto "),
        ("source", "auto"),
        ("canonical_model", "SOFR "),
        ("canonical_model", "*"),
        ("principal_user_id", True),
        ("principal_user_id", 1.0),
        ("principal_user_id", "17"),
        ("principal_user_id", 0),
        ("principal_user_id", -1),
        ("task_attempts", True),
        ("task_attempts", 1.0),
        ("task_attempts", "2"),
        ("task_attempts", -1),
        ("task_attempts", 11),
        ("source_attempts", True),
        ("source_attempts", -1),
        ("source_attempts", 4),
        ("allowed_hosts", "api.example.com"),
    ):
        with pytest.raises(ValidationError):
            SourceUsePolicy(**policy_data(**{field: value}))

    with pytest.raises(ValidationError):
        SourceUsePolicy(**policy_data(expires_at=datetime(2099, 1, 1)))

    for field, value in (
        ("source", "fred other"),
        ("source", "class"),
        ("canonical_model", "a-b"),
    ):
        with pytest.raises(ValidationError):
            SourceUsePolicy(**policy_data(**{field: value}))


def test_document_rejects_nonexact_versions_duplicate_ids_and_duplicate_identity() -> None:
    valid = SourceUsePolicy(**policy_data())
    for version in (True, 1.0, "1", 2):
        with pytest.raises(ValidationError):
            SourcePolicyDocument(version=version, policies=(valid,))

    duplicate_id = SourceUsePolicy(**policy_data(source="bls"))
    with pytest.raises(ValidationError):
        SourcePolicyDocument(version=1, policies=(valid, duplicate_id))

    duplicate_identity = SourceUsePolicy(**policy_data(policy_id="different-id"))
    with pytest.raises(ValidationError):
        SourcePolicyDocument(version=1, policies=(valid, duplicate_identity))

    with pytest.raises(ValidationError):
        SourceUsePolicy(**{**policy_data(), "unexpected": "rejected"})
    with pytest.raises(ValidationError):
        SourcePolicyDocument(version=1, policies=(valid,), unexpected="rejected")
    with pytest.raises(ValidationError):
        valid.purpose = "cannot mutate frozen model"


def test_parse_rejects_duplicate_keys_nonfinite_invalid_utf8_oversize_and_deep_json() -> None:
    valid_body = make_document().model_dump_json().encode()
    policy_json = json.loads(valid_body)["policies"][0]
    malformed_documents = (
        b'{"version":1,"version":1,"policies":[]}',
        b'{"version":1,"policies":[{"policy_id":"a","policy_id":"b"}]}',
        b'{"version":1,"policies":[],"extra":NaN}',
        b'{"version":1,"policies":[],"extra":1e999}',
        b'{"version":1,"policies":[],"extra":-1e999}',
        json.dumps({"version": 1, "policies": [], "unexpected": "rejected"}).encode(),
        json.dumps(
            {"version": 1, "policies": [{**policy_json, "unexpected": "rejected"}]}
        ).encode(),
        b"\xff",
        valid_body + (b" " * 1_048_577),
        b'{"version":1,"policies":[],"padding":' + (b"[" * 65) + (b"]" * 65) + b"}",
    )
    for body in malformed_documents:
        with pytest.raises(SourcePolicyConfigurationError) as error:
            parse_source_policy_document(body)
        assert str(error.value) == "invalid source policy configuration"
        assert error.value.__cause__ is None
        assert "synthetic-evidence-reference-01" not in str(error.value)

    with pytest.raises(SourcePolicyConfigurationError):
        parse_source_policy_document(bytearray(valid_body))  # type: ignore[arg-type]


def test_parser_does_not_echo_evidence_or_tokenlike_text_on_failure() -> None:
    sentinel = "private-key-like-secret-DO-NOT-ECHO"
    invalid = {
        "version": 1,
        "policies": [
            {
                **SourceUsePolicy(**policy_data(rights_evidence=sentinel)).model_dump(mode="json"),
                "unknown": sentinel,
            }
        ],
    }
    with pytest.raises(SourcePolicyConfigurationError) as error:
        parse_source_policy_document(json.dumps(invalid).encode())
    assert str(error.value) == "invalid source policy configuration"
    assert sentinel not in str(error.value)
    assert error.value.__cause__ is None


def test_build_rejects_constructed_or_mutated_models_with_safe_error() -> None:
    valid = SourceUsePolicy(**policy_data())
    forged_policy = SourceUsePolicy.model_construct(**policy_data())
    forged_document = SourcePolicyDocument.model_construct(version=1, policies=(forged_policy,))
    for document in (forged_document,):
        with pytest.raises(SourcePolicyConfigurationError) as error:
            build_context(document)
        assert str(error.value) == "invalid source policy configuration"

    valid_document = SourcePolicyDocument(version=1, policies=(valid,))
    object.__setattr__(valid, "purpose", "mutated but still text")
    with pytest.raises(SourcePolicyConfigurationError):
        build_context(valid_document)

    cyclic_document = SourcePolicyDocument.model_construct(version=1, policies=())
    object.__setattr__(cyclic_document, "policies", (cyclic_document,))
    with pytest.raises(SourcePolicyConfigurationError):
        build_context(cyclic_document)


def test_model_copy_numeric_aliases_cannot_pass_integrity_or_reach_runtime_grant() -> None:
    class IntSubclass(int):
        pass

    original = SourceUsePolicy(
        **policy_data(principal_user_id=1, task_attempts=1, source_attempts=1)
    )
    numeric_fields = ("principal_user_id", "task_attempts", "source_attempts")
    for field in numeric_fields:
        for forged_value in (True, 1.0, IntSubclass(1)):
            mutated = original.model_copy(update={field: forged_value})
            with pytest.raises(ValidationError):
                SourcePolicyDocument(version=1, policies=(mutated,))

            forged_document = SourcePolicyDocument.model_construct(
                version=1,
                policies=(mutated,),
            )
            with pytest.raises(SourcePolicyConfigurationError) as error:
                build_context(forged_document)
            assert str(error.value) == "invalid source policy configuration"
            assert error.value.__cause__ is None

    for field, forged_value in (("operation", "query"), ("decision", "ALLOWED")):
        mutated = SourceUsePolicy(**policy_data()).model_copy(update={field: forged_value})
        with pytest.raises(ValidationError):
            SourcePolicyDocument(version=1, policies=(mutated,))


def test_factory_requires_finite_positive_numeric_timeout_for_match_and_miss() -> None:
    document = make_document()
    invalid_timeouts: tuple[Any, ...] = (
        None,
        True,
        False,
        float("nan"),
        float("inf"),
        float("-inf"),
        -1,
        0,
        "30",
    )
    for timeout in invalid_timeouts:
        for identity in ({}, {"source": "other_source"}):
            with pytest.raises(SourcePolicyConfigurationError) as error:
                build_context(document, timeout=timeout, **identity)
            assert str(error.value) == "invalid source policy configuration"
            assert error.value.__cause__ is None

    for timeout in (12, 12.5):
        matched = build_context(document, timeout=timeout)
        missed = build_context(document, source="other_source", timeout=timeout)
        assert matched.timeout == timeout
        assert missed.timeout == timeout
        assert missed.request_budget is not None
        assert missed.request_budget.grants == ()


def test_factory_rejects_nonexact_identity_types_without_returning_a_grant() -> None:
    document = make_document()
    for invalid in (
        {"operation": "query"},
        {"operation": True},
        {"principal_user_id": True},
        {"principal_user_id": 17.0},
        {"source": " auto "},
        {"canonical_model": "SOFR "},
    ):
        with pytest.raises(SourcePolicyConfigurationError) as error:
            build_context(document, **invalid)
        assert str(error.value) == "invalid source policy configuration"
