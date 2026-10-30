"""Offline V3 integration checks for the isolated CSI 500 sample job."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest

from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.ths.endpoints import (
    build_index_prices_request,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.index_daily_publication import build_index_daily_contract
from opendata.data.providers.ths.models import index_daily as ths_index_model
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V3,
    SourceAttemptBinding,
    current_request_scopes,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptScopeV3,
)
from opendata.pipeline import ths_index_daily_publication as job_module
from opendata.pipeline.run_identity import RunRequest
from opendata.pipeline.run_service import RunExecutionContext

if TYPE_CHECKING:
    from collections.abc import Iterator

SAMPLE_DATE = date(2026, 10, 28)
_QUERY_FIELDS = ("end", "interval", "start", "thscode")


def _source_access(
    *,
    business_date: date = SAMPLE_DATE,
) -> job_module.Csi500FuyaoV3SampleAccess:
    """Build synthetic pins for one isolated sample; no persistent gate is opened."""
    gate = object.__new__(SourceAttemptGate)
    return job_module.Csi500FuyaoV3SampleAccess(
        source_attempt_gate=gate,
        source_attempt_binding=SourceAttemptBinding(
            grant_id="fixture-csi500-v3-grant",
            expected_revision=1,
            principal_scope="fixture-csi500-v3-worker",
        ),
        business_date=business_date,
        capture_intent_id="fixture-csi500-v3-intent",
        source_policy_id="fixture-csi500-source-policy",
        source_policy_revision="fixture-1",
        source_policy_sha256="a" * 64,
        budget_binding_sha256="b" * 64,
        budget_quota_key="fixture-csi500-v3-quota",
        budget_policy_revision=1,
        budget_policy_sha256="c" * 64,
        target_identity_sha256="d" * 64,
        operator_confirms_user_declared_internal_use=True,
    )


def _request(access: job_module.Csi500FuyaoV3SampleAccess) -> RunRequest:
    """Build the job's dedicated one-date payload for a synthetic run."""
    return RunRequest(
        run_type="manual",
        **job_module.build_csi500_v3_sample_payload(
            source_access=access,
            idempotency_key="fixture-csi500-v3-one-date-001",
        ),
    )


def _context() -> RunExecutionContext:
    """Return a no-op execution context with one stable synthetic run key."""
    return RunExecutionContext(
        run_key="fixture-csi500-v3-run",
        worker="fixture-csi500-v3-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )


def test_v3_payload_and_job_are_fixed_to_one_sample_date() -> None:
    """The explicit V3 path is separate, unscheduled, and date-bound."""
    access = _source_access()
    payload = job_module.build_csi500_v3_sample_payload(
        source_access=access,
        idempotency_key="fixture-csi500-v3-one-date-001",
    )
    assert payload["partition"] == {"symbol": "000905.SH"}
    assert payload["query"] == {
        "start_date": SAMPLE_DATE.isoformat(),
        "end_date": SAMPLE_DATE.isoformat(),
    }

    contract = build_index_daily_contract("000905.SH")
    job = job_module.build_csi500_v3_sample_index_daily_job(
        DatasetContractRegistry([contract]),
        source_access=access,
    )
    assert job.reviewed is True
    assert job.one_shot_only is True
    assert job.task.enabled is False
    assert job.task.schedule is None
    assert ".fuyao-v3-sample-" in job.task.version

    with pytest.raises(ValueError, match="one exact business date"):
        access.pin_for_request(SAMPLE_DATE, date.fromordinal(SAMPLE_DATE.toordinal() + 1))


def test_v3_sample_job_pins_one_mocked_physical_request_without_v1_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-date job passes exact route/query pins and reserves only V3."""
    access = _source_access()
    contract = build_index_daily_contract("000905.SH")
    job = job_module.build_csi500_v3_sample_index_daily_job(
        DatasetContractRegistry([contract]),
        source_access=access,
    )
    request = _request(access)
    gate = access.source_attempt_gate
    reservations: list[SourceAttemptScopeV3] = []
    sends: list[httpx.Request] = []
    observed_pins: list[object] = []

    def reserve_v3(
        self: SourceAttemptGate,
        scope: SourceAttemptScopeV3,
    ) -> AttemptReservation:
        assert self is gate
        reservations.append(scope)
        return AttemptReservation(
            status=ReservationStatus.RESERVED_NEW,
            reserved_at_us=1,
        )

    def forbid_v1(_self: SourceAttemptGate, _attempt: object) -> AttemptReservation:
        pytest.fail("V3 sample route fell back to V1 reserve_attempt")

    monkeypatch.setattr(SourceAttemptGate, "reserve_attempt_v3", reserve_v3)
    monkeypatch.setattr(SourceAttemptGate, "reserve_attempt", forbid_v1)

    query_parameters = build_index_prices_request(
        symbol="000905.SH",
        start=SAMPLE_DATE,
        end=date.fromordinal(SAMPLE_DATE.toordinal() + 1),
    )
    expected_wire_query = {key: str(value) for key, value in query_parameters.items()}
    expected_summary = request_summary_sha256(
        expected_wire_query,
        allowed_fields=_QUERY_FIELDS,
    )

    def handler(prepared: httpx.Request) -> httpx.Response:
        sends.append(prepared)
        scopes = current_request_scopes()
        assert scopes
        assert all(
            scope.source_attempt_scope_version == SOURCE_ATTEMPT_SCOPE_V3 for scope in scopes
        )
        pin = scopes[0].source_attempt_scope_v3_pin
        assert pin is not None
        assert all(scope.source_attempt_scope_v3_pin == pin for scope in scopes)
        observed_pins.append(pin)
        assert pin.expected_https_host == "fuyao.aicubes.cn"
        assert pin.expected_path == "/api/a-share-index/prices/historical"
        assert pin.expected_method == "GET"
        assert pin.request_summary_allowed_fields == _QUERY_FIELDS
        assert pin.request_summary_sha256 == expected_summary
        assert prepared.method == "GET"
        assert str(prepared.url.scheme) == "https"
        assert prepared.url.host == pin.expected_https_host
        assert prepared.url.path == pin.expected_path
        assert dict(prepared.url.params.multi_items()) == expected_wire_query
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "fixture-provider-request",
                "data": {
                    "thscode": "000905.SH",
                    "item": [
                        {
                            "date_ms": shanghai_midnight_millis(SAMPLE_DATE),
                            "open_price": 10.0,
                            "high_price": 11.0,
                            "low_price": 9.0,
                            "close_price": 10.5,
                            "volume": 1000.0,
                            "turnover": 10500.0,
                        }
                    ],
                },
            },
        )

    @contextmanager
    def gated_client(
        *,
        timeout_seconds: float | None = None,
        source_attempt_gate: SourceAttemptGate | None = None,
        max_attempts: int | None = None,
    ) -> Iterator[FuyaoHttpClient]:
        assert timeout_seconds == 30
        assert source_attempt_gate is gate
        assert max_attempts == 1
        active = FuyaoHttpClient(
            credentials=FuyaoCredentials(
                "offline-fixture-key",
                base_url=DEFAULT_FUYAO_API_BASE_URL,
            ),
            mock_transport=httpx.MockTransport(handler),
            source_attempt_gate=gate,
            max_attempts=1,
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            rate_limiter=FuyaoRateLimiter(rate_per_second=1000, burst=10),
        )
        try:
            yield active
        finally:
            active.close()

    monkeypatch.setattr(ths_index_model, "client", gated_client)
    raw = job.fetch(request, _context())
    batch = job.normalize(raw, request, _context())

    assert len(sends) == 1
    assert len(reservations) == 1
    assert len(observed_pins) == 1
    assert reservations[0].attempt.source == "ths"
    assert reservations[0].attempt.canonical_model == "index_daily"
    assert reservations[0].attempt.operation == "query"
    assert reservations[0].attempt.host == "fuyao.aicubes.cn"
    assert reservations[0].attempt.run_key == _context().run_key
    assert reservations[0].request_summary_sha256 == expected_summary
    assert len(batch.records) == 1
    assert batch.records[0].business_date == SAMPLE_DATE
    assert batch.provenance["source_per_request_calendar_day_limit"] == 1
    assert batch.provenance["source_cross_execution_attempt_limit"] == "UNKNOWN"


def test_v3_sample_job_rejects_a_different_date_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job cannot reuse one sample's V3 pins for another business date."""
    access = _source_access()
    job = job_module.build_csi500_v3_sample_index_daily_job(
        DatasetContractRegistry([build_index_daily_contract("000905.SH")]),
        source_access=access,
    )
    payload = job_module.build_csi500_v3_sample_payload(
        source_access=access,
        idempotency_key="fixture-csi500-v3-one-date-mismatch-001",
    )
    payload["query"] = {
        "start_date": date(2026, 10, 29).isoformat(),
        "end_date": date(2026, 10, 29).isoformat(),
    }
    request = RunRequest(run_type="manual", **payload)

    def no_transport(*_args: object, **_kwargs: object) -> object:
        pytest.fail("date-mismatched sample reached transport")

    monkeypatch.setattr(ths_index_model, "client", no_transport)
    with pytest.raises(ValueError, match="authorized business date"):
        job.fetch(request, _context())


def test_v3_sample_access_rejects_malformed_target_or_budget_pins() -> None:
    """Malformed local identity or budget hashes cannot construct V3 access."""
    gate = object.__new__(SourceAttemptGate)
    binding = SourceAttemptBinding(
        grant_id="fixture-csi500-v3-grant",
        expected_revision=1,
        principal_scope="fixture-csi500-v3-worker",
    )
    with pytest.raises(ValueError, match="budget_policy_sha256"):
        job_module.Csi500FuyaoV3SampleAccess(
            source_attempt_gate=gate,
            source_attempt_binding=binding,
            business_date=SAMPLE_DATE,
            capture_intent_id="fixture-csi500-v3-intent",
            source_policy_id="fixture-csi500-source-policy",
            source_policy_revision="fixture-1",
            source_policy_sha256="a" * 64,
            budget_binding_sha256="b" * 64,
            budget_quota_key="fixture-csi500-v3-quota",
            budget_policy_revision=1,
            budget_policy_sha256="not-a-sha256",
            target_identity_sha256="d" * 64,
            operator_confirms_user_declared_internal_use=True,
        )
