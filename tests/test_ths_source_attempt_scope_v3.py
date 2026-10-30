"""Offline THS transport checks for exact V3 query pins and no fallback."""

from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING

import httpx
import pytest

from opendata.data.providers.ths.transport import http_client as http_client_module
from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
from opendata.data.providers.ths.transport.http_client import FuyaoHttpClient
from opendata.data.providers.ths.transport.rate_limiter import FuyaoRateLimiter
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V3,
    RequestAuthorizationError,
    RequestBudget,
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    request_execution_scope,
    validate_source_attempt_scope_v3,
)
from opendata.data.request_summary import request_summary_sha256
from opendata.data.source_attempt_gate import AttemptReservation, ReservationStatus

if TYPE_CHECKING:
    from collections.abc import Callable

_QUERY = {"stock_code": "000001"}
_ALLOWED_FIELDS = ("stock_code",)
_RIGHTS_REFERENCE = "evidence:ths-research-2026"


class _RecordingGate:
    """Record gate calls without touching a database."""

    def __init__(
        self,
        *,
        status: ReservationStatus = ReservationStatus.RESERVED_NEW,
        on_v3: Callable[[object], None] | None = None,
        error: Exception | None = None,
    ) -> None:
        """Initialize one deterministic local gate stub."""
        self.status = status
        self.on_v3 = on_v3
        self.error = error
        self.v1_attempts: list[object] = []
        self.v3_scopes: list[object] = []

    def reserve_attempt(self, attempt: object) -> AttemptReservation:
        """Record a legacy reservation call so V3 fallback is observable."""
        self.v1_attempts.append(attempt)
        return AttemptReservation(status=ReservationStatus.RESERVED_NEW, reserved_at_us=1)

    def reserve_attempt_v3(self, scope: object) -> AttemptReservation:
        """Record a V3 reservation or raise the configured failure."""
        self.v3_scopes.append(scope)
        if self.on_v3 is not None:
            self.on_v3(scope)
        if self.error is not None:
            raise self.error
        return AttemptReservation(status=self.status, reserved_at_us=2)


def _summary(parameters: dict[str, str] = _QUERY) -> str:
    """Return the source project's canonical scalar-query digest."""
    return request_summary_sha256(parameters, allowed_fields=_ALLOWED_FIELDS)


def _pin(
    summary: str | None = None,
    *,
    host: str = "fuyao.test",
    path: str = "/v1/financial",
) -> SourceAttemptScopeV3Pin:
    """Build a synthetic transport pin with exact, bounded policy identities."""
    return SourceAttemptScopeV3Pin(
        capture_intent_id="intent-ths-synthetic",
        request_summary_sha256=summary or _summary(),
        request_summary_allowed_fields=_ALLOWED_FIELDS,
        expected_https_host=host,
        expected_path=path,
        expected_method="GET",
        source_policy_id="policy-ths-synthetic",
        source_policy_revision="revision-1",
        source_policy_sha256="a" * 64,
        budget_binding_sha256="b" * 64,
        budget_quota_key="quota-ths-synthetic",
        budget_policy_revision=1,
        budget_policy_sha256="c" * 64,
    )


def _binding(*, evidence_reference: str | None = _RIGHTS_REFERENCE) -> SourceAttemptBinding:
    """Build a synthetic per-run gate binding."""
    return SourceAttemptBinding(
        grant_id="grant-ths-synthetic",
        expected_revision=1,
        principal_scope="principal-synthetic",
        run_key="run-ths-synthetic",
        expected_rights_evidence_reference=evidence_reference,
    )


def _success_response() -> httpx.Response:
    """Return one deterministic success envelope accepted by the parser."""
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
) -> FuyaoHttpClient:
    """Build a verified offline client with local authorization seams."""
    client = FuyaoHttpClient(
        credentials=FuyaoCredentials("synthetic-key", base_url="https://fuyao.test"),
        mock_transport=httpx.MockTransport(handler),
        rate_limiter=FuyaoRateLimiter(rate_per_second=1_000, burst=1_000),
        max_attempts=max_attempts,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setattr(http_client_module, "authorize_request", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", lambda *_args: None)
    monkeypatch.setattr(http_client_module, "scoped_host_slot", lambda *_args: nullcontext())
    monkeypatch.setattr(client, "_scoped_shared_host_request", lambda *_args: nullcontext())
    monkeypatch.setattr(client._limiter, "wait_and_acquire", lambda *_args: None)
    return client


def _v3_scope(
    pin: SourceAttemptScopeV3Pin | None = None,
    *,
    binding: SourceAttemptBinding | None = None,
):
    """Open the explicitly selected V3 request scope for one synthetic call."""
    return request_execution_scope(
        source="ths",
        canonical_model="financial_statement",
        operation=RequestOperation.QUERY,
        budget=RequestBudget(),
        source_attempt_binding=binding or _binding(),
        source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
        source_attempt_scope_v3_pin=pin,
    )


def test_v3_get_recomputes_query_and_reserves_the_complete_v3_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual prepared query and all trusted pins reach only the V3 gate."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()):
            result = client.get("/v1/financial", params=_QUERY)

    assert result.status_code == 200
    assert len(sent) == len(gate.v3_scopes) == 1
    assert gate.v1_attempts == []
    scope = gate.v3_scopes[0]
    assert scope.attempt.expected_rights_evidence_reference == _RIGHTS_REFERENCE
    assert scope.attempt.run_key == "run-ths-synthetic"
    assert scope.attempt.host == scope.request_host == "fuyao.test"
    assert scope.request_scheme == "https"
    assert scope.request_method == "GET"
    assert scope.request_path == "/v1/financial"
    assert scope.request_summary_sha256 == _summary()
    assert scope.capture_intent_id == "intent-ths-synthetic"
    assert scope.source_policy_id == "policy-ths-synthetic"
    assert scope.source_policy_revision == "revision-1"
    assert scope.source_policy_sha256 == "a" * 64
    assert scope.budget_binding_sha256 == "b" * 64
    assert scope.budget_quota_key == "quota-ths-synthetic"
    assert scope.budget_policy_revision == 1
    assert scope.budget_policy_sha256 == "c" * 64


def test_v3_query_mismatch_fails_before_gate_reservation_or_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expected digest for another query cannot reach either gate or transport."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with (
            _v3_scope(_pin(_summary({"stock_code": "000002"}))),
            pytest.raises(RequestAuthorizationError),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 0
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


def test_v3_path_drift_with_the_same_query_fails_before_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching query cannot reuse a pin for another endpoint path."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin(path="/v1/another-financial-endpoint")),
            pytest.raises(RequestAuthorizationError),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


def test_v3_host_drift_with_the_same_query_fails_before_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching query cannot reuse a pin for another HTTPS host."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin(host="other-fuyao.test")),
            pytest.raises(RequestAuthorizationError),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


def test_v3_accepts_httpx_generated_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical Host header generated from the pinned URL remains valid."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()):
            result = client.get("/v1/financial", params=_QUERY)

    assert result.status_code == 200
    assert len(sent) == len(gate.v3_scopes) == 1
    assert sent[0].headers.get_list("host", split_commas=False) == ["fuyao.test"]
    assert gate.v1_attempts == []


def test_v3_accepts_explicit_default_port_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit Host authority with the HTTPS default port is equivalent."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        client._client.headers["Host"] = "fuyao.test:443"
        with _v3_scope(_pin()):
            result = client.get("/v1/financial", params=_QUERY)

    assert result.status_code == 200
    assert len(sent) == len(gate.v3_scopes) == 1
    assert sent[0].headers.get_list("host", split_commas=False) == ["fuyao.test:443"]
    assert gate.v1_attempts == []


@pytest.mark.parametrize(
    "host_value",
    [
        "other-fuyao.test",
        "fuyao.test:8443",
        "user@fuyao.test",
        "fuyao.test, other-fuyao.test",
        " fuyao.test",
        "fuyao.test ",
        "fuyao.test.",
        "fuyao%2etest",
        "",
    ],
)
def test_v3_invalid_host_authority_fails_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    host_value: str,
) -> None:
    """Noncanonical Host authorities cannot spend budget, reserve, or send."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        client._client.headers["Host"] = host_value
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin()),
            pytest.raises(RequestAuthorizationError, match="Host header"),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


@pytest.mark.parametrize(
    "host_values",
    [
        ("fuyao.test", "fuyao.test"),
        ("fuyao.test", "other-fuyao.test"),
    ],
)
def test_v3_duplicate_host_headers_fail_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    host_values: tuple[str, str],
) -> None:
    """Repeated Host fields are rejected even when one value matches the pin."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        client._client.headers = httpx.Headers(
            [(b"host", value.encode("ascii")) for value in host_values]
        )
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin()),
            pytest.raises(RequestAuthorizationError, match="Host header"),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "/v1/./financial",
        "/v1/../financial",
        "/v1/%2e%2e/financial",
        "/v1/%2Ffinancial",
        "/v1/%5C/financial",
        "/v1//financial",
    ],
)
def test_v3_noncanonical_path_alias_fails_before_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """Dot, encoded-separator, and repeated-slash aliases cannot reach HTTPX."""
    calls = {"send": 0, "budget": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin()),
            pytest.raises(RequestAuthorizationError, match="target URL"),
        ):
            client.get(endpoint, params=_QUERY)

    assert calls == {"send": 0, "budget": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


@pytest.mark.parametrize("method_name", ["build_request", "send"])
def test_v3_httpx_instance_method_override_fails_before_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Per-instance HTTPX overrides cannot prepare or send a V3 request."""
    calls = {"override": 0, "send": 0, "budget": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def replacement(*_args: object, **_kwargs: object) -> None:
        calls["override"] += 1
        raise AssertionError("untrusted instance method override must not run")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        setattr(client._client, method_name, replacement)
        with (
            _v3_scope(_pin()),
            pytest.raises(RequestAuthorizationError, match="methods"),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"override": 0, "send": 0, "budget": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


@pytest.mark.parametrize("method_name", ["build_request", "send"])
def test_v3_httpx_class_method_override_fails_before_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
) -> None:
    """Later HTTPX class-level replacement fails before reservation or dispatch."""
    calls = {"override": 0, "send": 0, "budget": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def replacement(*_args: object, **_kwargs: object) -> None:
        calls["override"] += 1
        raise AssertionError("untrusted class method override must not run")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        monkeypatch.setattr(httpx.Client, method_name, replacement)
        with (
            _v3_scope(_pin()),
            pytest.raises(RequestAuthorizationError, match="HTTPX methods"),
        ):
            client.get("/v1/financial", params=_QUERY)

    assert calls == {"override": 0, "send": 0, "budget": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


def test_v3_requires_a_pin_and_exact_rights_reference_at_scope_creation() -> None:
    """V3 cannot be constructed with a missing pin or blank source-rights reference."""
    with pytest.raises(RequestAuthorizationError, match="binding and complete pin"), _v3_scope():
        pytest.fail("missing V3 pin must fail before entering the scope")

    with (
        pytest.raises(RequestAuthorizationError, match="exact rights ref"),
        _v3_scope(_pin(), binding=_binding(evidence_reference=None)),
    ):
        pytest.fail("missing rights reference must fail before entering the scope")


def test_v3_pin_tampering_fails_before_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frozen caller pin changed after construction fails its integrity check."""
    calls = {"send": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    gate = _RecordingGate()
    pin = _pin()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(pin):
            object.__setattr__(pin, "budget_quota_key", "tampered-quota")
            with pytest.raises(RequestAuthorizationError):
                client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 0
    assert gate.v3_scopes == []


def test_v3_reservation_failure_never_falls_back_to_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate exception fails closed without invoking the legacy reservation API."""
    calls = {"send": 0}
    gate = _RecordingGate(error=RuntimeError("synthetic storage failure"))

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()), pytest.raises(RequestAuthorizationError, match="V3 gate"):
            client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 0
    assert len(gate.v3_scopes) == 1
    assert gate.v1_attempts == []


def test_v3_duplicate_reservation_is_not_reused_or_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate V3 tombstone cannot authorize a send or a V1 retry."""
    calls = {"send": 0}
    gate = _RecordingGate(status=ReservationStatus.DUPLICATE)

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()), pytest.raises(RequestAuthorizationError, match="V3 gate"):
            client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 0
    assert len(gate.v3_scopes) == 1
    assert gate.v1_attempts == []


def test_v3_retry_reserves_a_new_attempt_identity_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each retry has a unique gate attempt while preserving one request identity."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(
                200,
                json={
                    "code": 5001,
                    "message": "temporary",
                    "request_id": "provider-request-synthetic",
                },
            )
        return _success_response()

    with _client(monkeypatch, handler, max_attempts=2) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()):
            result = client.get("/v1/financial", params=_QUERY)

    assert result.status_code == 200
    assert len(sent) == len(gate.v3_scopes) == 2
    attempts = [scope.attempt for scope in gate.v3_scopes]
    assert [attempt.attempt_no for attempt in attempts] == [1, 2]
    assert len({attempt.attempt_id for attempt in attempts}) == 2
    assert len({attempt.request_id for attempt in attempts}) == 1
    assert all(scope.request_summary_sha256 == _summary() for scope in gate.v3_scopes)
    assert gate.v1_attempts == []


def test_v3_redirect_is_not_followed_or_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A V3 redirect returns a terminal error after exactly one reserved send."""
    calls = {"send": 0}
    gate = _RecordingGate()

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return httpx.Response(
            302,
            headers={"Location": "https://other.example.invalid/financial"},
            content=b"redirect",
        )

    with _client(monkeypatch, handler, max_attempts=3) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin()), pytest.raises(RequestRedirectError):
            client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 1
    assert len(gate.v3_scopes) == 1
    assert gate.v1_attempts == []


def test_v3_pin_mutated_during_reservation_blocks_the_same_prepared_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins are checked again after storage returns and immediately before send."""
    calls = {"send": 0}
    pin = _pin()
    gate = _RecordingGate(
        on_v3=lambda _scope: object.__setattr__(pin, "source_policy_revision", "changed")
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return _success_response()

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(pin), pytest.raises(RequestAuthorizationError):
            client.get("/v1/financial", params=_QUERY)

    assert calls["send"] == 0
    assert len(gate.v3_scopes) == 1
    assert gate.v1_attempts == []


def test_legacy_v1_scope_without_v3_pin_keeps_legacy_gate_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing V1 callers without a V3 pin continue to reserve via the V1 API."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _success_response()

    binding = _binding(evidence_reference=None)
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with request_execution_scope(
            source="ths",
            canonical_model="financial_statement",
            operation=RequestOperation.QUERY,
            budget=RequestBudget(),
            source_attempt_binding=binding,
        ):
            client.get("/v1/financial", params=_QUERY)

    assert len(sent) == 1
    assert len(gate.v1_attempts) == 1
    assert gate.v3_scopes == []


def test_v3_raw_exchange_uses_the_same_prepared_query_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw one-send path also reserves its exact prepared query through V3."""
    sent: list[httpx.Request] = []
    gate = _RecordingGate()

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, content=b"raw-synthetic")

    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        with _v3_scope(_pin(path="/v1/raw")):
            result = client.get_raw_exchange("/v1/raw", params=_QUERY)

    assert result.body == b"raw-synthetic"
    assert len(sent) == len(gate.v3_scopes) == 1
    assert sent[0].url.scheme == "https"
    assert sent[0].url.host == "fuyao.test"
    assert sent[0].url.path == "/v1/raw"
    assert gate.v3_scopes[0].request_summary_sha256 == _summary()
    assert gate.v1_attempts == []


def test_v3_raw_exchange_host_mismatch_fails_before_budget_gate_or_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw one-send path checks Host authority before spending its request budget."""
    calls = {"budget": 0, "send": 0}

    def reserve_budget(*_args: object) -> None:
        calls["budget"] += 1

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["send"] += 1
        return httpx.Response(200, content=b"raw-synthetic")

    gate = _RecordingGate()
    with _client(monkeypatch, handler) as client:
        client._source_attempt_gate = gate
        client._client.headers["Host"] = "other-fuyao.test"
        monkeypatch.setattr(http_client_module, "reserve_scoped_attempt", reserve_budget)
        with (
            _v3_scope(_pin(path="/v1/raw")),
            pytest.raises(RequestAuthorizationError, match="Host header"),
        ):
            client.get_raw_exchange("/v1/raw", params=_QUERY)

    assert calls == {"budget": 0, "send": 0}
    assert gate.v3_scopes == []
    assert gate.v1_attempts == []


def test_v3_scope_validator_rejects_mismatched_enclosing_pins() -> None:
    """The reusable scope validator refuses different nested policy pins."""
    first_pin = _pin()
    second_pin = _pin()
    object.__setattr__(second_pin, "source_policy_sha256", "d" * 64)
    with _v3_scope(first_pin) as first:
        assert first is not None
        with pytest.raises(RequestAuthorizationError):
            validate_source_attempt_scope_v3(
                (
                    first,
                    type(first)(
                        source=first.source,
                        canonical_model=first.canonical_model,
                        operation=first.operation,
                        budget=first.budget,
                        source_attempt_binding=first.source_attempt_binding,
                        source_attempt_scope_version=SOURCE_ATTEMPT_SCOPE_V3,
                        source_attempt_scope_v3_pin=second_pin,
                    ),
                )
            )
