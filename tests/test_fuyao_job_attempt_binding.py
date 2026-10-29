"""Synthetic checks for preserving Fuyao transport retry policy in job copies."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.fuyao_financial_run_job as fuyao_financial_run_job
from opendata.pipeline.fuyao_financial_run_job import (
    _RESULT_FIELDS,
    FuyaoFinancialProcessFetchHandler,
    _copy_deployment_binding,
    _decode_process_result,
    build_fuyao_financial_job,
    build_fuyao_financial_manual_payload,
)
from opendata.pipeline.process_executor import (
    FuyaoProcessDeploymentBinding,
    ReviewedProcessExecutor,
)
from opendata.pipeline.run_identity import TaskDefinition

if TYPE_CHECKING:
    from pathlib import Path

_SYNTHETIC_API_KEY = "synthetic-fuyao-key-never-send"


def _binding(tmp_path: Path) -> FuyaoProcessDeploymentBinding:
    """Build a synthetic binding whose source paths do not exist."""
    return FuyaoProcessDeploymentBinding(
        api_key=_SYNTHETIC_API_KEY,
        financial_bundle_path=str(tmp_path / "missing-bundle.json"),
        financial_bundle_sha256="a" * 64,
        source_policy_path=str(tmp_path / "missing-policy.json"),
        source_policy_sha256="b" * 64,
        gate_database_path=str(tmp_path / "missing-gate.sqlite"),
        source_attempt_grant_id="synthetic-grant",
        source_attempt_expected_revision=1,
        source_attempt_principal_scope="synthetic-principal",
        raw_artifact_root=str(tmp_path / "missing-raw"),
        retention_class="unknown",
        source_history_limit="unknown",
    )


def _disabled_task() -> TaskDefinition:
    """Return the fixed, disabled Fuyao job identity."""
    return TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
        schedule=None,
    )


def test_binding_copy_preserves_cap_one_without_source_io_or_secret_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewed job copy retains the deployment retry cap without opening sources."""
    monkeypatch.setattr(
        ReviewedProcessExecutor,
        "run_fuyao",
        lambda *_args, **_kwargs: pytest.fail("factory must not execute a source request"),
    )
    binding = replace(_binding(tmp_path), transport_max_attempts=1)
    copied = _copy_deployment_binding(binding)

    assert copied is not binding
    assert copied.transport_max_attempts == 1
    assert _SYNTHETIC_API_KEY not in repr(binding)
    assert _SYNTHETIC_API_KEY not in repr(copied)
    assert _SYNTHETIC_API_KEY not in str(binding)
    assert _SYNTHETIC_API_KEY not in str(copied)

    job = build_fuyao_financial_job(_disabled_task(), deployment_binding=binding)
    assert job.reviewed is False
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert isinstance(job.fetch, FuyaoFinancialProcessFetchHandler)
    assert job.fetch._fuyao_binding.transport_max_attempts == 1
    assert not (tmp_path / "missing-bundle.json").exists()
    assert not (tmp_path / "missing-policy.json").exists()
    assert not (tmp_path / "missing-gate.sqlite").exists()
    assert not (tmp_path / "missing-raw").exists()


def test_binding_default_attempt_cap_is_three_and_not_business_payload_input(
    tmp_path: Path,
) -> None:
    """The old manual factory retains its default and payload cannot set deployment policy."""
    binding = _binding(tmp_path)
    assert binding.transport_max_attempts == 3
    assert _copy_deployment_binding(binding).transport_max_attempts == 3

    payload = build_fuyao_financial_manual_payload(
        "600000.SH",
        statement_type="income",
        periodicity="annual",
        limit=1,
        idempotency_key="synthetic-run-no-retry-override",
    )
    assert "transport_max_attempts" not in payload

    job = build_fuyao_financial_job(_disabled_task(), deployment_binding=binding)
    assert job.reviewed is False
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert isinstance(job.fetch, FuyaoFinancialProcessFetchHandler)
    assert job.fetch._fuyao_binding.transport_max_attempts == 3


@pytest.mark.parametrize(
    "reported_attempts",
    [2, True, 1.0, "1", None, 0, 4],
    ids=["mismatch", "bool", "float", "string", "null", "below-range", "above-range"],
)
def test_result_rejects_unbound_attempt_cap_before_capture_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_attempts: object,
) -> None:
    """Reject malformed or mismatched attempt caps before decoding capture bytes."""
    binding = replace(_binding(tmp_path), transport_max_attempts=1)
    result: dict[str, object] = dict.fromkeys(_RESULT_FIELDS)
    result.update(
        {
            "instrument_id": "600000.SH",
            "statement_type": "income",
            "periodicity": "annual",
            "prepared_capture_b64": "eA==",
            "observation_ids": [],
            "quarantined_row_count": 0,
            "transport_max_attempts": reported_attempts,
        }
    )

    def fail_if_capture_decode_is_reached(_payload: bytes) -> object:
        pytest.fail("result binding must be validated before capture decoding")

    monkeypatch.setattr(
        fuyao_financial_run_job,
        "decode_prepared_capture",
        fail_if_capture_decode_is_reached,
    )
    parameters: dict[str, object] = {
        "instrument_id": "600000.SH",
        "statement_type": "income",
        "periodicity": "annual",
        "start_date": None,
        "end_date": None,
        "limit": 1,
        "run_key": "e" * 64,
    }

    with pytest.raises(ValueError, match="fuyao_financial_process_result_invalid"):
        _decode_process_result(result, parameters=parameters, binding=binding)
