"""Synthetic tests for the non-executable Fuyao one-shot identity contract."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields, replace
from typing import Any

import pytest

import opendata.pipeline.fuyao_one_shot_contract as contract_module
from opendata.pipeline.fuyao_one_shot_contract import (
    FuyaoOneShotContract,
    FuyaoOneShotSnapshot,
    build_fuyao_one_shot_contract,
)


def _values() -> dict[str, object]:
    """Return a fixed all-synthetic closed declaration with no credentials."""
    return {
        "provider_id": "ths",
        "transport_id": "fuyao",
        "canonical_model": "financial_statement",
        "host": "fuyao.aicubes.cn",
        "operation": "query",
        "transport_max_attempts": 1,
        "financial_bundle_sha256": "a" * 64,
        "financial_bundle_validation": "loader_validated",
        "source_policy_id": "synthetic-policy",
        "source_policy_revision": "revision-1",
        "source_policy_sha256": "b" * 64,
        "source_policy_validation": "parser_validated",
        "grant_id": "synthetic-grant",
        "grant_revision": 3,
        "grant_principal": "synthetic-runner",
        "grant_validation": "local_self_declaration",
        "budget_binding_version": 2,
        "budget_binding_sha256": "c" * 64,
        "budget_quota_key": "synthetic-quota",
        "budget_policy_revision": 4,
        "budget_policy_sha256": "d" * 64,
        "budget_binding_validation": "gate_validated",
        "budget_policy_validation": "loader_validated",
        "credential_revision": 5,
        "credential_revision_validation": "operator_declared",
    }


def _contract(values: dict[str, object] | None = None) -> FuyaoOneShotContract:
    """Build a contract from a fresh synthetic declaration."""
    return build_fuyao_one_shot_contract(_values() if values is None else values)


def _synthetic_credential_marker() -> str:
    """Assemble a harmless test marker from fragments to avoid credential-like source text."""
    return "".join(("s", "k-", "synthetic", "-only-not", "-a-real-key"))


def test_snapshot_is_frozen_canonical_and_contains_every_identity_field() -> None:
    """The snapshot serializes every field in stable order with explicit provenance."""
    contract = _contract()
    expected_snapshot_fields = {field.name for field in fields(FuyaoOneShotSnapshot)}
    document = json.loads(contract.canonical_json)

    assert set(document) == expected_snapshot_fields
    assert contract.snapshot.task_version == "fuyao_financial_observation_one_shot.v1"
    assert document["grant_validation"] == "local_self_declaration"
    assert document["credential_revision_validation"] == "operator_declared"
    assert document["transport_max_attempts"] == 1
    assert document["budget_binding_version"] == 2
    assert contract.canonical_json == json.dumps(
        document, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    with pytest.raises(FrozenInstanceError):
        contract.snapshot.credential_revision = 6  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("financial_bundle_sha256", "e" * 64),
        ("financial_bundle_validation", "self_declared"),
        ("source_policy_id", "synthetic-policy-v2"),
        ("source_policy_revision", "revision-2"),
        ("source_policy_sha256", "f" * 64),
        ("source_policy_validation", "self_declared"),
        ("grant_id", "synthetic-grant-v2"),
        ("grant_revision", 4),
        ("grant_principal", "synthetic-runner-v2"),
        ("budget_binding_sha256", "1" * 64),
        ("budget_quota_key", "q2"),
        ("budget_policy_revision", 5),
        ("budget_policy_sha256", "2" * 64),
        ("budget_binding_validation", "self_declared"),
        ("budget_policy_validation", "self_declared"),
        ("credential_revision", 6),
    ],
)
def test_any_variable_identity_or_provenance_change_changes_fingerprint(
    field_name: str, replacement: object
) -> None:
    """Every mutable identity claim is included in the canonical fingerprint."""
    original = _contract()
    changed_values = _values()
    changed_values[field_name] = replacement

    assert _contract(changed_values).fingerprint_sha256 != original.fingerprint_sha256


def test_fingerprint_is_deterministic_without_credential_fields() -> None:
    """Identical non-secret snapshots have stable domain-separated fingerprints."""
    first = _contract()
    second = _contract()

    assert first.fingerprint_sha256 == second.fingerprint_sha256
    assert len(first.fingerprint_sha256) == 64
    assert "api_key" not in first.canonical_json
    assert "secret" not in first.canonical_json


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("provider_id", "other"),
        ("transport_id", "other"),
        ("canonical_model", "other"),
        ("host", "elsewhere.example"),
        ("operation", "store"),
        ("transport_max_attempts", True),
        ("transport_max_attempts", 2),
        ("budget_binding_version", True),
        ("budget_binding_version", 1),
    ],
)
def test_fixed_execution_scope_and_exact_integer_caps_are_enforced(
    field_name: str, replacement: object
) -> None:
    """A caller cannot widen provider, model, host, operation, or retry settings."""
    values = _values()
    values[field_name] = replacement

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract(values)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("financial_bundle_sha256", "A" * 64),
        ("financial_bundle_validation", "unverified"),
        ("source_policy_revision", "*"),
        ("source_policy_sha256", "bad"),
        ("source_policy_validation", "loader_validated"),
        ("source_policy_id", 42),
        ("grant_revision", True),
        ("grant_revision", 0),
        ("grant_validation", "independently_verified"),
        ("budget_binding_sha256", "c" * 63),
        ("budget_quota_key", "auto"),
        ("budget_policy_revision", False),
        ("budget_policy_revision", 0),
        ("budget_policy_sha256", "Z" * 64),
        ("budget_binding_validation", "verified_by_provider"),
        ("budget_policy_validation", "verified_by_provider"),
        ("credential_revision", "synthetic-credential-revision"),
        ("credential_revision_validation", "key_verified"),
    ],
)
def test_invalid_values_are_rejected_without_echoing_input(
    field_name: str, replacement: object
) -> None:
    """Malformed pins, identifiers, revisions, and provenance fail closed."""
    values = _values()
    values[field_name] = replacement

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract(values)


@pytest.mark.parametrize(
    "extra_field",
    ["api_key", "secret", "password", "access_token", "unknown"],
)
def test_secret_named_or_unknown_fields_are_rejected(extra_field: str) -> None:
    """Only the exact non-secret contract field set is accepted."""
    values = _values()
    canary = _synthetic_credential_marker()
    values[extra_field] = canary

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$") as exc_info:
        _contract(values)
    assert canary not in str(exc_info.value)


@pytest.mark.parametrize("field_name", ["grant_id", "grant_principal"])
def test_known_synthetic_credential_marker_is_rejected_in_identifier_fields(
    field_name: str,
) -> None:
    """A familiar synthetic key marker is denied even inside a valid-looking ID."""
    values = _values()
    canary = _synthetic_credential_marker()
    values[field_name] = canary if field_name == "grant_id" else f"runner-{canary}"

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract(values)


def test_known_synthetic_aws_marker_is_rejected_in_identifier_fields() -> None:
    """A synthetic AWS-shaped marker is denied without storing a credential literal."""
    values = _values()
    aws_marker = "".join(("AK", "IA", "0" * 8, "0" * 8))
    values["grant_principal"] = f"runner-{aws_marker}"

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract(values)


def test_missing_required_field_is_rejected() -> None:
    """A partial declaration cannot produce a deployment fingerprint."""
    values = _values()
    del values["credential_revision"]

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract(values)


def test_contract_rejects_non_dict_mappings() -> None:
    """Only a plain explicit dict crosses this narrow pure-data boundary."""
    values: Any = _values().items()

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        build_fuyao_one_shot_contract(values)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("financial_bundle_sha256", "invalid"),
        ("financial_bundle_validation", "invalid"),
        ("source_policy_id", "*"),
        ("source_policy_revision", "*"),
        ("source_policy_sha256", "invalid"),
        ("source_policy_validation", "invalid"),
        ("grant_id", "auto"),
        ("grant_revision", True),
        ("grant_principal", None),
        ("grant_validation", "invalid"),
        ("budget_binding_sha256", "invalid"),
        ("budget_quota_key", "auto"),
        ("budget_policy_revision", False),
        ("budget_policy_sha256", "invalid"),
        ("budget_binding_validation", "invalid"),
        ("budget_policy_validation", "invalid"),
        ("credential_revision", True),
        ("credential_revision_validation", "invalid"),
    ],
)
def test_direct_snapshot_construction_revalidates_each_identity_field(
    field_name: str, replacement: object
) -> None:
    """Dataclass construction enforces the same field bounds as the mapping builder."""
    snapshot = _contract().snapshot

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        replace(snapshot, **{field_name: replacement})


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("provider_id", "other"),
        ("transport_id", "other"),
        ("canonical_model", "other"),
        ("host", "elsewhere.example"),
        ("operation", "store"),
        ("schema_version", 2),
        ("task_version", "different-task.v1"),
        ("transport_max_attempts", True),
        ("budget_binding_version", True),
    ],
)
def test_snapshot_validation_rejects_fixed_scope_drift(
    field_name: str, replacement: object
) -> None:
    """Post-init validation catches fixed-field drift if frozen state is bypassed."""
    snapshot = _contract().snapshot
    object.__setattr__(snapshot, field_name, replacement)

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        snapshot.__post_init__()


def test_contract_rejects_a_non_snapshot_object() -> None:
    """The public contract accepts only the exact validated snapshot class."""
    invalid_snapshot: Any = object()

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        FuyaoOneShotContract(snapshot=invalid_snapshot)


def test_repr_redacts_identity_fields_but_exposes_version_and_fingerprint() -> None:
    """Debug representations omit grant identifiers and retain safe version metadata."""
    contract = _contract()

    assert "synthetic-grant" not in repr(contract.snapshot)
    assert "synthetic-runner" not in repr(contract)
    assert contract.snapshot.task_version in repr(contract)
    assert contract.fingerprint_sha256 in repr(contract)


def test_snapshot_fails_closed_if_canonical_serialization_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected serializer errors map to the fixed public contract error."""

    def fail_serialization(_snapshot: FuyaoOneShotSnapshot) -> str:
        """Raise a synthetic internal error without including user data."""
        raise TypeError("synthetic serializer failure")

    monkeypatch.setattr(contract_module, "_canonical_json", fail_serialization)

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract()


def test_snapshot_fails_closed_when_canonical_size_limit_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical payload size limit is enforced before returning a snapshot."""
    monkeypatch.setattr(contract_module, "_MAX_CANONICAL_BYTES", 1)

    with pytest.raises(ValueError, match="^fuyao_one_shot_contract_invalid$"):
        _contract()
