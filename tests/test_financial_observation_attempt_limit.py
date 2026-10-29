"""Offline tests for the financial fetcher's explicit physical-send ceiling."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool

from opendata.data.providers.ths.models.financial_observation_fetcher import (
    FinancialObservationFetchError,
    ThsFinancialObservationFetcher,
)
from opendata.data.providers.ths.transport.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FuyaoCredentials,
)
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    GrantDecision,
    RequestOperation,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import SourceAttemptGate
from tests import test_ths_financial_observation_fetcher as synthetic

if TYPE_CHECKING:
    from opendata.data.providers.ths.transport.http_client import FuyaoHttpResponse

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"


@pytest.fixture
def gate_engine(tmp_path: Path) -> Any:
    """Create one disposable, file-backed SQLite gate for synthetic grant rows."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'financial-observation-attempt-limit.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    spec = importlib.util.spec_from_file_location(
        "financial_observation_attempt_limit_migration_0015",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()


def _make_fetcher(
    *,
    gate: SourceAttemptGate,
    handler: Any,
    max_attempts: int | None = None,
) -> tuple[ThsFinancialObservationFetcher, list[httpx.Request]]:
    """Build a fetcher whose only possible transport is an in-process handler."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    arguments: dict[str, object] = {
        "credentials": FuyaoCredentials(
            "synthetic-key-not-a-credential",
            base_url=DEFAULT_FUYAO_API_BASE_URL,
        ),
        "source_attempt_gate": gate,
        "instrument_registry": synthetic._instrument_registry(),
        "unit_profiles": synthetic._unit_profiles(),
        "mock_transport": httpx.MockTransport(respond),
    }
    if max_attempts is not None:
        arguments["max_attempts"] = max_attempts
    fetcher = ThsFinancialObservationFetcher(**arguments)
    fetcher._client._limiter = FuyaoRateLimiter(rate_per_second=1_000, burst=1_000)
    fetcher._client._sleep = lambda _seconds: None
    fetcher._client._backoff_jitter_seconds = 0
    return fetcher, requests


def _transient_response(_request: httpx.Request) -> httpx.Response:
    """Return a real retryable Fuyao envelope without rate-limit cooldown."""
    return httpx.Response(
        200,
        json={"code": 5001, "message": "synthetic transient response", "request_id": "test"},
    )


def _run_fetch(fetcher: ThsFinancialObservationFetcher) -> Any:
    """Invoke one fixed synthetic instrument request under a synthetic trusted scope."""
    with request_execution_scope(
        source=synthetic.SOURCE,
        canonical_model=synthetic.MODEL,
        operation=RequestOperation.QUERY,
        budget=synthetic._request_budget(),
        source_attempt_binding=synthetic._binding(),
    ):
        return fetcher.fetch(
            instrument_id=synthetic.INSTRUMENT_ID,
            statement_type="income",
            periodicity="annual",
            limit=1,
        )


def _attempt_rows(engine: Any) -> list[dict[str, object]]:
    """Read only this test's isolated gate to check one reservation per send."""
    return synthetic._attempt_rows(engine)


@pytest.mark.parametrize("max_attempts", [1, 2, 3])
def test_constructor_accepts_only_the_documented_attempt_range(
    gate_engine: Any,
    max_attempts: int,
) -> None:
    """A configured cap is passed through and retained as the integrity baseline."""
    gate = SourceAttemptGate(gate_engine)
    fetcher, _requests = _make_fetcher(
        gate=gate,
        handler=_transient_response,
        max_attempts=max_attempts,
    )
    try:
        assert fetcher._client._max_attempts == max_attempts
        assert fetcher._expected_max_attempts == max_attempts
    finally:
        fetcher.close()


@pytest.mark.parametrize("max_attempts", [True, False, 0, 4, -1, 1.0, "1", None])
def test_constructor_rejects_non_integer_boolean_and_out_of_range_caps(
    gate_engine: Any,
    max_attempts: object,
) -> None:
    """Booleans and coercible values cannot silently widen or alter the cap."""
    gate = SourceAttemptGate(gate_engine)
    arguments: dict[str, object] = {
        "credentials": FuyaoCredentials(
            "synthetic-key-not-a-credential",
            base_url=DEFAULT_FUYAO_API_BASE_URL,
        ),
        "source_attempt_gate": gate,
        "instrument_registry": synthetic._instrument_registry(),
        "unit_profiles": synthetic._unit_profiles(),
        "max_attempts": max_attempts,
        "mock_transport": httpx.MockTransport(_transient_response),
    }
    with pytest.raises(ValueError, match="integer from 1 through 3"):
        ThsFinancialObservationFetcher(**arguments)


def test_omitted_cap_preserves_three_attempt_retry_behavior(gate_engine: Any) -> None:
    """Existing callers retain three physical sends and one reservation per send."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests = _make_fetcher(gate=gate, handler=_transient_response)
    try:
        assert fetcher._expected_max_attempts == 3
        with pytest.raises(FuyaoError) as error:
            _run_fetch(fetcher)

        assert error.value.category == "transient"
        assert len(requests) == fetcher._client._max_attempts == 3
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == 3
        assert [row["attempt_no"] for row in reservations] == [1, 2, 3]
        assert all(row["outcome"] == "RESERVED" for row in reservations)
    finally:
        fetcher.close()


def test_two_attempt_cap_returns_two_traced_sends_and_two_reservations(
    gate_engine: Any,
) -> None:
    """A second-attempt success stays bound to exactly two local gate reservations."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    body = synthetic._financial_body()

    def transient_then_success(_request: httpx.Request) -> httpx.Response:
        if len(requests) == 1:
            return _transient_response(_request)
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json; charset=utf-8"},
        )

    fetcher, requests = _make_fetcher(
        gate=gate,
        handler=transient_then_success,
        max_attempts=2,
    )
    try:
        result = _run_fetch(fetcher)

        assert result.status_code == 200
        assert fetcher._expected_max_attempts == 2
        assert len(requests) == 2
        assert [trace.attempt_no for trace in result.attempt_trace] == [1, 2]
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == 2
        assert [row["attempt_no"] for row in reservations] == [1, 2]
        assert all(row["outcome"] == "RESERVED" for row in reservations)
    finally:
        fetcher.close()


@pytest.mark.parametrize("failure_kind", ["retryable_response", "timeout"])
def test_one_attempt_cap_stops_after_one_physical_send_and_reservation(
    gate_engine: Any,
    failure_kind: str,
) -> None:
    """Retryable source responses and transport timeouts both stop at the explicit cap."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)

    def fail_once(request: httpx.Request) -> httpx.Response:
        if failure_kind == "timeout":
            raise httpx.ConnectTimeout("synthetic offline timeout", request=request)
        return _transient_response(request)

    fetcher, requests = _make_fetcher(gate=gate, handler=fail_once, max_attempts=1)
    try:
        with pytest.raises(FuyaoError):
            _run_fetch(fetcher)

        assert fetcher._expected_max_attempts == 1
        assert len(requests) == 1
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == 1
        assert reservations[0]["attempt_no"] == 1
        assert reservations[0]["outcome"] == "RESERVED"
    finally:
        fetcher.close()


def test_successful_response_trace_cannot_exceed_this_fetchers_cap(gate_engine: Any) -> None:
    """A transport trace claiming an attempt above this fetcher's cap is rejected."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    body = synthetic._financial_body()
    fetcher, requests = _make_fetcher(
        gate=gate,
        handler=lambda _request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json; charset=utf-8"},
        ),
        max_attempts=1,
    )
    actual_get = fetcher._client.get

    def response_with_out_of_cap_trace(*args: Any, **kwargs: Any) -> FuyaoHttpResponse:
        response = actual_get(*args, **kwargs)
        assert len(response.attempt_trace) == 1
        return replace(
            response,
            attempt_trace=(replace(response.attempt_trace[0], attempt_no=2),),
        )

    fetcher._client.get = response_with_out_of_cap_trace  # type: ignore[method-assign]
    try:
        with pytest.raises(
            FinancialObservationFetchError,
            match="transport_attempt_trace_invalid",
        ):
            _run_fetch(fetcher)

        assert len(requests) == 1
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == 1
        assert reservations[0]["attempt_no"] == 1
        assert reservations[0]["outcome"] == "RESERVED"
    finally:
        fetcher._client.get = actual_get  # type: ignore[method-assign]
        fetcher.close()


def test_missing_scope_and_rejected_scope_send_nothing(gate_engine: Any) -> None:
    """Absent and denied trusted context are both rejected before the mock send."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests = _make_fetcher(
        gate=gate,
        handler=_transient_response,
        max_attempts=1,
    )
    try:
        with pytest.raises(FinancialObservationFetchError, match="trusted_request_scope_required"):
            fetcher.fetch(
                instrument_id=synthetic.INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests == []
        assert _attempt_rows(gate_engine) == []

        with (
            request_execution_scope(
                source=synthetic.SOURCE,
                canonical_model=synthetic.MODEL,
                operation=RequestOperation.QUERY,
                budget=synthetic._request_budget(decision=GrantDecision.DENIED),
                source_attempt_binding=synthetic._binding(),
            ),
            pytest.raises(
                FinancialObservationFetchError,
                match="trusted_request_scope_unauthorized",
            ),
        ):
            fetcher.fetch(
                instrument_id=synthetic.INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests == []
        assert _attempt_rows(gate_engine) == []
    finally:
        fetcher.close()


def test_mutating_service_client_attempt_cap_fails_closed(gate_engine: Any) -> None:
    """Changing a one-shot client's mutable retry field cannot increase sends."""
    synthetic._seed_gate(gate_engine)
    gate = SourceAttemptGate(gate_engine)
    fetcher, requests = _make_fetcher(
        gate=gate,
        handler=_transient_response,
        max_attempts=1,
    )
    fetcher._client._max_attempts = 2
    try:
        with (
            pytest.raises(FinancialObservationFetchError, match="transport_attempt_limit_invalid"),
            request_execution_scope(
                source=synthetic.SOURCE,
                canonical_model=synthetic.MODEL,
                operation=RequestOperation.QUERY,
                budget=synthetic._request_budget(),
                source_attempt_binding=synthetic._binding(),
            ),
        ):
            fetcher.fetch(
                instrument_id=synthetic.INSTRUMENT_ID,
                statement_type="income",
                periodicity="annual",
                limit=1,
            )
        assert requests == []
        assert _attempt_rows(gate_engine) == []
    finally:
        fetcher.close()
