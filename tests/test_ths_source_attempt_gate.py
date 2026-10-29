"""Offline integration tests for Fuyao's durable source-attempt boundary.

The gate uses a fresh, test-owned file-backed SQLite database. Fuyao transport
is an exact HTTPX MockTransport, and every grant/policy below is synthetic.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json as jsonlib
import logging
import ssl
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool

from opendata.data.providers.ths.endpoints import (
    financial_statement_items,
    shanghai_midnight_millis,
)
from opendata.data.providers.ths.models import _client as ths_client_module
from opendata.data.providers.ths.models.financial_statement import (
    ThsFinancialStatementFetcher,
)
from opendata.data.providers.ths.transport import http_client as ths_http_client_module
from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.raw_response_cache import CachedRawResponse
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionScope,
    RequestGrant,
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
MIGRATION_NAME = "ths_source_attempt_gate_migration_0015_test"
SOURCE = "ths"
MODEL = "financial_statement"
PRINCIPAL = "fixture-worker"
GRANT_ID = "fixture-ths-grant"
QUOTA_KEY = "test-quota"
HOST = "fuyao.test"
NOW_US = 1_700_000_000_000_000
FAR_FUTURE_US = 253_402_300_799_999_999
MICROTOKEN = 1_000_000
_SUCCESS = jsonlib.dumps(
    {
        "code": 0,
        "message": "success",
        "request_id": "offline",
        "data": {"item": []},
    }
).encode()


def _load_migration() -> Any:
    """Load the standalone SQLite-only gate migration for this fixture."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate_engine(tmp_path: Path) -> Iterable[Any]:
    """Yield an isolated file-backed database with only migration 0015 applied."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'ths-source-attempt-gate.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
    try:
        yield engine
    finally:
        engine.dispose()


def _seed_gate(engine: Any) -> None:
    """Seed a test-only allowed grant, source binding, and shared token bucket."""
    digest = hashlib.sha256(b"synthetic offline fixture; not source-rights evidence").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, :rate, :capacity, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": QUOTA_KEY,
                "rate": MICROTOKEN,
                "capacity": 5 * MICROTOKEN,
                "reference": "synthetic:test-policy-only",
                "digest": digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, :reference, :digest, :actor, 1)"
            ),
            {
                "source": SOURCE,
                "quota": QUOTA_KEY,
                "reference": "synthetic:test-source-binding-only",
                "digest": digest,
                "actor": "offline-test",
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, :tokens, 0, :refill, 1)"
            ),
            {"quota": QUOTA_KEY, "tokens": 5 * MICROTOKEN, "refill": NOW_US},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, revoked_at_us, created_at_us, updated_at_us) "
                "VALUES (:grant, :source, :model, 'query', :principal, 'ALLOWED', 1, 1, "
                ":expires, '[]', :evidence, :digest, :note, 'offline-test', 1, NULL, 1, 1)"
            ),
            {
                "grant": GRANT_ID,
                "source": SOURCE,
                "model": MODEL,
                "principal": PRINCIPAL,
                "expires": FAR_FUTURE_US,
                "evidence": "synthetic:offline-fixture-only",
                "digest": digest,
                "note": "Synthetic test declaration; not legal authorization.",
            },
        )
        connection.execute(
            sa.text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": GRANT_ID, "host": HOST},
        )
        connection.execute(
            sa.text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-test-only')"
            ),
            {"grant": GRANT_ID},
        )


def _gate(engine: Any) -> SourceAttemptGate:
    """Bind the exact source gate to the test-owned SQLite engine."""
    return SourceAttemptGate(engine)


def _budget(*, source: str = SOURCE, model: str = MODEL) -> RequestBudget:
    """Build a separate in-memory local request budget for the test scope."""
    grant = RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="synthetic-test-reference-only",
        task_attempts=8,
        source_attempts=3,
        allowed_hosts={HOST},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return RequestBudget(task_attempts=8, source_attempts=3, grants=(grant,))


def _binding() -> SourceAttemptBinding:
    """Return a trusted fixture binding, independent of GET parameters."""
    return SourceAttemptBinding(
        grant_id=GRANT_ID,
        expected_revision=1,
        principal_scope=PRINCIPAL,
        run_key="fixture-run",
    )


def _independent_limiter() -> FuyaoRateLimiter:
    """Give each offline test client an isolated, comfortably sized bucket."""
    return FuyaoRateLimiter(rate_per_second=1_000.0, burst=1_000)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    gate: SourceAttemptGate | None = None,
    max_attempts: int = 2,
    cache: Any = None,
    max_response_bytes: int = 32 * 1024 * 1024,
    sleep: Callable[[float], None] | None = None,
    rate_limiter: FuyaoRateLimiter | None = None,
) -> FuyaoHttpClient:
    """Create a deterministic offline Fuyao client."""
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        mock_transport=httpx.MockTransport(handler),
        max_attempts=max_attempts,
        max_response_bytes=max_response_bytes,
        backoff_base_seconds=0,
        backoff_jitter_seconds=0,
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        rate_limiter=rate_limiter if rate_limiter is not None else _independent_limiter(),
        raw_response_cache=cache,
        source_attempt_gate=gate,
    )


def _streamed_response(*args: Any, content: bytes, **kwargs: Any) -> httpx.Response:
    """Build an unconsumed byte stream while preserving content-length semantics."""
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers.setdefault("Content-Length", str(len(content)))
    return httpx.Response(*args, headers=headers, stream=httpx.ByteStream(content), **kwargs)


def _streamed_json_response(*args: Any, json: Any, **kwargs: Any) -> httpx.Response:
    """Build a streamed JSON response with the headers HTTPX's eager form supplies."""
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers.setdefault("Content-Type", "application/json")
    content = jsonlib.dumps(
        json,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _streamed_response(*args, content=content, headers=headers, **kwargs)


def test_constructor_rejects_invalid_retry_transport_and_gate_inputs() -> None:
    credentials = FuyaoCredentials("offline-key", base_url=f"https://{HOST}")
    with pytest.raises(ValueError, match="max_attempts"):
        FuyaoHttpClient(credentials=credentials, max_attempts=0)

    injected = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    try:
        with pytest.raises(ValueError, match="mutually exclusive"):
            FuyaoHttpClient(
                credentials=credentials,
                client=injected,
                mock_transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
            )
    finally:
        injected.close()

    with pytest.raises(ValueError, match="exact httpx.MockTransport"):
        FuyaoHttpClient(credentials=credentials, mock_transport=object())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="SourceAttemptGate"):
        FuyaoHttpClient(credentials=credentials, source_attempt_gate=object())  # type: ignore[arg-type]


def test_endpoint_path_must_be_root_relative() -> None:
    client = _client(lambda _request: _streamed_response(200, content=_SUCCESS))
    with pytest.raises(ValueError, match="endpoint must start"):
        client.get("api/a-share/financials")


def _run_scoped_get(
    client: FuyaoHttpClient,
    *,
    binding: SourceAttemptBinding | None,
    source: str = SOURCE,
    model: str | None = MODEL,
    budget: RequestBudget | None = None,
) -> Any:
    """Run one synthetic GET under an explicit trusted request scope."""
    with request_execution_scope(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        budget=budget if budget is not None else _budget(source=source),
        source_attempt_binding=binding,
    ):
        return client.get("/v1/financial-statement", params={"symbol": "600000.SH"})


def _attempt_rows(engine: Any) -> list[dict[str, Any]]:
    """Read only the test-owned durable attempt ledger."""
    with engine.connect() as connection:
        result = connection.execute(
            sa.text(
                "SELECT * FROM od_source_attempt_reservations "
                "ORDER BY request_id, attempt_no, attempt_id"
            )
        )
        return [dict(row) for row in result.mappings().all()]


def test_gate_reserves_each_physical_retry_before_mock_transport_send(
    gate_engine: Any,
) -> None:
    """Each retry has one committed reservation and carries one trusted identity."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rows = _attempt_rows(gate_engine)
        assert len(rows) == len(sent) + 1
        assert rows[-1]["outcome"] == "RESERVED"
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ReadTimeout("synthetic retry", request=request)
        return _streamed_response(200, content=_SUCCESS)

    client = _client(handler, gate=gate)
    result = _run_scoped_get(client, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert result.status_code == 200
    assert len(sent) == len(rows) == 2
    assert [row["attempt_no"] for row in rows] == [1, 2]
    assert len({row["attempt_id"] for row in rows}) == 2
    assert len({row["request_id"] for row in rows}) == 1
    assert {row["source"] for row in rows} == {SOURCE}
    assert {row["canonical_model"] for row in rows} == {MODEL}
    assert {row["operation"] for row in rows} == {"query"}
    assert {row["principal_scope"] for row in rows} == {PRINCIPAL}
    assert {row["run_key"] for row in rows} == {"fixture-run"}
    assert {row["host"] for row in rows} == {HOST}


def test_client_mutation_during_reservation_cannot_change_gated_send(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutable auth added while reserving is caught before the physical send."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
    )
    original_reserve = gate.reserve_attempt

    def reserve_then_mutate_auth(attempt: Any) -> AttemptReservation:
        reservation = original_reserve(attempt)
        client._client.auth = httpx.BasicAuth("late-user", "late-password")
        return reservation

    monkeypatch.setattr(gate, "reserve_attempt", reserve_then_mutate_auth)
    with pytest.raises(RequestAuthorizationError, match="authentication is not allowed"):
        _run_scoped_get(client, binding=_binding())

    assert sent == []
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"


def test_gate_enablement_cannot_be_mutated_to_weaken_transport_after_reserve(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Captured gate mode still verifies TLS/retry settings after reservation."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        rate_limiter=_independent_limiter(),
        source_attempt_gate=gate,
    )
    pool = client._client._transport._pool
    sent: list[httpx.Request] = []
    monkeypatch.setattr(
        client._client,
        "send",
        lambda request, **_kwargs: (
            sent.append(request) or _streamed_response(200, content=_SUCCESS)
        ),
    )
    original_reserve = gate.reserve_attempt

    def reserve_then_weaken_transport(attempt: Any) -> AttemptReservation:
        reservation = original_reserve(attempt)
        client._source_attempt_gate = None
        pool._retries = 1
        return reservation

    monkeypatch.setattr(gate, "reserve_attempt", reserve_then_weaken_transport)
    try:
        with pytest.raises(RequestAuthorizationError, match="TLS transport"):
            _run_scoped_get(client, binding=_binding())

        assert sent == []
        rows = _attempt_rows(gate_engine)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "RESERVED"
    finally:
        client.close()


def test_gate_mode_never_reads_or_writes_raw_response_cache(gate_engine: Any) -> None:
    """A stale response cannot bypass the durable grant or be cached as gated data."""
    _seed_gate(gate_engine)

    class Cache:
        get_calls = 0
        put_calls = 0

        def get(self, **_kwargs: Any) -> Any:
            self.get_calls += 1
            raise AssertionError("gated transport must bypass cached responses")

        def put(self, **_kwargs: Any) -> None:
            self.put_calls += 1
            raise AssertionError("gated transport must not write cached responses")

    cache = Cache()
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=_gate(gate_engine),
        cache=cache,
    )
    assert _run_scoped_get(client, binding=_binding()).status_code == 200
    assert len(sent) == 1
    assert (cache.get_calls, cache.put_calls) == (0, 0)


def test_invalid_cached_response_is_invalidated_then_replaced_from_mock_send() -> None:
    class Cache:
        get_calls = 0
        invalidations = 0
        writes: list[dict[str, Any]] = []

        def get(self, **_kwargs: Any) -> CachedRawResponse:
            self.get_calls += 1
            now = time.time()
            return CachedRawResponse(
                status_code=200,
                content=b"not-json",
                encoding="utf-8",
                created_at=now,
                expires_at=now + 60,
            )

        def invalidate(self, **_kwargs: Any) -> None:
            self.invalidations += 1

        def put(self, **kwargs: Any) -> None:
            self.writes.append(kwargs)

    cache = Cache()
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        cache=cache,
    )

    result = client.get("/api/a-share/financials/income-statements")

    assert result.status_code == 200
    assert cache.get_calls == cache.invalidations == 1
    assert len(cache.writes) == 1
    assert cache.writes[0]["content"] == _SUCCESS
    assert len(sent) == 1


def test_scoped_cached_redirect_is_rejected_without_invalidation_or_send() -> None:
    class Cache:
        invalidations = 0

        def get(self, **_kwargs: Any) -> CachedRawResponse:
            now = time.time()
            return CachedRawResponse(
                status_code=302,
                content=_SUCCESS,
                encoding="utf-8",
                created_at=now,
                expires_at=now + 60,
            )

        def invalidate(self, **_kwargs: Any) -> None:
            self.invalidations += 1

        def put(self, **_kwargs: Any) -> None:
            raise AssertionError("a cached redirect must not be replaced")

    cache = Cache()
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        cache=cache,
    )
    budget = _budget()

    with pytest.raises(RequestRedirectError):
        _run_scoped_get(client, binding=None, budget=budget)

    assert cache.invalidations == 0
    assert sent == []
    assert budget.attempts_used == 0


def test_unavailable_optional_cache_configuration_does_not_block_transport(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache_module = importlib.import_module("opendata.data.raw_response_cache")

    def unavailable() -> Any:
        raise RuntimeError("private cache configuration detail")

    monkeypatch.setattr(cache_module, "get_configured_raw_response_cache", unavailable)
    monkeypatch.setattr(ths_http_client_module, "_cache_config_warning_emitted", False)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS)
    )

    with caplog.at_level(logging.WARNING, logger=ths_http_client_module.__name__):
        result = client.get("/api/a-share/financials/income-statements")

    assert result.status_code == 200
    assert len(sent) == 1
    warnings = [
        record for record in caplog.records if record.name == ths_http_client_module.__name__
    ]
    assert len(warnings) == 1
    assert warnings[0].error_type == "RuntimeError"
    assert "private cache configuration detail" not in caplog.text


def test_cache_identity_is_disabled_for_opaque_auth_proxy_or_preparation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(lambda _request: _streamed_response(200, content=_SUCCESS))
    url = f"https://{HOST}/api/a-share/financials/income-statements"
    headers = {"X-api-key": "offline-key"}

    assert client._prepare_cache_identity(url, None, headers) is not None
    client._client.auth = httpx.BasicAuth("offline-user", "offline-password")
    assert client._prepare_cache_identity(url, None, headers) is None
    client._client.auth = None
    client._client._trust_env = True
    assert client._prepare_cache_identity(url, None, headers) is None
    client._client._trust_env = False

    def preparation_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("synthetic request preparation error")

    monkeypatch.setattr(client._client, "build_request", preparation_failure)
    assert client._prepare_cache_identity(url, None, headers) is None

    class OpaqueClient(httpx.Client):
        pass

    opaque = OpaqueClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    wrapper = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        client=opaque,
        rate_limiter=_independent_limiter(),
    )
    try:
        assert wrapper._prepare_cache_identity(url, None, headers) is None
    finally:
        opaque.close()


@pytest.mark.parametrize("body", [b"not-json", b"[]"])
def test_invalid_success_response_shape_fails_closed_without_retry(body: bytes) -> None:
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=body),
        max_attempts=3,
    )

    with pytest.raises(FuyaoError) as error:
        client.get("/api/a-share/financials/income-statements")

    assert error.value.category == "envelope_invalid"
    assert len(sent) == 1


def test_oversized_success_response_is_rejected_before_parsing() -> None:
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=b"large"),
        max_attempts=3,
        max_response_bytes=4,
    )

    with pytest.raises(FuyaoError) as error:
        client.get("/api/a-share/financials/income-statements")

    assert error.value.category == "response_too_large"
    assert len(sent) == 1


def test_unscoped_timeout_retry_uses_configured_backoff_without_scope() -> None:
    sent: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            raise httpx.ReadTimeout("offline timeout", request=request)
        return _streamed_response(200, content=_SUCCESS)

    client = _client(handler, max_attempts=2, sleep=sleeps.append)

    result = client.get("/api/a-share/financials/income-statements")

    assert result.status_code == 200
    assert len(sent) == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 0


def test_numeric_retry_after_wait_is_consumed_before_second_offline_send() -> None:
    now = {"value": 0.0}
    sleeps: list[float] = []
    limiter = FuyaoRateLimiter(
        rate_per_second=10.0,
        burst=10,
        clock=lambda: now["value"],
    )
    sent: list[httpx.Request] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds + 1e-6

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            return _streamed_response(429, headers={"Retry-After": "2.5"}, content=b"")
        return _streamed_response(200, content=_SUCCESS)

    client = _client(handler, max_attempts=2, sleep=sleep, rate_limiter=limiter)

    result = client.get("/api/a-share/financials/income-statements")

    assert result.status_code == 200
    assert len(sent) == 2
    assert any(abs(seconds - 2.5) < 1e-3 for seconds in sleeps)


def test_request_logs_redact_values_and_format_ip_authorities_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class EmptyCache:
        def get(self, **_kwargs: Any) -> None:
            return None

        def put(self, **_kwargs: Any) -> None:
            return None

        def invalidate(self, **_kwargs: Any) -> None:
            return None

    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-api-key-secret", base_url="https://[2001:db8::1]"),
        mock_transport=httpx.MockTransport(
            lambda _request: _streamed_response(200, content=_SUCCESS)
        ),
        rate_limiter=_independent_limiter(),
        raw_response_cache=EmptyCache(),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.INFO, logger=ths_http_client_module.__name__):
        result = client.get(
            "/v1/log-safe",
            params={"token": "fixture-token-value", "visible": "fixture-query-value"},
        )
        client._log_request_event(
            url="https://fuyao.test:invalid/path",
            params={"secret": "fixture-secret-value"},
            request_id="offline-manual-log",
            attempt=1,
            started=time.monotonic(),
            status=None,
            failure_category="network",
        )

    request_record = next(
        record for record in caplog.records if record.request_id != "offline-manual-log"
    )
    manual_record = next(
        record for record in caplog.records if record.request_id == "offline-manual-log"
    )
    assert result.status_code == 200
    assert request_record.endpoint == "https://[2001:db8::1]/v1/log-safe"
    assert request_record.parameter_summary == {
        "keys": ("token", "visible"),
        "redacted_keys": ("token",),
    }
    assert manual_record.endpoint == "https://fuyao.test/path"
    assert "offline-api-key-secret" not in caplog.text
    assert "fixture-token-value" not in caplog.text
    assert "fixture-query-value" not in caplog.text
    assert "fixture-secret-value" not in caplog.text


def test_non_timeout_transport_error_is_classified_without_retry() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise httpx.ConnectError("offline connection failure", request=request)

    client = _client(handler, max_attempts=3)

    with pytest.raises(FuyaoError) as error:
        client.get("/api/a-share/financials/income-statements")

    assert error.value.category == "network"
    assert len(sent) == 3


def test_binding_without_gate_fails_before_cache_or_transport() -> None:
    """A trusted binding is never silently downgraded to legacy in-memory checks."""

    class Cache:
        def get(self, **_kwargs: Any) -> Any:
            raise AssertionError("missing gate must be detected before cache lookup")

        def put(self, **_kwargs: Any) -> None:
            raise AssertionError("missing gate must be detected before cache write")

    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        cache=Cache(),
    )
    with pytest.raises(RequestAuthorizationError, match="configured durable gate"):
        _run_scoped_get(client, binding=_binding())
    assert sent == []


def test_gate_without_binding_or_scope_fails_before_transport(gate_engine: Any) -> None:
    """Explicitly configuring a gate cannot silently leave it unused."""
    _seed_gate(gate_engine)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=_gate(gate_engine),
    )
    with pytest.raises(RequestAuthorizationError, match="trusted request scopes"):
        client.get("/v1/financial-statement")
    with pytest.raises(RequestAuthorizationError, match="trusted request scopes"):
        _run_scoped_get(client, binding=None)
    assert sent == []
    assert _attempt_rows(gate_engine) == []


def test_ths_client_context_forwards_only_explicit_gate(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing internal client factory forwards explicit dependency injection."""
    gate = _gate(gate_engine)
    monkeypatch.setattr(
        ths_client_module,
        "credentials",
        lambda: FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
    )
    with ths_client_module.client(source_attempt_gate=gate) as active:
        assert active._source_attempt_gate is gate
    with ths_client_module.client() as legacy_active:
        assert legacy_active._source_attempt_gate is None


def test_ths_client_context_omits_gate_kwarg_for_legacy_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-gate construction keeps the pre-gate client factory signature."""
    resolved = FuyaoCredentials("offline-key", base_url=f"https://{HOST}")
    monkeypatch.setattr(ths_client_module, "credentials", lambda: resolved)

    class LegacyClientFactory:
        def __init__(self, *, credentials: FuyaoCredentials) -> None:
            self.credentials = credentials
            self.closed = False

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(ths_client_module, "FuyaoHttpClient", LegacyClientFactory)
    with ths_client_module.client() as active:
        assert active.credentials is resolved
        assert not active.closed
    assert active.closed


def test_financial_statement_fetcher_persists_gate_before_mock_send(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trusted finance-fetcher injection reaches the durable gate before transport."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    # The registry still constructs fetchers without arguments. Production
    # registry/scheduler wiring is a separate follow-up; only trusted code
    # which explicitly constructs this fetcher can opt into this gate.
    assert ThsFinancialStatementFetcher()._source_attempt_gate is None

    report_period = date(2024, 12, 31)
    announce_date = date(2025, 3, 31)
    row: dict[str, Any] = {
        "thscode": "600000.SH",
        "ticker": "600000",
        "currency": "CNY",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": shanghai_midnight_millis(report_period),
        "report_date_ms": shanghai_midnight_millis(announce_date),
    }
    row.update(dict.fromkeys(financial_statement_items("income"), 1.0))
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == len(sent) + 1
        assert reservations[-1]["outcome"] == "RESERVED"
        assert request.url.host == HOST
        assert request.url.path == "/api/a-share/financials/income-statements"
        sent.append(request)
        return _streamed_json_response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "offline-financial-statement",
                "data": {"item": [row]},
            },
        )

    monkeypatch.setattr(
        ths_client_module,
        "credentials",
        lambda: FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
    )

    def offline_client_factory(
        *,
        credentials: FuyaoCredentials,
        source_attempt_gate: SourceAttemptGate | None = None,
        timeout_seconds: float = 20.0,
    ) -> FuyaoHttpClient:
        assert source_attempt_gate is gate
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=httpx.MockTransport(handler),
            source_attempt_gate=source_attempt_gate,
            timeout_seconds=timeout_seconds,
            max_attempts=1,
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            rate_limiter=_independent_limiter(),
        )

    monkeypatch.setattr(ths_client_module, "FuyaoHttpClient", offline_client_factory)
    fetcher = ThsFinancialStatementFetcher(source_attempt_gate=gate)
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=_budget(),
        source_attempt_binding=_binding(),
    ):
        result = fetcher.fetch(symbol="600000.SH", statement_type="income", period="annual")

    rows = _attempt_rows(gate_engine)
    assert len(sent) == len(rows) == 1
    assert len(result) == len(financial_statement_items("income"))
    assert result[0].symbol == "600000"
    assert result[0].report_period == report_period
    assert result[0].announce_date == announce_date
    assert {row["source"] for row in rows} == {SOURCE}
    assert {row["canonical_model"] for row in rows} == {MODEL}
    assert {row["operation"] for row in rows} == {"query"}
    assert {row["principal_scope"] for row in rows} == {PRINCIPAL}
    assert {row["grant_id"] for row in rows} == {GRANT_ID}
    assert {row["host"] for row in rows} == {HOST}


def test_bare_symbol_resolution_has_its_own_durable_request_reservation(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticker resolution and statement retrieval each reserve a physical call."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    report_period = date(2024, 12, 31)
    announce_date = date(2025, 3, 31)
    statement_row: dict[str, Any] = {
        "thscode": "600000.SH",
        "ticker": "600000",
        "currency": "CNY",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": shanghai_midnight_millis(report_period),
        "report_date_ms": shanghai_midnight_millis(announce_date),
    }
    statement_row.update(dict.fromkeys(financial_statement_items("income"), 1.0))
    instrument_row = {
        "thscode": "600000.SH",
        "ticker": "600000",
        "exchange": "SSE",
        "asset_type": "stock",
        "name": "fixture instrument",
        "currency": "CNY",
    }
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reservations = _attempt_rows(gate_engine)
        assert len(reservations) == len(sent) + 1
        assert reservations[-1]["outcome"] == "RESERVED"
        assert request.url.host == HOST
        sent.append(request)
        if request.url.path == "/api/meta/tickers/search":
            assert request.url.params["q"] == "600000"
            payload: dict[str, Any] = {"item": [instrument_row]}
        else:
            assert request.url.path == "/api/a-share/financials/income-statements"
            assert request.url.params["thscode"] == "600000.SH"
            payload = {"item": [statement_row]}
        return _streamed_json_response(
            200,
            json={
                "code": 0,
                "message": "success",
                "request_id": "offline-bare-symbol",
                "data": payload,
            },
        )

    monkeypatch.setattr(
        ths_client_module,
        "credentials",
        lambda: FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
    )

    def offline_client_factory(
        *,
        credentials: FuyaoCredentials,
        source_attempt_gate: SourceAttemptGate | None = None,
        timeout_seconds: float = 20.0,
    ) -> FuyaoHttpClient:
        assert source_attempt_gate is gate
        return FuyaoHttpClient(
            credentials=credentials,
            mock_transport=httpx.MockTransport(handler),
            source_attempt_gate=source_attempt_gate,
            timeout_seconds=timeout_seconds,
            max_attempts=1,
            backoff_base_seconds=0,
            backoff_jitter_seconds=0,
            sleep=lambda _seconds: None,
            rate_limiter=_independent_limiter(),
        )

    monkeypatch.setattr(ths_client_module, "FuyaoHttpClient", offline_client_factory)
    fetcher = ThsFinancialStatementFetcher(source_attempt_gate=gate)
    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=_budget(),
        source_attempt_binding=_binding(),
    ):
        result = fetcher.fetch(symbol="600000", statement_type="income", period="annual")

    rows = _attempt_rows(gate_engine)
    assert [request.url.path for request in sent] == [
        "/api/meta/tickers/search",
        "/api/a-share/financials/income-statements",
    ]
    assert len(rows) == len(sent) == 2
    assert [row["attempt_no"] for row in rows] == [1, 1]
    assert len({row["request_id"] for row in rows}) == 2
    assert {row["source"] for row in rows} == {SOURCE}
    assert {row["canonical_model"] for row in rows} == {MODEL}
    assert {row["operation"] for row in rows} == {"query"}
    assert {row["principal_scope"] for row in rows} == {PRINCIPAL}
    assert len(result) == len(financial_statement_items("income"))


def test_unbound_legacy_scoped_request_still_works_without_gate() -> None:
    """Existing scoped callers remain compatible when durable gating is not enabled."""
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS)
    )
    result = _run_scoped_get(client, binding=None)
    assert result.status_code == 200
    assert len(sent) == 1


def test_empty_gate_denies_without_sending_and_does_not_retry(gate_engine: Any) -> None:
    """An empty/unconfigured durable gate denies the send before the retry loop."""
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=_gate(gate_engine),
        max_attempts=3,
    )
    with pytest.raises(RequestAuthorizationError, match="did not authorize"):
        _run_scoped_get(client, binding=_binding())
    assert sent == []
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "DENIED"


def test_gate_exception_is_sanitized_and_never_retried(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown commit outcomes cannot be retried or expose storage details."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)

    def unavailable(_attempt: Any) -> Any:
        raise SourceAttemptGateUnavailable("synthetic private database detail")

    monkeypatch.setattr(gate, "reserve_attempt", unavailable)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
        max_attempts=3,
    )
    with pytest.raises(RequestAuthorizationError) as raised:
        _run_scoped_get(client, binding=_binding())
    assert str(raised.value) == "source attempt gate did not authorize transport"
    assert raised.value.__cause__ is None
    assert sent == []
    assert _attempt_rows(gate_engine) == []


@pytest.mark.parametrize("status", [ReservationStatus.DENIED, ReservationStatus.DUPLICATE])
def test_non_new_gate_reservation_never_sends(
    gate_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: ReservationStatus,
) -> None:
    """Only the exact one-use RESERVED_NEW result is accepted for transport."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    monkeypatch.setattr(
        gate,
        "reserve_attempt",
        lambda _attempt: AttemptReservation(status=status, denial_code="fixture"),
    )
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
    )
    with pytest.raises(RequestAuthorizationError, match="did not authorize"):
        _run_scoped_get(client, binding=_binding())
    assert sent == []


def test_durable_reservation_acknowledgement_loss_cannot_reach_transport(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed-but-ambiguous reservation is not safe to use or retry."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    original_commit = gate._commit

    def commit_then_lose_ack(connection: Any) -> None:
        original_commit(connection)
        raise SourceAttemptGateUnavailable("synthetic commit acknowledgement loss")

    monkeypatch.setattr(gate, "_commit", commit_then_lose_ack)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
        max_attempts=3,
    )
    with pytest.raises(RequestAuthorizationError):
        _run_scoped_get(client, binding=_binding())
    assert sent == []
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"


def test_scope_identity_mismatch_is_rejected_before_gate_or_send(gate_engine: Any) -> None:
    """Nested trusted scopes cannot change source/model under one durable binding."""
    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
    )
    budget = _budget()
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=_binding(),
        ),
        request_execution_scope(
            source=SOURCE,
            canonical_model="DifferentFixtureModel",
            operation=RequestOperation.QUERY,
            budget=budget,
        ),
        pytest.raises(RequestAuthorizationError, match="consistent trusted"),
    ):
        client.get("/v1/financial-statement")
    assert sent == []
    assert _attempt_rows(gate_engine) == []


@pytest.mark.parametrize(
    ("overrides", "nested"),
    [
        ({"source": "akshare"}, False),
        ({"canonical_model": ""}, False),
        ({"canonical_model": " financial_statement"}, False),
        ({"operation": "query"}, False),
        ({"source_attempt_binding": None}, False),
        ({"source_attempt_binding": object()}, False),
        ({"source": "akshare"}, True),
        ({"canonical_model": "another_model"}, True),
        ({"operation": RequestOperation.STORE}, True),
        (
            {
                "source_attempt_binding": SourceAttemptBinding(
                    grant_id=GRANT_ID,
                    expected_revision=2,
                    principal_scope=PRINCIPAL,
                    run_key="fixture-run",
                )
            },
            True,
        ),
    ],
)
def test_source_attempt_gate_rejects_malformed_or_inconsistent_scope_stacks(
    overrides: dict[str, Any], nested: bool
) -> None:
    """Durable attempts require exact bound identity through the full scope stack."""
    budget = _budget()

    def scope(**values: Any) -> RequestExecutionScope:
        fields: dict[str, Any] = {
            "source": SOURCE,
            "canonical_model": MODEL,
            "operation": RequestOperation.QUERY,
            "budget": budget,
            "source_attempt_binding": _binding(),
        }
        fields.update(values)
        return RequestExecutionScope(**fields)

    scopes = (scope(), scope(**overrides)) if nested else (scope(**overrides),)
    with pytest.raises(RequestAuthorizationError):
        FuyaoHttpClient._validated_source_attempt_scope(scopes)


@pytest.mark.parametrize(
    "url",
    [
        "http://fuyao.test/v1/query",
        "https://user@fuyao.test/v1/query",
        "https://fuyao.test:444/v1/query",
        "https://fuyao.test:invalid/v1/query",
    ],
)
def test_scoped_host_validation_rejects_non_exact_https_authorities(url: str) -> None:
    with pytest.raises(RequestAuthorizationError):
        FuyaoHttpClient._scoped_host(url)

    assert FuyaoHttpClient._scoped_host("https://FUYAO.test:443/v1/query") == HOST


def test_redirect_is_not_followed_and_has_exactly_one_reservation(gate_engine: Any) -> None:
    """A gate-bound 3xx is rejected after one reserved request and never followed."""
    _seed_gate(gate_engine)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: (
            sent.append(request) or httpx.Response(302, headers={"Location": "https://other.test/"})
        ),
        gate=_gate(gate_engine),
        max_attempts=3,
    )
    with pytest.raises(RequestRedirectError):
        _run_scoped_get(client, binding=_binding())
    assert len(sent) == 1
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"


def test_liveness_change_after_reservation_prevents_mock_transport_send(
    gate_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation after a committed permit stops the physical request."""
    from threading import Event

    from opendata.data.request_budget import RequestExecutionCancelledError

    _seed_gate(gate_engine)
    gate = _gate(gate_engine)
    original_reserve = gate.reserve_attempt
    cancelled = Event()

    def reserve_then_cancel(attempt: Any) -> Any:
        reservation = original_reserve(attempt)
        cancelled.set()
        return reservation

    monkeypatch.setattr(gate, "reserve_attempt", reserve_then_cancel)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=gate,
    )
    budget = _budget()
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=_binding(),
            cancellation=cancelled,
        ),
        pytest.raises(RequestExecutionCancelledError),
    ):
        client.get("/v1/financial-statement")
    assert sent == []
    rows = _attempt_rows(gate_engine)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"


def test_scope_cancellation_while_waiting_for_shared_host_gate_prevents_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from opendata.data.request_budget import RequestExecutionCancelledError

    cancelled = Event()
    sent: list[httpx.Request] = []

    class ContendedHostGate:
        def acquire(self, *, timeout: float) -> bool:
            assert timeout == 0.05
            cancelled.set()
            return False

        def release(self) -> None:
            raise AssertionError("a contended semaphore was never acquired")

    monkeypatch.setattr(
        ths_http_client_module,
        "get_shared_concurrency_gate",
        lambda _host: ContendedHostGate(),
    )
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS)
    )
    budget = _budget()

    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            cancellation=cancelled,
        ),
        pytest.raises(RequestExecutionCancelledError),
    ):
        client.get("/api/a-share/financials/income-statements")

    assert sent == []
    # The send budget is consumed only after the shared host gate is acquired.
    assert budget.attempts_used == 0


def test_gate_rejects_httpx_transport_with_hidden_retry_before_reservation(
    gate_engine: Any,
) -> None:
    """The real HTTP transport must retain retries=0 before the gate can reserve."""
    _seed_gate(gate_engine)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        rate_limiter=_independent_limiter(),
        source_attempt_gate=_gate(gate_engine),
    )
    pool = client._client._transport._pool
    pool._retries = 1
    try:
        with pytest.raises(RequestAuthorizationError, match="TLS transport"):
            _run_scoped_get(client, binding=_binding())
        assert _attempt_rows(gate_engine) == []
    finally:
        client.close()


@pytest.mark.parametrize("mutation", ["redirect", "trust_env", "mount", "auth", "event_hook"])
def test_gate_rejects_client_configuration_that_can_change_routing_or_auth(
    gate_engine: Any, mutation: str
) -> None:
    """Client redirects, mounts, auth and callbacks fail before a durable permit."""
    _seed_gate(gate_engine)
    sent: list[httpx.Request] = []
    client = _client(
        lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS),
        gate=_gate(gate_engine),
    )
    if mutation == "redirect":
        client._client.follow_redirects = True
    elif mutation == "trust_env":
        client._client._trust_env = True
    elif mutation == "mount":
        client._client._mounts["all://"] = object()
    elif mutation == "auth":
        client._client.auth = httpx.BasicAuth("offline-user", "offline-password")
    else:
        client._client.event_hooks["response"].append(lambda _response: None)

    with pytest.raises(RequestAuthorizationError):
        _run_scoped_get(client, binding=_binding())
    assert sent == []
    assert _attempt_rows(gate_engine) == []


@pytest.mark.parametrize("mutation", ["proxy", "hostname-verification", "certificate-verification"])
def test_gate_rejects_unsafe_http_transport_security_state(gate_engine: Any, mutation: str) -> None:
    """A gate permit cannot be issued to a proxied or weakened TLS transport."""
    _seed_gate(gate_engine)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        rate_limiter=_independent_limiter(),
        source_attempt_gate=_gate(gate_engine),
    )
    pool = client._client._transport._pool
    if mutation == "proxy":
        pool._proxy = object()
    elif mutation == "hostname-verification":
        pool._ssl_context.check_hostname = False
    else:
        pool._ssl_context.check_hostname = False
        pool._ssl_context.verify_mode = ssl.CERT_NONE

    try:
        with pytest.raises(RequestAuthorizationError, match="TLS transport"):
            _run_scoped_get(client, binding=_binding())
        assert _attempt_rows(gate_engine) == []
    finally:
        client.close()


def test_gate_rejects_injected_httpx_client_before_gate_or_send(gate_engine: Any) -> None:
    """An opaque injected client cannot bypass the verified transport constructor."""
    _seed_gate(gate_engine)
    sent: list[httpx.Request] = []
    injected = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or _streamed_response(200, content=_SUCCESS)
        )
    )
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("offline-key", base_url=f"https://{HOST}"),
        client=injected,
        source_attempt_gate=_gate(gate_engine),
    )
    try:
        with pytest.raises(RequestAuthorizationError, match="transport is not verified"):
            _run_scoped_get(client, binding=_binding())
        assert sent == []
        assert _attempt_rows(gate_engine) == []
    finally:
        injected.close()
