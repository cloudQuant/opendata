"""Synthetic protocol tests for the Fuyao transport-attempt deployment binding."""

from __future__ import annotations

import base64
import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from opendata.pipeline import process_executor as executor

if TYPE_CHECKING:
    from pathlib import Path


def _report_generic_child_environment(
    _parameters: dict[str, executor.JsonValue],
) -> dict[str, executor.JsonValue]:
    """Return only selected synthetic environment values for the generic-path test."""
    return {
        "home": os.environ.get("HOME"),
        "marker": os.environ.get("OD204_GENERIC_INHERITANCE_TEST"),
        "cwd": os.getcwd(),
        "user": os.environ.get("USER"),
    }


def _binding(
    tmp_path: Path, *, transport_max_attempts: int = 3
) -> executor.FuyaoProcessDeploymentBinding:
    """Create a binding using synthetic paths, pins, and an API key."""
    return executor.FuyaoProcessDeploymentBinding(
        api_key="synthetic-fuyao-key-for-binding-tests",
        financial_bundle_path=str(tmp_path / "bundle.json"),
        financial_bundle_sha256="a" * 64,
        source_policy_path=str(tmp_path / "policy.json"),
        source_policy_sha256="b" * 64,
        gate_database_path=str(tmp_path / "gate.sqlite"),
        source_attempt_grant_id="synthetic-grant",
        source_attempt_expected_revision=1,
        source_attempt_principal_scope="synthetic-service",
        raw_artifact_root=str(tmp_path / "raw"),
        retention_class="ephemeral",
        source_history_limit="unknown",
        transport_max_attempts=transport_max_attempts,
    )


def _binding_document(
    tmp_path: Path,
    *,
    schema_version: int,
    transport_max_attempts: object = 3,
) -> dict[str, Any]:
    """Build the exact synthetic environment document for a selected schema."""
    document: dict[str, Any] = {
        "schema_version": schema_version,
        "financial_bundle_path": str(tmp_path / "bundle.json"),
        "financial_bundle_sha256": "a" * 64,
        "source_policy_path": str(tmp_path / "policy.json"),
        "source_policy_sha256": "b" * 64,
        "gate_database_path": str(tmp_path / "gate.sqlite"),
        "source_attempt_grant_id": "synthetic-grant",
        "source_attempt_expected_revision": 1,
        "source_attempt_principal_scope": "synthetic-service",
        "raw_artifact_root": str(tmp_path / "raw"),
        "retention_class": "ephemeral",
        "source_history_limit": "unknown",
    }
    if schema_version == 2:
        document["transport_max_attempts"] = transport_max_attempts
    return document


def _parameters() -> dict[str, object]:
    """Return a synthetic bounded Fuyao business request."""
    return {
        "instrument_id": "600000.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "start_date": "2020-01-01",
        "end_date": "2024-12-31",
        "limit": None,
        "run_key": "c" * 64,
    }


def _result(
    binding: executor.FuyaoProcessDeploymentBinding,
    parameters: dict[str, object],
    *,
    transport_max_attempts: object | None = None,
) -> dict[str, object]:
    """Return a complete synthetic child result tied to the supplied binding."""
    result: dict[str, object] = {
        "attempt_association_status": "caller_declared_unverified",
        "authorization_status": "operator_policy_allowed_unverified",
        "capture_manifest_sha256": "a" * 64,
        "coverage_assessment": "not_assessed",
        "financial_bundle_sha256": binding.financial_bundle_sha256,
        "instrument_id": parameters["instrument_id"],
        "observation_ids": ["c" * 64],
        "observation_count": 1,
        "periodicity": parameters["periodicity"],
        "prepared_capture_b64": base64.b64encode(b'{"synthetic":true}').decode("ascii"),
        "quarantined_row_count": 0,
        "request_summary_sha256": "d" * 64,
        "retention_class": binding.retention_class,
        "source_history_limit": binding.source_history_limit,
        "source_policy_sha256": binding.source_policy_sha256,
        "source_request_id_match_status": "not_checked_no_trace",
        "statement_type": parameters["statement_type"],
        "transport_max_attempts": (
            binding.transport_max_attempts
            if transport_max_attempts is None
            else transport_max_attempts
        ),
        "unit_mapping_declaration_status": "operator_declared_unverified",
        "unit_mapping_sha256_declared": "e" * 64,
        "unit_mapping_version_declared": "synthetic-v1",
    }
    return result


@pytest.mark.parametrize("transport_max_attempts", [1, 2, 3])
def test_binding_encoder_emits_v2_cap_without_api_key(
    tmp_path: Path, transport_max_attempts: int
) -> None:
    """The parent emits the explicit bounded cap and never serializes its key."""
    binding = _binding(tmp_path, transport_max_attempts=transport_max_attempts)

    encoded = executor._encode_fuyao_binding(binding)
    document = json.loads(encoded)

    assert document["schema_version"] == 2
    assert document["transport_max_attempts"] == transport_max_attempts
    assert set(document) == executor._FUYAO_BINDING_V2_FIELDS
    assert binding.api_key.encode("utf-8") not in encoded


@pytest.mark.parametrize("transport_max_attempts", [1, 2, 3])
def test_child_environment_accepts_v2_and_preserves_exact_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    transport_max_attempts: int,
) -> None:
    """The child parser accepts an exact v2 document with an exact integer cap."""
    monkeypatch.setenv(executor.FUYAO_PROCESS_API_KEY_ENV, "synthetic-child-key")
    monkeypatch.setenv(
        executor.FUYAO_PROCESS_BINDING_ENV,
        json.dumps(
            _binding_document(
                tmp_path,
                schema_version=2,
                transport_max_attempts=transport_max_attempts,
            )
        ),
    )

    binding = executor.FuyaoProcessDeploymentBinding.from_child_environment()

    assert binding.transport_max_attempts == transport_max_attempts


def test_child_environment_accepts_strict_v1_document_with_legacy_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old exact v1 field set remains readable and defaults to three sends."""
    monkeypatch.setenv(executor.FUYAO_PROCESS_API_KEY_ENV, "synthetic-child-key")
    monkeypatch.setenv(
        executor.FUYAO_PROCESS_BINDING_ENV,
        json.dumps(_binding_document(tmp_path, schema_version=1)),
    )

    binding = executor.FuyaoProcessDeploymentBinding.from_child_environment()

    assert binding.transport_max_attempts == 3


@pytest.mark.parametrize(
    "document_mutation",
    [
        lambda document: document.pop("transport_max_attempts"),
        lambda document: document.update(extra_field="unexpected"),
        lambda document: document.update(transport_max_attempts=True),
        lambda document: document.update(transport_max_attempts=0),
        lambda document: document.update(transport_max_attempts=4),
        lambda document: document.update(transport_max_attempts="1"),
    ],
    ids=["missing-cap", "extra-field", "boolean-cap", "below-range", "above-range", "string-cap"],
)
def test_child_environment_rejects_invalid_v2_field_sets_and_caps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    document_mutation,
) -> None:
    """Malformed v2 field sets or non-exact bounded caps fail closed."""
    document = _binding_document(tmp_path, schema_version=2)
    document_mutation(document)
    monkeypatch.setenv(executor.FUYAO_PROCESS_API_KEY_ENV, "synthetic-child-key")
    monkeypatch.setenv(executor.FUYAO_PROCESS_BINDING_ENV, json.dumps(document))

    with pytest.raises(ValueError, match="fuyao_process_binding_invalid"):
        executor.FuyaoProcessDeploymentBinding.from_child_environment()


@pytest.mark.parametrize(
    "document_mutation",
    [
        lambda document: document.pop("source_history_limit"),
        lambda document: document.update(extra_field="unexpected"),
        lambda document: document.update(transport_max_attempts=1),
        lambda document: document.update(schema_version=True),
    ],
    ids=["missing-legacy-field", "extra-field", "v2-field-in-v1", "boolean-version"],
)
def test_child_environment_rejects_noncanonical_v1_documents(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    document_mutation,
) -> None:
    """Only the frozen legacy v1 field set and exact integer version are accepted."""
    document = _binding_document(tmp_path, schema_version=1)
    document_mutation(document)
    monkeypatch.setenv(executor.FUYAO_PROCESS_API_KEY_ENV, "synthetic-child-key")
    monkeypatch.setenv(executor.FUYAO_PROCESS_BINDING_ENV, json.dumps(document))

    with pytest.raises(ValueError, match="fuyao_process_binding_invalid"):
        executor.FuyaoProcessDeploymentBinding.from_child_environment()


@pytest.mark.parametrize("transport_max_attempts", [True, 0, 4, 1.0, "1"])
def test_binding_constructor_rejects_non_exact_or_out_of_range_caps(
    tmp_path: Path, transport_max_attempts: object
) -> None:
    """The deployment binding itself enforces an exact integer in the safe range."""
    with pytest.raises(ValueError, match="transport_max_attempts is invalid"):
        _binding(tmp_path, transport_max_attempts=transport_max_attempts)  # type: ignore[arg-type]


def test_business_parameters_cannot_supply_the_transport_cap() -> None:
    """A business query has a closed schema and cannot override deployment policy."""
    parameters = _parameters()
    parameters["transport_max_attempts"] = 1

    with pytest.raises(ValueError, match="Fuyao query shape is invalid"):
        executor._validate_fuyao_parameters(parameters)


def test_parent_accepts_only_child_cap_matching_the_deployment_binding(
    tmp_path: Path,
) -> None:
    """The parent ties the response cap to its own binding, not child echo alone."""
    binding = _binding(tmp_path, transport_max_attempts=1)
    parameters = _parameters()
    result = _result(binding, parameters)

    capture = executor._validate_fuyao_child_result(result, parameters, binding)

    assert capture == b'{"synthetic":true}'


@pytest.mark.parametrize("transport_max_attempts", [True, 2, 4, "1"])
def test_parent_rejects_child_cap_that_is_not_exactly_bound(
    tmp_path: Path, transport_max_attempts: object
) -> None:
    """Boolean, malformed, or mismatched child cap values cannot pass validation."""
    binding = _binding(tmp_path, transport_max_attempts=1)
    parameters = _parameters()
    result = _result(binding, parameters, transport_max_attempts=transport_max_attempts)

    with pytest.raises(executor._ProtocolValueError) as error:
        executor._validate_fuyao_child_result(result, parameters, binding)

    assert error.value.code == "fuyao_result_binding_invalid"


def test_parent_rejects_child_result_missing_bound_cap(tmp_path: Path) -> None:
    """The response protocol requires the cap field and rejects its omission."""
    binding = _binding(tmp_path, transport_max_attempts=1)
    parameters = _parameters()
    result = _result(binding, parameters)
    del result["transport_max_attempts"]

    with pytest.raises(executor._ProtocolValueError) as error:
        executor._validate_fuyao_child_result(result, parameters, binding)

    assert error.value.code == "fuyao_result_shape_invalid"


def test_fuyao_executor_uses_private_home_and_scopes_key_and_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Fuyao process gets a private HOME and keeps secrets in its env only."""
    from opendata.pipeline.fuyao_financial_process import execute_fuyao_financial_request

    host_home = "synthetic-parent-home-must-not-be-inherited"
    host_user = "synthetic-parent-user-must-not-be-inherited"
    monkeypatch.setenv("HOME", host_home)
    monkeypatch.setenv("USER", host_user)
    binding = _binding(tmp_path, transport_max_attempts=1)
    runner = executor.ReviewedProcessExecutor(
        executor.ReviewedProcessRegistry(
            {executor.FUYAO_PROCESS_OPERATION_ID: execute_fuyao_financial_request}
        )
    )
    captured: dict[str, object] = {}

    def capture_run(
        operation_id: str,
        parameters: dict[str, executor.JsonValue],
        **kwargs: object,
    ) -> executor.ProcessExecutionResult:
        captured["operation_id"] = operation_id
        captured["parameters"] = parameters
        captured.update(kwargs)
        return executor.ProcessExecutionResult(
            value=_result(binding, parameters),
            stdout_discarded_bytes=0,
            stderr_discarded_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(runner, "_run_registered", capture_run)
    parameters = _parameters()

    runner.run_fuyao(binding, parameters, timeout_seconds=5)

    child_environment = captured["_child_environment"]
    assert isinstance(child_environment, dict)
    private_cwd = captured["_child_cwd"]
    assert isinstance(private_cwd, str)
    assert set(child_environment) == {
        executor.FUYAO_PROCESS_API_KEY_ENV,
        executor.FUYAO_PROCESS_BINDING_ENV,
        "HOME",
        "USER",
        "TMPDIR",
        "TMP",
        "TEMP",
    }
    assert child_environment["HOME"] == private_cwd
    assert child_environment["HOME"] != host_home
    assert child_environment["USER"] == "opendata-worker"
    assert child_environment["USER"] != host_user
    assert child_environment["TMPDIR"] == child_environment["HOME"]
    assert child_environment["TMP"] == child_environment["HOME"]
    assert child_environment["TEMP"] == child_environment["HOME"]
    assert captured["operation_id"] == executor.FUYAO_PROCESS_OPERATION_ID
    assert captured["parameters"] == parameters
    assert "transport_max_attempts" not in parameters
    binding_json = child_environment[executor.FUYAO_PROCESS_BINDING_ENV]
    assert isinstance(binding_json, str)
    assert binding.api_key not in binding_json
    assert json.loads(binding_json)["transport_max_attempts"] == 1
    secret_environment_entries = [
        name
        for name, value in child_environment.items()
        if isinstance(value, str) and binding.api_key in value
    ]
    assert secret_environment_entries == [executor.FUYAO_PROCESS_API_KEY_ENV]
    assert binding.api_key not in json.dumps(parameters)
    assert not os.path.exists(private_cwd)


def test_generic_executor_keeps_inherited_home_and_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Fuyao-only HOME change leaves generic process behavior unchanged."""
    host_home = "synthetic-parent-home-for-generic-path"
    marker = "generic-path-still-inherits-selected-values"
    host_user = "synthetic-parent-user-for-generic-path"
    monkeypatch.setenv("HOME", host_home)
    monkeypatch.setenv("USER", host_user)
    monkeypatch.setenv("OD204_GENERIC_INHERITANCE_TEST", marker)
    expected_cwd = os.getcwd()
    runner = executor.ReviewedProcessExecutor(
        executor.ReviewedProcessRegistry({"generic.environment": _report_generic_child_environment})
    )

    result = runner.run("generic.environment", {}, timeout_seconds=5)

    assert result.value == {
        "home": host_home,
        "marker": marker,
        "cwd": expected_cwd,
        "user": host_user,
    }
