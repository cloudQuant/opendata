"""Offline integration tests for persistent HTTP attempt reservations.

All HTTP adapter sends are monkeypatched in memory. The source-attempt gate
uses only a fresh SQLite database under ``tmp_path`` with migration 0015 and
synthetic policy/grant declarations; no production source or network is used.
"""

from __future__ import annotations

import contextvars
import hashlib
import importlib.util
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

import pytest
import requests
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.pool import NullPool
from urllib3.util.retry import Retry

from opendata.data.http_client import (
    FailureCategory,
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.raw_response_cache import RawResponseCache
from opendata.data.request_budget import (
    GrantDecision,
    RequestAdapterRetryError,
    RequestAuthorizationError,
    RequestBudget,
    RequestBudgetProfile,
    RequestBudgetScopeError,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
    capture_request_scopes,
    request_execution_scope,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    GrantRevocationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from types import ModuleType

    from pytest import MonkeyPatch
    from sqlalchemy.engine import Engine


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
MIGRATION_NAME = "http_source_attempt_gate_migration_0015_test"
SOURCE = "gate-fixture"
MODEL = "fixture-model"
PRINCIPAL = "fixture-worker"
GRANT_ID = "fixture-grant"
QUOTA_KEY = "fixture-quota-v1"
HOST = "api.example.test"
URL = f"https://{HOST}/data"
NOW_US = 1_700_000_000_000_000
FAR_FUTURE_US = 253_402_300_799_999_999
MICROTOKEN = 1_000_000


def _load_migration() -> ModuleType:
    """Load only the standalone SQLite migration, without importing app config."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path) -> Engine:
    """Create a new file-backed SQLite database owned by the current test."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / 'http-source-attempt-gate.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )


def _apply_migration(engine: Engine) -> None:
    """Apply revision 0015 only, using the supplied temporary connection."""
    migration = _load_migration()
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()


@pytest.fixture
def gate_engine(tmp_path: Path) -> Iterable[Engine]:
    """Yield a migrated temporary database and always close its connections."""
    engine = _new_engine(tmp_path)
    try:
        _apply_migration(engine)
        yield engine
    finally:
        engine.dispose()


def _seed_gate(
    engine: Engine,
    *,
    decision: str = "ALLOWED",
    grant_id: str = GRANT_ID,
    source: str = SOURCE,
    model: str = MODEL,
    principal: str = PRINCIPAL,
    hosts: tuple[str, ...] = (HOST,),
    tokens_micro: int = 5 * MICROTOKEN,
    capacity_microtokens: int = 5 * MICROTOKEN,
) -> None:
    """Insert an explicit synthetic grant and quota; evidence is not legal proof."""
    digest = hashlib.sha256(b"synthetic fixture only; not legal source evidence").hexdigest()
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
                "capacity": capacity_microtokens,
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
                "source": source,
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
            {"quota": QUOTA_KEY, "tokens": tokens_micro, "refill": NOW_US},
        )
        if decision == "ALLOWED":
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grants "
                    "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                    "revision, not_before_us, expires_at_us, conditions_json, "
                    "rights_evidence_reference, rights_evidence_sha256, "
                    "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                    "created_at_us, updated_at_us) VALUES (:grant, :source, :model, 'query', "
                    ":principal, 'ALLOWED', 1, 1, :expires, '[]', :evidence, :digest, "
                    ":note, 'offline-test', 1, NULL, 1, 1)"
                ),
                {
                    "grant": grant_id,
                    "source": source,
                    "model": model,
                    "principal": principal,
                    "expires": FAR_FUTURE_US,
                    "evidence": "synthetic:offline-fixture-only",
                    "digest": digest,
                    "note": "Synthetic test declaration; not legal authorization.",
                },
            )
            for host in hosts:
                connection.execute(
                    sa.text(
                        "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"
                    ),
                    {"grant": grant_id, "host": host},
                )
            if hosts:
                connection.execute(
                    sa.text(
                        "INSERT INTO od_source_grant_host_sets "
                        "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                        "VALUES (:grant, 1, :count, 1, 'offline-test', 'synthetic-test-only')"
                    ),
                    {"grant": grant_id, "count": len(hosts)},
                )
        elif decision == "UNKNOWN":
            connection.execute(
                sa.text(
                    "INSERT INTO od_source_grants "
                    "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                    "revision, not_before_us, expires_at_us, conditions_json, created_at_us, "
                    "updated_at_us) VALUES (:grant, :source, :model, 'query', :principal, "
                    "'UNKNOWN', 1, 1, :expires, '[]', 1, 1)"
                ),
                {
                    "grant": grant_id,
                    "source": source,
                    "model": model,
                    "principal": principal,
                    "expires": FAR_FUTURE_US,
                },
            )
        else:
            raise ValueError("test fixture decision must be ALLOWED or UNKNOWN")


def _new_gate(engine: Engine, monkeypatch: MonkeyPatch) -> SourceAttemptGate:
    """Create a gate attached to the fixture database with a stable local clock."""
    gate = SourceAttemptGate(engine)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    return gate


def _local_budget(
    *,
    source: str = SOURCE,
    model: str = MODEL,
    decision: GrantDecision = GrantDecision.ALLOWED,
    task_attempts: int = 8,
    source_attempts: int = 8,
) -> RequestBudget:
    """Build the separate in-memory trusted-code budget for the synthetic source."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source=source,
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=decision,
        rights_evidence="synthetic-test-reference-only",
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        allowed_hosts=(HOST,),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        profile=profile,
    )
    return RequestBudget(
        task_attempts=task_attempts,
        source_attempts=source_attempts,
        grants=(grant,),
        profile=profile,
    )


def _binding(
    *, grant_id: str = GRANT_ID, principal: str = PRINCIPAL, run_key: str = "fixture-run"
) -> SourceAttemptBinding:
    """Return a trusted in-process binding that is independent of request data."""
    return SourceAttemptBinding(
        grant_id=grant_id,
        expected_revision=1,
        principal_scope=principal,
        run_key=run_key,
    )


def _make_client(
    *,
    gate: SourceAttemptGate | None = None,
    cache: RawResponseCache | None = None,
    max_attempts: int = 2,
    time_fn: Callable[[], float] | None = None,
    session: requests.Session | None = None,
) -> GovernedHttpClient:
    """Create an ordinary client with no socket path or real backoff delay."""
    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=max_attempts,
            rate_limit_per_host=100.0,
            rate_burst=10,
            max_concurrency_per_host=2,
        ),
        session=session,
        source_attempt_gate=gate,
        raw_response_cache=cache,
        sleep=lambda _seconds: None,
        time_fn=time_fn if time_fn is not None else lambda: 100.0,
        jitter_fn=lambda: 0.0,
    )
    # Keep requests' environment proxy and netrc discovery outside this test.
    client._get_session().trust_env = False
    return client


def _install_fake_adapter(
    monkeypatch: MonkeyPatch,
    script: Iterable[tuple[int, Mapping[str, str], bytes] | int],
    *,
    before_send: Callable[[requests.PreparedRequest], None] | None = None,
    capture_kwargs: list[dict[str, Any]] | None = None,
) -> list[requests.PreparedRequest]:
    """Patch only exact HTTPAdapter.send; requests.Session keeps its real pipeline."""
    replies = deque(script)
    sent: list[requests.PreparedRequest] = []

    def fake_send(
        _adapter: requests.adapters.HTTPAdapter,
        request: requests.PreparedRequest,
        **_kwargs: Any,
    ) -> requests.Response:
        sent.append(request)
        if capture_kwargs is not None:
            capture_kwargs.append(dict(_kwargs))
        if before_send is not None:
            before_send(request)
        if not replies:
            raise AssertionError("unexpected HTTP adapter send")
        item = replies.popleft()
        if isinstance(item, int):
            status_code, headers, body = item, {}, b"{}"
        else:
            status_code, headers, body = item
        response = requests.Response()
        response.status_code = status_code
        response.headers.update(headers)
        response._content = body
        response._content_consumed = True
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
    return sent


def _attempt_rows(engine: Engine) -> list[dict[str, object]]:
    """Read only the test-owned attempt ledger for assertions."""
    with engine.connect() as connection:
        result = connection.execute(
            sa.text(
                "SELECT * FROM od_source_attempt_reservations "
                "ORDER BY request_id, attempt_no, attempt_id"
            )
        )
        return [dict(row) for row in result.mappings().all()]


def _remaining_tokens(engine: Engine) -> int:
    """Return the configured fixture quota balance after a request sequence."""
    with engine.connect() as connection:
        value = connection.execute(
            sa.text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
            {"quota": QUOTA_KEY},
        ).scalar_one()
    return int(value)


def _seed_response_cache(
    cache: RawResponseCache,
    client: GovernedHttpClient,
    content: bytes,
) -> requests.PreparedRequest:
    """Insert one stale synthetic response under the client's prepared identity."""
    session = client._get_session()
    prepared = session.prepare_request(
        requests.Request("GET", URL, headers=client._config.default_headers)
    )
    assert cache.put(
        method="GET",
        source=SOURCE,
        endpoint=prepared.url,
        headers=prepared.headers,
        status_code=200,
        content=content,
        encoding="utf-8",
    )
    return prepared


def _cached_body(cache: RawResponseCache, prepared: requests.PreparedRequest) -> bytes:
    """Read the exact prepared identity from the synthetic test cache."""
    cached = cache.get(
        source=SOURCE,
        endpoint=prepared.url,
        headers=prepared.headers,
    )
    assert cached is not None
    return cached.content


def _request_in_scope(
    client: GovernedHttpClient,
    budget: RequestBudget,
    *,
    binding: SourceAttemptBinding | None = None,
    cancellation: Event | None = None,
    deadline: float | None = None,
    source: str = SOURCE,
    use_cache: bool = False,
) -> requests.Response:
    """Run one synthetic GET inside its trusted source and model scope."""
    with request_execution_scope(
        source=source,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=binding,
        cancellation=cancellation,
        deadline=deadline,
    ):
        return client.get(URL, source=source, use_cache=use_cache)


def test_gate_reserves_each_physical_retry_before_adapter_send(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """Each retry gets a fresh durable permit and both ledgers debit once per send."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()

    def assert_committed_before_send(_request: requests.PreparedRequest) -> None:
        rows = _attempt_rows(gate_engine)
        assert len(rows) == len(sent)
        assert rows[-1]["outcome"] == "RESERVED"

    send_kwargs: list[dict[str, Any]] = []
    sent = _install_fake_adapter(
        monkeypatch,
        [503, 200],
        before_send=assert_committed_before_send,
        capture_kwargs=send_kwargs,
    )
    client = _make_client(gate=gate)
    response = _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert response.status_code == 200
    assert len(sent) == len(rows) == 2
    assert [row["attempt_no"] for row in rows] == [1, 2]
    assert len({row["attempt_id"] for row in rows}) == 2
    assert len({row["request_id"] for row in rows}) == 1
    assert {row["outcome"] for row in rows} == {"RESERVED"}
    assert {row["source"] for row in rows} == {SOURCE}
    assert {row["canonical_model"] for row in rows} == {MODEL}
    assert {row["principal_scope"] for row in rows} == {PRINCIPAL}
    assert {row["run_key"] for row in rows} == {"fixture-run"}
    assert len(send_kwargs) == 2
    assert all(
        kwargs.get("verify") is True and kwargs.get("cert") is None and kwargs.get("proxies") == {}
        for kwargs in send_kwargs
    )
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (2, 2)
    assert _remaining_tokens(gate_engine) == 3 * MICROTOKEN


def test_gate_enabled_request_bypasses_cache_and_reserves_before_send(
    gate_engine: Engine, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A stale entry cannot bypass a durable reservation or be replaced by gated traffic."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    cache = RawResponseCache(tmp_path / "response-cache", ttl_seconds=60, clock=lambda: 100.0)
    client = _make_client(gate=gate, cache=cache)
    stale = b'{"source":"stale-cache"}'
    prepared = _seed_response_cache(cache, client, stale)
    sent = _install_fake_adapter(monkeypatch, [(200, {}, b'{"source":"fresh-transport"}')])

    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=_binding(),
    ):
        response = client.get(URL, source=SOURCE, use_cache=True)

    assert response.status_code == 200
    assert json.loads(response.content) == {"source": "fresh-transport"}
    rows = _attempt_rows(gate_engine)
    assert len(sent) == len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (1, 1)
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN
    assert _cached_body(cache, prepared) == stale


def test_revoked_gate_grant_cannot_return_stale_cache_or_send(
    gate_engine: Engine, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A revoked durable grant blocks a pre-existing cache entry without an HTTP hop."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    assert (
        gate.revoke_grant(
            GRANT_ID,
            expected_revision=1,
            actor="offline-test",
            reason_code="synthetic-test-revoke",
        ).status
        is GrantRevocationStatus.REVOKED
    )
    budget = _local_budget()
    cache = RawResponseCache(
        tmp_path / "revoked-response-cache", ttl_seconds=60, clock=lambda: 100.0
    )
    client = _make_client(gate=gate, cache=cache)
    stale = b'{"source":"stale-before-revoke"}'
    prepared = _seed_response_cache(cache, client, stale)
    sent = _install_fake_adapter(monkeypatch, [])

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(
            client,
            budget,
            binding=_binding(),
            use_cache=True,
        )

    rows = _attempt_rows(gate_engine)
    assert sent == []
    assert len(rows) == 1
    assert (rows[0]["outcome"], rows[0]["denial_code"]) == (
        "DENIED",
        "grant_not_allowed",
    )
    assert _cached_body(cache, prepared) == stale
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_gate_none_legacy_cache_hit_consumes_no_local_attempt(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The legacy gate-free cache path remains available without spending a send budget."""
    budget = _local_budget()
    cache = RawResponseCache(
        tmp_path / "legacy-response-cache", ttl_seconds=60, clock=lambda: 100.0
    )
    client = _make_client(cache=cache, gate=None)
    stale = b'{"source":"synthetic-legacy-cache"}'
    _seed_response_cache(cache, client, stale)
    sent = _install_fake_adapter(monkeypatch, [])

    response = _request_in_scope(client, budget, binding=None, use_cache=True)

    assert response.status_code == 200
    assert json.loads(response.content) == {"source": "synthetic-legacy-cache"}
    assert sent == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (0, 0)


def test_unknown_local_grant_fails_before_gate_or_transport(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A local UNKNOWN declaration cannot be upgraded by the persistent gate."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget(decision=GrantDecision.UNKNOWN)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (0, 0)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_persisted_unknown_grant_fails_without_adapter_send(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """The database gate rejects its own UNKNOWN grant even with a local ALLOWED scope."""
    _seed_gate(gate_engine, decision="UNKNOWN")
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert sent == []
    assert len(rows) == 1
    assert (rows[0]["outcome"], rows[0]["denial_code"]) == ("DENIED", "grant_not_allowed")
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (1, 1)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_persisted_grant_scope_mismatch_fails_without_adapter_send(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A valid local grant cannot change the source scope stored in the gate."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget(source="other-fixture-source")
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(
            client,
            budget,
            binding=_binding(),
            source="other-fixture-source",
        )

    rows = _attempt_rows(gate_engine)
    assert sent == []
    assert len(rows) == 1
    assert (rows[0]["outcome"], rows[0]["denial_code"]) == (
        "DENIED",
        "grant_scope_mismatch",
    )
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_bound_scope_fails_closed_when_durable_gate_is_unconfigured(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A bound request cannot send when its file-backed gate has no grant or policy."""
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert sent == []
    assert len(rows) == 1
    assert (rows[0]["outcome"], rows[0]["denial_code"]) == ("DENIED", "grant_not_found")
    with gate_engine.connect() as connection:
        bucket_rows = (
            connection.execute(sa.text("SELECT * FROM od_source_gate_state")).mappings().all()
        )
    assert bucket_rows == []


def test_configured_gate_requires_a_trusted_scope_binding(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """An injected persistent gate cannot be used without trusted caller binding."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=None)

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_bound_scope_fails_closed_when_client_gate_is_not_configured(
    monkeypatch: MonkeyPatch,
) -> None:
    """A trusted durable binding cannot silently downgrade to the legacy client path."""
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=None)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    assert sent == []


def test_gate_client_without_scope_fails_before_session_or_reservation(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """An injected durable gate requires an explicit trusted execution scope."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = GovernedHttpClient(source_attempt_gate=gate)

    with pytest.raises(RequestAuthorizationError, match="consistent trusted request scopes"):
        client.get(URL, source=SOURCE, use_cache=False)

    assert sent == []
    assert not hasattr(client._thread_local, "session")
    assert _attempt_rows(gate_engine) == []
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_client_rejects_a_gate_that_is_not_the_exact_gate_type() -> None:
    """A duck-typed object cannot impersonate the durable reservation authority."""
    invalid_gate: Any = object()

    with pytest.raises(TypeError, match="source_attempt_gate must be"):
        GovernedHttpClient(source_attempt_gate=invalid_gate)


@pytest.mark.parametrize(
    ("transport_case", "expected_error"),
    (
        ("session_subclass", RequestAuthorizationError),
        ("session_method_override", RequestAuthorizationError),
        ("adapter_subclass", RequestAuthorizationError),
        ("adapter_send_override", RequestAuthorizationError),
        ("retry_total", RequestAdapterRetryError),
        ("retry_sub_limit", RequestAdapterRetryError),
        ("retry_subclass", RequestAdapterRetryError),
        ("response_hook", RequestAuthorizationError),
        ("opaque_auth", RequestAuthorizationError),
        ("verify_disabled", RequestAuthorizationError),
        ("verify_not_exact_true", RequestAuthorizationError),
        ("client_certificate", RequestAuthorizationError),
        ("session_proxy", RequestAuthorizationError),
        ("config_proxy", RequestAuthorizationError),
    ),
)
def test_attempt_gate_rejects_transport_customizations_before_budget_or_send(
    transport_case: str,
    expected_error: type[Exception],
    gate_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    """Customized requests pipelines cannot hide physical sends from the gate."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()

    if transport_case == "session_subclass":

        class CustomSession(requests.Session):
            pass

        session = CustomSession()
    else:
        session = requests.Session()

    # Keep this suite focused on each transport mutation; trust_env=True is
    # independently covered by the dedicated default/explicit rejection test.
    session.trust_env = False

    if transport_case == "verify_disabled":
        session.verify = False
    elif transport_case == "verify_not_exact_true":
        session.verify = 1
    elif transport_case == "client_certificate":
        session.cert = "synthetic-client-certificate.pem"
    elif transport_case == "session_proxy":
        session.proxies = {"https": "http://proxy.example.test:8080"}

    if transport_case == "session_method_override":
        session.request = lambda *_args, **_kwargs: pytest.fail(  # type: ignore[method-assign]
            "custom session method must be rejected before use"
        )
    elif transport_case == "adapter_subclass":

        class CustomAdapter(requests.adapters.HTTPAdapter):
            pass

        session.mount("https://", CustomAdapter())
    elif transport_case == "adapter_send_override":
        adapter = session.get_adapter(URL)
        adapter.send = adapter.send  # type: ignore[method-assign]
    elif transport_case in {"retry_total", "retry_sub_limit", "retry_subclass"}:
        adapter = session.get_adapter(URL)
        if transport_case == "retry_total":
            adapter.max_retries = Retry(total=1)
        elif transport_case == "retry_sub_limit":
            adapter.max_retries = Retry(total=0, connect=1)
        else:

            class CustomRetry(Retry):
                pass

            adapter.max_retries = CustomRetry(total=0)
    elif transport_case == "response_hook":
        session.hooks["response"].append(lambda response, **_kwargs: response)
    elif transport_case == "opaque_auth":
        session.auth = lambda request: request

    sent = _install_fake_adapter(monkeypatch, [200])
    config = HttpClientConfig(
        max_attempts=2,
        rate_limit_per_host=100.0,
        rate_burst=10,
        max_concurrency_per_host=2,
        proxies=(
            {"https": "http://proxy.example.test:8080"}
            if transport_case == "config_proxy"
            else None
        ),
    )
    client = GovernedHttpClient(
        config,
        session=session,
        source_attempt_gate=gate,
        sleep=lambda _seconds: None,
        time_fn=lambda: 100.0,
        jitter_fn=lambda: 0.0,
    )

    with pytest.raises(expected_error):
        _request_in_scope(client, budget, binding=_binding())

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (0, 0)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


@pytest.mark.parametrize("session_mode", ("default", "explicit"))
def test_attempt_gate_rejects_environment_trusting_session_before_any_attempt(
    session_mode: str,
    gate_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    """The default and explicitly supplied environment-trusting sessions fail closed."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    session = requests.Session() if session_mode == "explicit" else None
    if session is not None:
        session.trust_env = True

    client = GovernedHttpClient(
        HttpClientConfig(
            max_attempts=2,
            rate_limit_per_host=100.0,
            rate_burst=10,
            max_concurrency_per_host=2,
        ),
        session=session,
        source_attempt_gate=gate,
        sleep=lambda _seconds: None,
        time_fn=lambda: 100.0,
        jitter_fn=lambda: 0.0,
    )
    assert client._get_session().trust_env is True
    sent = _install_fake_adapter(monkeypatch, [200])

    with pytest.raises(RequestAuthorizationError, match="environment trust to be disabled"):
        _request_in_scope(client, budget, binding=_binding())

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (0, 0)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_gate_reservation_exception_is_sanitized_and_never_reaches_transport(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """An unavailable reservation authority fails closed without exposing its error."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()

    def unavailable(_attempt: TransportAttempt) -> AttemptReservation:
        raise SourceAttemptGateUnavailable("synthetic secret detail must stay private")

    monkeypatch.setattr(gate, "reserve_attempt", unavailable)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError) as raised:
        _request_in_scope(client, budget, binding=_binding())

    assert str(raised.value) == "source attempt gate did not authorize transport"
    assert raised.value.__cause__ is None
    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (1, 1)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_gate_rejects_non_reservation_result_without_transport(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A lookalike or malformed gate response is never interpreted as a permit."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    monkeypatch.setattr(gate, "reserve_attempt", lambda _attempt: object())
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (1, 1)
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN


def test_gate_rejects_duplicate_attempt_identity_without_second_send(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A duplicate generated attempt id is never treated as a reusable permit."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)
    monkeypatch.setattr("opendata.data.http_client.uuid.uuid4", lambda: UUID(int=1))

    assert _request_in_scope(client, budget, binding=_binding()).status_code == 200
    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert len(sent) == len(rows) == 1
    assert rows[0]["attempt_id"] == UUID(int=1).hex
    assert rows[0]["outcome"] == "RESERVED"
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN


def test_committed_reservation_with_lost_ack_never_reaches_adapter(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A commit that landed but raised before its acknowledgement cannot send or retry."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    commit_calls = 0

    def commit_then_lose_ack(connection: Any) -> None:
        nonlocal commit_calls
        commit_calls += 1
        connection.commit()
        raise SourceAttemptGateUnavailable("synthetic acknowledgement loss after commit")

    monkeypatch.setattr(gate, "_commit", commit_then_lose_ack)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with pytest.raises(RequestAuthorizationError):
        _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert commit_calls == 1
    assert sent == []
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN


@pytest.mark.parametrize("trigger", ("cancel", "deadline"))
def test_liveness_change_after_reservation_prevents_adapter_send(
    trigger: str,
    gate_engine: Engine,
    monkeypatch: MonkeyPatch,
) -> None:
    """Cancellation and deadline are checked again after the committed permit."""
    import opendata.data.request_budget as request_budget_module

    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    original_reserve = gate.reserve_attempt
    cancellation = Event()
    real_monotonic = request_budget_module.time.monotonic
    fake_now = {"value": real_monotonic()}
    monkeypatch.setattr(request_budget_module.time, "monotonic", lambda: fake_now["value"])

    def reserve_then_expire(attempt: object) -> object:
        result = original_reserve(attempt)
        if trigger == "cancel":
            cancellation.set()
        else:
            fake_now["value"] = real_monotonic() + 2.0
        return result

    monkeypatch.setattr(gate, "reserve_attempt", reserve_then_expire)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)
    deadline = fake_now["value"] + 1.0 if trigger == "deadline" else None
    expected_error = (
        RequestExecutionCancelledError if trigger == "cancel" else RequestExecutionDeadlineError
    )

    with pytest.raises(expected_error):
        _request_in_scope(
            client,
            budget,
            binding=_binding(),
            cancellation=cancellation if trigger == "cancel" else None,
            deadline=deadline,
        )

    rows = _attempt_rows(gate_engine)
    assert sent == []
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN


def test_bounded_deadline_after_reservation_prevents_adapter_send(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """An overall timeout expiring during commit burns its permit but sends no bytes."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    original_reserve = gate.reserve_attempt
    fake_clock = {"now": 100.0}

    def reserve_then_expire(attempt: object) -> object:
        result = original_reserve(attempt)
        fake_clock["now"] = 102.0
        return result

    monkeypatch.setattr(gate, "reserve_attempt", reserve_then_expire)
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate, time_fn=lambda: fake_clock["now"])

    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            source_attempt_binding=_binding(),
        ),
        pytest.raises(HttpFetchError) as raised,
    ):
        client.get_bounded(
            URL,
            max_body_bytes=1024,
            overall_timeout=1.0,
            source=SOURCE,
        )

    rows = _attempt_rows(gate_engine)
    assert raised.value.category is FailureCategory.TIMEOUT
    assert sent == []
    assert len(rows) == 1
    assert rows[0]["outcome"] == "RESERVED"
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN


def test_scoped_redirect_never_creates_a_second_gate_reservation_or_hop(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A 302 is rejected after one reserved send and cannot follow Location."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(
        monkeypatch,
        [(302, {"Location": "https://redirect.example.test/second-hop"}, b"")],
    )
    client = _make_client(gate=gate)

    with pytest.raises(RequestRedirectError):
        _request_in_scope(client, budget, binding=_binding())

    rows = _attempt_rows(gate_engine)
    assert len(sent) == len(rows) == 1
    assert urlsplit(sent[0].url or "").hostname == HOST
    assert (rows[0]["attempt_no"], rows[0]["outcome"]) == (1, "RESERVED")
    assert _remaining_tokens(gate_engine) == 4 * MICROTOKEN


def test_original_client_path_without_gate_remains_compatible(
    monkeypatch: MonkeyPatch,
) -> None:
    """A legacy local request scope still works without persistent gate binding."""
    sent = _install_fake_adapter(monkeypatch, [200])
    budget = _local_budget()
    client = _make_client(gate=None)

    response = _request_in_scope(client, budget, binding=None)

    assert response.status_code == 200
    assert len(sent) == 1
    assert (budget.attempts_used, budget.attempts_used_for(SOURCE)) == (1, 1)


def test_inherited_context_cannot_replace_trusted_source_binding(
    gate_engine: Engine, monkeypatch: MonkeyPatch
) -> None:
    """A worker context cannot replace its captured trusted binding before transport."""
    _seed_gate(gate_engine)
    gate = _new_gate(gate_engine, monkeypatch)
    budget = _local_budget()
    sent = _install_fake_adapter(monkeypatch, [200])
    client = _make_client(gate=gate)

    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
        source_attempt_binding=_binding(),
    ):
        inherited = capture_request_scopes()

    def replace_binding_in_worker_context() -> None:
        with request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            inherited_scopes=inherited,
            source_attempt_binding=_binding(grant_id="replacement-grant"),
        ):
            client.get(URL, source=SOURCE, use_cache=False)

    with pytest.raises(RequestBudgetScopeError):
        contextvars.Context().run(replace_binding_in_worker_context)

    assert sent == []
    assert _attempt_rows(gate_engine) == []
    assert _remaining_tokens(gate_engine) == 5 * MICROTOKEN
