"""Offline THS transport checks for the explicitly locked V4 one-shot path."""

from __future__ import annotations

import hashlib
import importlib.util
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from opendata.data.providers.ths.transport import http_client as http_client_module
from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.errors import FuyaoError
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V4,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionScope,
    RequestOperation,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    request_execution_scope,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptScopeV3,
    TransportAttempt,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisionRequest,
)
from scripts import migrate_source_gate

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from sqlalchemy.engine import Engine

_QUERY = {
    "end": "1761695999999",
    "interval": "1d",
    "start": "1761609600000",
    "thscode": "000905.SH",
}
_ALLOWED_FIELDS = ("end", "interval", "start", "thscode")
_RIGHTS_REFERENCE = "evidence:ths-csi500-sample"
_REQUEST_ID = "request-ths-csi500-one-shot"
_ATTEMPT_ID = "attempt-ths-csi500-one-shot"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BASE_GATE_MIGRATION = (
    _REPOSITORY_ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
)


def _summary(parameters: dict[str, str] = _QUERY) -> str:
    """Return the exact canonical digest for the synthetic query."""
    return request_summary_sha256(parameters, allowed_fields=_ALLOWED_FIELDS)


def _pin(
    *, host: str = "fuyao.test", path: str = "/api/a-share-index/prices/historical"
) -> SourceAttemptScopeV3Pin:
    """Build a synthetic transport pin whose values mirror the locked scope."""
    return SourceAttemptScopeV3Pin(
        capture_intent_id="intent-ths-csi500-sample",
        request_summary_sha256=_summary(),
        request_summary_allowed_fields=_ALLOWED_FIELDS,
        expected_https_host=host,
        expected_path=path,
        expected_method="GET",
        source_policy_id="policy-ths-csi500-sample",
        source_policy_revision="revision-1",
        source_policy_sha256="a" * 64,
        budget_binding_sha256="b" * 64,
        budget_quota_key="quota-ths-csi500-sample",
        budget_policy_revision=1,
        budget_policy_sha256="c" * 64,
    )


def _binding() -> SourceAttemptBinding:
    """Build a synthetic binding matching the pre-locked attempt."""
    return SourceAttemptBinding(
        grant_id="grant-ths-csi500-sample",
        expected_revision=1,
        principal_scope="principal-ths-csi500-sample",
        run_key="run-ths-csi500-sample",
        expected_rights_evidence_reference=_RIGHTS_REFERENCE,
    )


def _locked_scope(
    pin: SourceAttemptScopeV3Pin | None = None,
    *,
    attempt_no: int = 1,
) -> SourceAttemptScopeV3:
    """Build the immutable gate identity whose IDs transport must reuse."""
    exact_pin = pin or _pin()
    binding = _binding()
    attempt = TransportAttempt(
        attempt_id=_ATTEMPT_ID,
        request_id=_REQUEST_ID,
        attempt_no=attempt_no,
        grant_id=binding.grant_id,
        expected_revision=binding.expected_revision,
        source="ths",
        canonical_model="index_daily",
        operation="query",
        principal_scope=binding.principal_scope,
        host=exact_pin.expected_https_host,
        run_key=binding.run_key,
        expected_rights_evidence_reference=binding.expected_rights_evidence_reference,
    )
    return SourceAttemptScopeV3(
        attempt=attempt,
        request_scheme="https",
        request_method=exact_pin.expected_method,
        request_host=exact_pin.expected_https_host,
        request_path=exact_pin.expected_path,
        capture_intent_id=exact_pin.capture_intent_id,
        request_summary_sha256=exact_pin.request_summary_sha256,
        source_policy_id=exact_pin.source_policy_id,
        source_policy_revision=exact_pin.source_policy_revision,
        source_policy_sha256=exact_pin.source_policy_sha256,
        budget_binding_sha256=exact_pin.budget_binding_sha256,
        budget_quota_key=exact_pin.budget_quota_key,
        budget_policy_revision=exact_pin.budget_policy_revision,
        budget_policy_sha256=exact_pin.budget_policy_sha256,
    )


def _install_v4_schema(engine: Engine) -> None:
    """Install a disposable V4 source gate using repository migrations."""
    spec = importlib.util.spec_from_file_location(
        "ths_one_shot_transport_base_migration",
        _BASE_GATE_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    database_path = engine.url.database
    if not database_path:
        raise AssertionError("transport tests require a file-backed SQLite gate")
    assert migrate_source_gate.main(["--database-path", database_path]) == 0


def _configure_one_shot_gate(engine: Engine, gate: SourceAttemptGate) -> None:
    """Create one synthetic grant, quota, and immutable transport lock."""
    pin = _pin()
    binding = _binding()
    scope = _locked_scope(pin)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1000000, 5000000, "
                "'synthetic:transport-policy', :digest, 'offline-test', 1)"
            ),
            {"quota": pin.budget_quota_key, "digest": pin.budget_policy_sha256},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES ('ths', :quota, 'synthetic:transport-binding', "
                ":digest, 'offline-test', 1)"
            ),
            {"quota": pin.budget_quota_key, "digest": pin.budget_binding_sha256},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, 5000000, 0, 1, 1)"
            ),
            {"quota": pin.budget_quota_key},
        )

    now = datetime.now(timezone.utc)
    SourceGrantProvisioner(gate).provision(
        SourceGrantProvisionRequest(
            grant_id=binding.grant_id,
            revision=binding.expected_revision,
            source="ths",
            canonical_model="index_daily",
            operation="query",
            principal_scope=binding.principal_scope,
            host=pin.expected_https_host,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            rights_evidence_reference=binding.expected_rights_evidence_reference,
            rights_evidence_sha256=hashlib.sha256(b"synthetic evidence metadata").hexdigest(),
            operator_confirms_authorized_use=True,
            operator_attestation_note=(
                "Synthetic offline transport fixture; supplier rights are not asserted."
            ),
            attested_by="offline-test-operator",
        )
    )
    gate.lock_one_shot_scope(scope)


@pytest.fixture
def configured_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Yield a disposable file-backed V4 gate with one synthetic locked request."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'ths-one-shot-transport.sqlite'}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )
    try:
        _install_v4_schema(engine)
        gate = SourceAttemptGate(engine)
        _configure_one_shot_gate(engine, gate)
        yield engine, gate
    finally:
        engine.dispose()


def _gate_counts(engine: Engine) -> tuple[int, int, int, int]:
    """Return lock, permit, reservation, and V3 scope counts for this temp gate."""
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_one_shot_configs")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_one_shot_permits")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")
            ).scalar_one(),
        )


def _one_shot_scope(
    pin: SourceAttemptScopeV3Pin | None = None,
    *,
    locked_scope: SourceAttemptScopeV3 | None = None,
):
    """Open a V4 request scope carrying transport pins and fixed gate IDs."""
    exact_pin = pin or _pin()
    return request_execution_scope(
        source="ths",
        canonical_model="index_daily",
        operation=RequestOperation.QUERY,
        budget=RequestBudget(),
        source_attempt_binding=_binding(),
        source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V4,
        source_attempt_scope_v3_pin=exact_pin,
        source_attempt_one_shot_scope_v4=locked_scope or _locked_scope(exact_pin),
    )


def _success_response() -> httpx.Response:
    """Return one deterministic Fuyao success envelope."""
    return httpx.Response(
        200,
        json={
            "code": 0,
            "message": "success",
            "request_id": "provider-request-synthetic",
            "data": {"timestamp": 1_767_369_600_000, "item": []},
        },
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_attempts: int = 1,
    source_attempt_gate: SourceAttemptGate | None = None,
) -> FuyaoHttpClient:
    """Build a verified offline client with local authorization seams."""
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url="https://fuyao.test"),
        mock_transport=httpx.MockTransport(handler),
        rate_limiter=FuyaoRateLimiter(rate_per_second=1_000, burst=1_000),
        max_attempts=max_attempts,
        sleep=lambda _seconds: None,
        source_attempt_gate=source_attempt_gate,
    )
    monkeypatch.setattr(http_client_module, "authorize_request", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "scoped_host_slot", lambda *_args: nullcontext())
    monkeypatch.setattr(client, "_scoped_shared_host_request", lambda *_args: nullcontext())
    monkeypatch.setattr(client._limiter, "wait_and_acquire", lambda *_args: None)
    return client


def test_v4_get_reuses_locked_ids_and_only_calls_one_shot_gate(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """One exact prepared request uses the locked IDs and one gate reservation."""
    sent: list[httpx.Request] = []
    logged_request_ids: list[str] = []
    engine, gate = configured_gate

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _success_response()

    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        # The per-request V4 preparation pins identity encoding even if the
        # mutable HTTPX client default was changed before the call.
        client._client.headers["Accept-Encoding"] = "gzip"
        monkeypatch.setattr(
            client,
            "_log_request_event",
            lambda **values: logged_request_ids.append(values["request_id"]),
        )
        with _one_shot_scope():
            result = client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert result.status_code == 200
    assert len(sent) == 1
    assert _gate_counts(engine) == (1, 1, 1, 1)
    assert sent[0].url.scheme == "https"
    assert sent[0].url.host == "fuyao.test"
    assert sent[0].url.path == "/api/a-share-index/prices/historical"
    assert dict(sent[0].url.params.multi_items()) == _QUERY
    assert sent[0].headers.get_list("host", split_commas=False) == ["fuyao.test"]
    assert sent[0].headers.get_list("accept-encoding", split_commas=False) == ["identity"]
    assert sent[0].headers.get_list("x-api-key", split_commas=False) == ["synthetic-key"]
    assert {name.lower() for name, _value in sent[0].headers.multi_items()} == {
        "accept",
        "accept-encoding",
        "connection",
        "host",
        "user-agent",
        "x-api-key",
    }
    locked_attempt = _locked_scope().attempt
    assert locked_attempt.request_id == _REQUEST_ID
    assert locked_attempt.attempt_id == _ATTEMPT_ID
    assert locked_attempt.attempt_no == 1
    assert result.attempt_trace[0].transport_request_id == _REQUEST_ID
    assert result.attempt_trace[0].attempt_id == _ATTEMPT_ID
    assert logged_request_ids == [_REQUEST_ID]


def test_v4_raw_exchange_uses_the_same_locked_identity(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The raw one-send path consumes the same V4 lock and stable IDs."""
    sent: list[httpx.Request] = []
    engine, gate = configured_gate

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=b"raw-synthetic")

    with _client(monkeypatch, handler, source_attempt_gate=gate) as client, _one_shot_scope():
        result = client.get_raw_exchange("/api/a-share-index/prices/historical", params=_QUERY)

    assert result.body == b"raw-synthetic"
    assert len(sent) == 1
    assert _gate_counts(engine) == (1, 1, 1, 1)
    assert result.attempt_trace[0].transport_request_id == _REQUEST_ID
    assert result.attempt_trace[0].attempt_id == _ATTEMPT_ID


@pytest.mark.parametrize(
    ("endpoint", "params", "host_header"),
    [
        ("/api/other", _QUERY, None),
        (
            "/api/a-share-index/prices/historical",
            {**_QUERY, "thscode": "000300.SH"},
            None,
        ),
        ("/api/a-share-index/prices/historical", _QUERY, "other-fuyao.test"),
    ],
)
def test_v4_prepared_route_query_or_host_mismatch_fails_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
    endpoint: str,
    params: dict[str, str],
    host_header: str | None,
) -> None:
    """Route, scalar query, and Host mismatches cannot consume or send."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        if host_header is not None:
            client._client.headers["Host"] = host_header
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get(endpoint, params=params)

    assert calls == {"budget": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


def test_v4_scope_rejects_mismatched_pins_before_transport() -> None:
    """The V3 route/query pin must equal the complete pre-locked gate scope."""
    different_pin = _pin(path="/api/different")

    with (
        pytest.raises(RequestAuthorizationError, match="identities or policy pins disagree"),
        _one_shot_scope(different_pin, locked_scope=_locked_scope()),
    ):
        pytest.fail("mismatched V4 identity unexpectedly entered the scope")


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Authorization", "Bearer synthetic"),
        ("Cookie", "session=synthetic"),
        ("X-Unpinned-Header", "synthetic"),
    ],
)
def test_v4_rejects_unpinned_headers_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
    header: str,
    value: str,
) -> None:
    """Only fixed HTTPX defaults, Host, and the Fuyao API key may be sent."""
    calls = {"budget": 0, "send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        client._client.headers[header] = value
        monkeypatch.setattr(
            http_client_module,
            "reserve_scoped_attempt",
            lambda *_args: calls.__setitem__("budget", calls["budget"] + 1),
        )
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


def test_v4_rejects_unpinned_user_agent_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The actual prepared User-Agent is validated before any reservation or send."""
    calls = {"budget": 0, "send": 0}
    prepared_user_agents: list[str | None] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        client._client.headers["User-Agent"] = "untrusted-client"
        original_validator = client._validate_v3_prepared_request

        def record_prepared_user_agent(
            scopes: tuple[RequestExecutionScope, ...],
            request: httpx.Request,
        ) -> None:
            prepared_user_agents.append(request.headers.get("user-agent"))
            original_validator(scopes, request)

        monkeypatch.setattr(client, "_validate_v3_prepared_request", record_prepared_user_agent)
        monkeypatch.setattr(
            http_client_module,
            "reserve_scoped_attempt",
            lambda *_args: calls.__setitem__("budget", calls["budget"] + 1),
        )
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert prepared_user_agents == ["untrusted-client"]
    assert calls == {"budget": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


@pytest.mark.parametrize(
    "replacement",
    [
        "object",
        "instance_method",
        "class_method",
        "storage_instance_method",
        "storage_class_method",
    ],
)
def test_v4_gate_identity_and_method_replacements_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
    replacement: str,
) -> None:
    """A changed gate instance or V4 method cannot manufacture authorization."""
    calls = {"gate": 0, "send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        if replacement == "object":
            client._source_attempt_gate = object()  # type: ignore[assignment]
        elif replacement == "instance_method":
            gate.reserve_one_shot_attempt = lambda _scope: AttemptReservation(
                status=ReservationStatus.RESERVED_NEW,
                reserved_at_us=7,
            )
        elif replacement == "class_method":
            monkeypatch.setattr(
                SourceAttemptGate,
                "reserve_one_shot_attempt",
                lambda _self, _scope: AttemptReservation(
                    status=ReservationStatus.RESERVED_NEW,
                    reserved_at_us=7,
                ),
            )
        elif replacement == "storage_instance_method":

            def forged_storage(*_args: object, **_kwargs: object) -> AttemptReservation:
                calls["gate"] += 1
                return AttemptReservation(
                    status=ReservationStatus.RESERVED_NEW,
                    reserved_at_us=7,
                )

            gate._reserve_attempt = forged_storage
        else:
            monkeypatch.setattr(
                SourceAttemptGate,
                "_reserve_attempt",
                lambda _self, _attempt, _scope, **_kwargs: AttemptReservation(
                    status=ReservationStatus.RESERVED_NEW,
                    reserved_at_us=7,
                ),
            )
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls == {"gate": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


def test_v4_non_mock_transport_rejects_gate_storage_entrypoint_override() -> None:
    """A production HTTP transport cannot use an instance-patched DB writer."""
    gate = object.__new__(SourceAttemptGate)
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url="https://fuyao.test"),
        source_attempt_gate=gate,
        max_attempts=1,
    )
    calls = {"gate": 0, "send": 0}

    def forged_storage(*_args: object, **_kwargs: object) -> AttemptReservation:
        calls["gate"] += 1
        return AttemptReservation(status=ReservationStatus.RESERVED_NEW, reserved_at_us=7)

    gate._reserve_attempt = forged_storage
    with client, pytest.raises(RequestAuthorizationError, match="gate method identity"):
        client._validate_v4_gate_integrity()

    assert calls == {"gate": 0, "send": 0}


def test_v4_rechecks_headers_after_reservation_before_send(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A header added after the gate returns is rejected before HTTP send."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        original_reserve = client._reserve_one_shot_source_attempt

        def reserve_then_add_cookie(*args: object, **kwargs: object):
            reservation = original_reserve(*args, **kwargs)  # type: ignore[arg-type]
            request = kwargs["prepared_request"]
            assert isinstance(request, httpx.Request)
            request.headers["Cookie"] = "session=synthetic"
            return reservation

        monkeypatch.setattr(client, "_reserve_one_shot_source_attempt", reserve_then_add_cookie)
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 0
    assert _gate_counts(engine) == (1, 1, 1, 1)


def test_v4_duplicate_locked_scope_never_sends_twice_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An exact replay is rejected after its one-shot permit was consumed."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        with _one_shot_scope():
            response = client.get("/api/a-share-index/prices/historical", params=_QUERY)
            assert response.status_code == 200
        with _one_shot_scope(), pytest.raises(RequestAuthorizationError):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 1
    assert _gate_counts(engine) == (1, 1, 1, 1)


def test_v4_denied_locked_scope_never_sends_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A revoked grant produces a durable denial and no physical send."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    gate.revoke_grant(
        _binding().grant_id,
        expected_revision=1,
        actor="offline-test",
        reason_code="synthetic-test-revocation",
    )
    with (
        _client(monkeypatch, handler, source_attempt_gate=gate) as client,
        _one_shot_scope(),
        pytest.raises(RequestAuthorizationError),
    ):
        client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 0
    assert _gate_counts(engine) == (1, 1, 1, 1)


def test_v4_locked_scope_identity_conflict_never_sends_or_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A changed stable ID cannot consume the locked request or send."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    changed_attempt = replace(_locked_scope().attempt, attempt_id="changed-attempt")
    changed_scope = replace(_locked_scope(), attempt=changed_attempt)
    with (
        _client(monkeypatch, handler, source_attempt_gate=gate) as client,
        _one_shot_scope(locked_scope=changed_scope),
        pytest.raises(RequestAuthorizationError),
    ):
        client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 0
    assert _gate_counts(engine) == (1, 0, 0, 0)


def test_v4_unavailable_gate_never_falls_back_to_v3_or_v1(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A missing persistent V4 schema fails closed and never reaches transport."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    database_path = engine.url.database
    assert database_path is not None
    engine.dispose()
    Path(database_path).unlink()
    with (
        _client(monkeypatch, handler, source_attempt_gate=gate) as client,
        _one_shot_scope(),
        pytest.raises(RequestAuthorizationError),
    ):
        client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 0


def test_v4_requires_exactly_one_configured_client_attempt(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A client that permits retries cannot enter the one-shot send path."""
    calls = {"budget": 0, "send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(
        monkeypatch,
        handler,
        max_attempts=2,
        source_attempt_gate=gate,
    ) as client:
        monkeypatch.setattr(
            http_client_module,
            "reserve_scoped_attempt",
            lambda *_args: calls.__setitem__("budget", calls["budget"] + 1),
        )
        with (
            _one_shot_scope(),
            pytest.raises(
                RequestAuthorizationError,
                match="max_attempts=1",
            ),
        ):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


@pytest.mark.parametrize(
    "timeout",
    [None, True, 0, -1, float("nan"), float("inf"), float("-inf")],
)
def test_v4_requires_a_finite_positive_timeout_before_reservation_or_send(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """One-shot mode rejects disabled, nonnumeric, and unbounded timeouts."""
    calls = {"budget": 0, "send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    engine, gate = configured_gate
    with _client(monkeypatch, handler, source_attempt_gate=gate) as client:
        client._timeout_seconds = timeout  # type: ignore[assignment]
        monkeypatch.setattr(
            http_client_module,
            "reserve_scoped_attempt",
            lambda *_args: calls.__setitem__("budget", calls["budget"] + 1),
        )
        with (
            _one_shot_scope(),
            pytest.raises(
                RequestAuthorizationError,
                match="finite positive timeout",
            ),
        ):
            client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert _gate_counts(engine) == (1, 0, 0, 0)


def test_v4_network_outcome_uncertainty_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A timeout after the one-shot reservation produces one send only."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        raise httpx.ConnectTimeout("synthetic timeout")

    engine, gate = configured_gate
    with (
        _client(monkeypatch, handler, source_attempt_gate=gate) as client,
        _one_shot_scope(),
        pytest.raises(FuyaoError),
    ):
        client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 1
    locked_attempt = _locked_scope().attempt
    assert locked_attempt.request_id == _REQUEST_ID
    assert locked_attempt.attempt_id == _ATTEMPT_ID
    assert _gate_counts(engine) == (1, 1, 1, 1)


def test_v4_scope_without_gate_is_refused_without_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit V4 pins have no path through the client without a gate."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    with (
        _client(monkeypatch, handler) as client,
        _one_shot_scope(),
        pytest.raises(RequestAuthorizationError, match="durable gate"),
    ):
        client.get("/api/a-share-index/prices/historical", params=_QUERY)

    assert calls["send"] == 0


def test_v4_locked_attempt_must_be_number_one() -> None:
    """A lock cannot authorize an attempt number that implies prior retries."""
    with (
        pytest.raises(RequestAuthorizationError, match="attempt_no"),
        _one_shot_scope(locked_scope=_locked_scope(attempt_no=2)),
    ):
        pytest.fail("attempt number two unexpectedly entered the V4 scope")
