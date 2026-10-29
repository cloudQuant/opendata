"""Offline integration checks for the Fuyao deployment-bound send ceiling.

The fixed child handler is exercised with its actual v2 environment binding,
a private SQLite source-attempt gate, and HTTPX ``MockTransport``. The retryable
provider response is synthetic; the test never sends network traffic or reads
dotenv files. This verifies local wiring only, not vendor authorization or
production readiness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import sqlalchemy as sa

pytest_plugins = ("tests.test_fuyao_financial_run_job",)

from tests import test_fuyao_financial_run_job as run_job_fixtures  # noqa: E402


def _write_multi_attempt_policy(path: Path) -> str:
    """Allow three synthetic local attempts so the binding cap is decisive."""
    from opendata.data.providers.ths.transport.credentials import DEFAULT_FUYAO_API_BASE_URL
    from opendata.data.request_budget import GrantDecision, RequestOperation
    from opendata.data.source_policy import SourceUsePolicy

    host = urlsplit(DEFAULT_FUYAO_API_BASE_URL).hostname
    assert host is not None
    policy = SourceUsePolicy(
        policy_id="synthetic-attempt-binding-policy",
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        principal_user_id=17,
        product="offline integration fixture",
        purpose="verify deployment-bound transport attempts",
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic fixture only; not rights evidence",
        allowed_hosts=[host],
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        conditions=[],
        task_attempts=3,
        source_attempts=3,
    )
    encoded = json.dumps(
        {"version": 1, "policies": [policy.model_dump(mode="json")]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.write_bytes(encoded)
    path.chmod(0o600)
    return hashlib.sha256(encoded).hexdigest()


def test_fixed_child_uses_bound_single_send_cap_and_reserves_each_send(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 cap of one stops a transient error before another send or reservation."""
    monkeypatch.chdir(offline_runtime)
    from opendata.data.providers.ths.models.financial_observation_fetcher import (
        ThsFinancialObservationFetcher,
    )
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
        FuyaoProcessDeploymentBinding,
        _encode_fuyao_binding,
    )

    gate_engine, default_binding = run_job_fixtures._synthetic_binding(offline_runtime)
    try:
        assert default_binding.transport_max_attempts == 3
        policy_path = Path(default_binding.source_policy_path)
        policy_sha256 = _write_multi_attempt_policy(policy_path)
        binding = replace(
            default_binding,
            source_policy_sha256=policy_sha256,
            transport_max_attempts=1,
        )
        encoded_binding = _encode_fuyao_binding(binding)
        envelope = json.loads(encoded_binding)
        assert envelope["schema_version"] == 2
        assert envelope["transport_max_attempts"] == 1

        monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, binding.api_key)
        monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, encoded_binding.decode("utf-8"))
        child_binding = FuyaoProcessDeploymentBinding.from_child_environment()
        assert child_binding.transport_max_attempts == binding.transport_max_attempts == 1

        parameters = run_job_fixtures._valid_process_parameters()
        for caller_override in ("max_attempts", "transport_max_attempts"):
            with pytest.raises(process_module.FuyaoFinancialProcessError):
                process_module._validate_parameters({**parameters, caller_override: 3})

        sent: list[httpx.Request] = []
        builder_caps: list[int] = []

        def retryable_response(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(
                200,
                json={
                    "code": 5001,
                    "message": "synthetic transient error",
                    "request_id": "synthetic-retryable-request",
                },
            )

        mock_transport = httpx.MockTransport(retryable_response)

        def build_fetcher(
            *,
            credentials: Any,
            source_attempt_gate: Any,
            bundle: Any,
            max_attempts: int,
        ) -> ThsFinancialObservationFetcher:
            builder_caps.append(max_attempts)
            fetcher = ThsFinancialObservationFetcher(
                credentials=credentials,
                source_attempt_gate=source_attempt_gate,
                instrument_registry=bundle.instrument_registry,
                unit_profiles=bundle.unit_profiles,
                max_attempts=max_attempts,
                mock_transport=mock_transport,
            )
            # Avoid delay if a regression ignores the one-send deployment cap.
            fetcher._client._backoff_base_seconds = 0.0
            fetcher._client._backoff_jitter_seconds = 0.0
            return fetcher

        monkeypatch.setattr(process_module, "_build_fetcher", build_fetcher)
        with pytest.raises(process_module.FuyaoFinancialProcessError) as error:
            process_module.execute_fuyao_financial_request(parameters)

        assert str(error.value) == "fuyao_financial_process_failed"
        assert binding.api_key not in str(error.value)
        assert builder_caps == [1]
        assert len(sent) == 1
        with gate_engine.connect() as connection:
            reservations = (
                connection.execute(
                    sa.text(
                        "SELECT attempt_no, outcome, denial_code, run_key "
                        "FROM od_source_attempt_reservations ORDER BY attempt_no"
                    )
                )
                .mappings()
                .all()
            )
        assert len(reservations) == len(sent) == 1
        assert reservations[0]["attempt_no"] == 1
        assert reservations[0]["outcome"] == "RESERVED"
        assert reservations[0]["denial_code"] is None
        assert reservations[0]["run_key"] == parameters["run_key"]
        assert not Path(binding.raw_artifact_root).exists()
    finally:
        gate_engine.dispose()
