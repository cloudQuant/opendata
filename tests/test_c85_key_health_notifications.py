"""Branch-level tests for the credential-health notification plane.

The behaviour suite already proves the de-duplication story. What it never
executes is the rejecting and routing half of the module: the TTL guard, the
patrol-result shapes that must *not* be stored, the transition that has to wait
for evidence, the bookkeeping of a half-delivered event, and the channel
dispatch itself. Every transport below is a hand-written fake that only records
what it was given - no socket, no SMTP conversation, no warehouse.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from email.message import Message
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from opendata.core.config import Settings
from opendata.pipeline import key_health_notifications as notifications
from opendata.pipeline.key_health import (
    CLASS_CREDENTIAL_REJECTED,
    CLASS_PATROL_GAP,
    CLASS_QUOTA_EXHAUSTED,
    CLASS_SOURCE_DEGRADED,
    CLASS_UNCLASSIFIED,
    FailureObservation,
    KeyReport,
    build_report,
)
from opendata.pipeline.patrol import PatrolResult

if TYPE_CHECKING:
    from collections.abc import Iterator

AS_OF = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
TTL = timedelta(hours=1)
BOTH = frozenset({"websocket", "email"})
WS_ONLY = frozenset({"websocket"})
EMAIL_ONLY = frozenset({"email"})


def _report(
    source: str = "ths",
    observations: tuple[FailureObservation, ...] = (),
    *,
    expires_at: date | None = None,
) -> KeyReport:
    """Grade one deterministic report without reading application secrets."""
    endpoint = "api.stlouisfed.org" if source == "fred" else "fuyao.aicubes.cn"
    return build_report(
        source,
        required=True,
        configured=True,
        endpoint=endpoint,
        observations=observations,
        expires_at=expires_at,
        as_of=AS_OF,
        expiry_warning_days=14,
    )


def _rejected(source: str = "ths") -> KeyReport:
    """A report with one current credential rejection."""
    return _report(source, (FailureObservation(source, CLASS_CREDENTIAL_REJECTED, "status=401"),))


def _spent(source: str = "ths") -> KeyReport:
    """A report with one current quota rejection."""
    return _report(source, (FailureObservation(source, CLASS_QUOTA_EXHAUSTED, "status=429"),))


def _result(
    *,
    failure_class: str | None,
    attribution: str | None,
    domain: str = "stock_daily",
    source: str = "ths",
    verified: bool = True,
) -> PatrolResult:
    """Build one patrol leg without probing anything."""
    return PatrolResult(
        domain=domain,
        source=source,
        ok=failure_class is None,
        error="private provider message must not be stored",
        latency_ms=1.0,
        verified=verified,
        failure_class=failure_class,
        attribution=attribution,
    )


def _event(notification_type: str = "key_health_alert", source: str = "ths") -> dict[str, object]:
    """One credential-safe event as the notifier would render it."""
    return {
        "notification_type": notification_type,
        "source": source,
        "level": "alert",
        "report": _report(source).as_dict(),
        "last_observed_at": NOW.isoformat(),
        "remaining_quota": None,
    }


class RecordingWebSocketManager:
    """Broadcast hub that records payloads instead of touching a socket."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.payloads: list[dict[str, object]] = []
        self._fail_with = fail_with

    async def broadcast(self, message: dict[str, object]) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.payloads.append(message)


class RecordingSmtpSend:
    """Synchronous SMTP helper replacement; records kwargs, optionally fails."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._fail_with = fail_with

    def __call__(self, **kwargs: object) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.calls.append(kwargs)


def _message(call: dict[str, object]) -> Message:
    """Return the composed MIME message a recorded SMTP call was given."""
    message = call["msg"]
    assert isinstance(message, Message)
    return message


def _body_text(message: Message) -> str:
    """Return the first attached plain-text part decoded back to text."""
    part = message.get_payload()[0]
    decoded: bytes = part.get_payload(decode=True)
    return decoded.decode("utf-8")


@pytest.fixture
def log_records() -> Iterator[list[str]]:
    """Collect warning-level loguru messages emitted during one test."""
    collected: list[str] = []
    handler_id = logger.add(
        lambda message: collected.append(str(message).rstrip("\n")),
        level="WARNING",
        format="{message}",
    )
    try:
        yield collected
    finally:
        logger.remove(handler_id)


@pytest.fixture
def fresh_store() -> Iterator[notifications.RecentObservationStore]:
    """Yield an isolated in-memory store."""
    store = notifications.RecentObservationStore()
    try:
        yield store
    finally:
        store.clear()


def _settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> Settings:
    """Install an in-memory Settings object for the dispatch helpers."""
    configured = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    monkeypatch.setattr("opendata.core.config.settings", configured)
    return configured


class TestObservationStoreGuards:
    """A non-positive TTL is a misconfiguration, not an infinite window."""

    @pytest.mark.parametrize("ttl", [timedelta(0), timedelta(hours=-1)])
    def test_record_rejects_a_nonpositive_ttl(
        self,
        fresh_store: notifications.RecentObservationStore,
        ttl: timedelta,
    ) -> None:
        with pytest.raises(ValueError, match="observation TTL must be positive"):
            fresh_store.record(
                [_result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401")],
                ttl=ttl,
                now=NOW,
            )

    @pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-30)])
    def test_snapshot_rejects_a_nonpositive_ttl(
        self,
        fresh_store: notifications.RecentObservationStore,
        ttl: timedelta,
    ) -> None:
        with pytest.raises(ValueError, match="observation TTL must be positive"):
            fresh_store.snapshot(ttl=ttl, now=NOW)

    def test_only_key_bearing_verified_legs_are_stored(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        stored = fresh_store.record(
            [
                _result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401"),
                _result(
                    failure_class=CLASS_QUOTA_EXHAUSTED,
                    attribution="status=429",
                    source="fred",
                ),
                _result(
                    failure_class=CLASS_CREDENTIAL_REJECTED,
                    attribution="status=403",
                    source="bls",
                ),
                _result(
                    failure_class=CLASS_SOURCE_DEGRADED,
                    attribution="status=503",
                    source="akshare",
                    domain="daily",
                ),
            ],
            ttl=TTL,
            now=NOW,
        )

        snapshot = fresh_store.snapshot(ttl=TTL, now=NOW)

        assert stored == 2
        assert {(item.source, item.failure_class) for item in snapshot.observations} == {
            ("ths", CLASS_CREDENTIAL_REJECTED),
            ("fred", CLASS_QUOTA_EXHAUSTED),
        }
        assert snapshot.last_observed_at == {"ths": NOW.isoformat(), "fred": NOW.isoformat()}

    def test_unverified_legs_never_become_an_alert(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        stored = fresh_store.record(
            [
                _result(
                    failure_class=CLASS_CREDENTIAL_REJECTED,
                    attribution="status=401",
                    verified=False,
                )
            ],
            ttl=TTL,
            now=NOW,
        )

        assert stored == 0
        assert fresh_store.snapshot(ttl=TTL, now=NOW).observations == ()

    def test_patrol_gap_is_not_a_key_failure(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        stored = fresh_store.record(
            [_result(failure_class=CLASS_PATROL_GAP, attribution="status=401")],
            ttl=TTL,
            now=NOW,
        )

        assert stored == 0
        assert fresh_store.snapshot(ttl=TTL, now=NOW).observations == ()

    def test_a_class_this_module_cannot_read_is_stored_as_unclassified(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [_result(failure_class="some-future-class", attribution="connection reset by peer")],
            ttl=TTL,
            now=NOW,
        )

        observation = fresh_store.snapshot(ttl=TTL, now=NOW).observations[0]

        assert observation.failure_class == CLASS_UNCLASSIFIED
        assert observation.attribution is None

    def test_expired_then_passed_capability_is_a_confirmed_recovery(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [_result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401")],
            ttl=TTL,
            now=NOW,
        )
        aged_out = fresh_store.snapshot(ttl=TTL, now=NOW + TTL + timedelta(seconds=1))
        assert aged_out.expired_sources == frozenset({"ths"})

        fresh_store.record(
            [_result(failure_class=None, attribution=None)],
            ttl=TTL,
            now=NOW + TTL + timedelta(seconds=2),
        )
        recovered = fresh_store.snapshot(ttl=TTL, now=NOW + TTL + timedelta(seconds=2))

        assert recovered.observations == ()
        assert recovered.recovered_sources == frozenset({"ths"})
        assert recovered.expired_sources == frozenset()

    def test_a_pass_on_one_capability_leaves_a_sibling_observation_expired(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [
                _result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401"),
                _result(
                    failure_class=CLASS_QUOTA_EXHAUSTED,
                    attribution="status=429",
                    domain="index_daily",
                ),
            ],
            ttl=TTL,
            now=NOW,
        )
        fresh_store.snapshot(ttl=TTL, now=NOW + TTL + timedelta(seconds=1))

        fresh_store.record(
            [_result(failure_class=None, attribution=None)],
            ttl=TTL,
            now=NOW + TTL + timedelta(seconds=2),
        )
        fresh_store.record(
            [
                _result(
                    failure_class=CLASS_SOURCE_DEGRADED,
                    attribution="status=503",
                    domain="financial_statement",
                )
            ],
            ttl=TTL,
            now=NOW + TTL + timedelta(seconds=3),
        )
        snapshot = fresh_store.snapshot(ttl=TTL, now=NOW + TTL + timedelta(seconds=3))

        assert [item.failure_class for item in snapshot.observations] == [CLASS_SOURCE_DEGRADED]
        assert snapshot.recovered_sources == frozenset()
        assert snapshot.expired_sources == frozenset()


class TestNotifierTransitions:
    """Every state change either has evidence or waits for it."""

    async def test_clean_report_without_an_active_issue_is_silent(self) -> None:
        calls: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            calls.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)

        counts = await notifier.notify({"ths": _report()})

        assert counts == {
            "alerts_sent": 0,
            "recoveries_sent": 0,
            "observations_expired": 0,
            "unchanged": 0,
            "delivery_failures": 0,
        }
        assert calls == []

    async def test_clearing_without_evidence_waits_instead_of_claiming_recovery(self) -> None:
        calls: list[str] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            calls.append(str(event["notification_type"]))
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _rejected()}, last_observed_at={"ths": NOW.isoformat()})

        waiting = await notifier.notify({"ths": _report()})
        assert waiting["recoveries_sent"] == 0
        assert waiting["observations_expired"] == 0
        assert calls == ["key_health_alert"]

        recovered = await notifier.notify({"ths": _report()}, recovered_sources=frozenset({"ths"}))
        assert recovered["recoveries_sent"] == 1
        assert calls == ["key_health_alert", "key_health_recovered"]

    async def test_a_confirmed_probe_outranks_an_expired_observation(self) -> None:
        calls: list[str] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            calls.append(str(event["notification_type"]))
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _spent()})

        counts = await notifier.notify(
            {"ths": _report()},
            recovered_sources=frozenset({"ths"}),
            expired_sources=frozenset({"ths"}),
        )

        assert counts["recoveries_sent"] == 1
        assert counts["observations_expired"] == 0
        assert calls == ["key_health_alert", "key_health_recovered"]

    async def test_failed_recovery_delivery_is_retried_until_it_lands(self) -> None:
        attempts: list[tuple[str, frozenset[str]]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            attempts.append((str(event["notification_type"]), pending))
            if event["notification_type"] == "key_health_alert":
                return pending
            return frozenset()

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _rejected()})

        first = await notifier.notify({"ths": _report()}, recovered_sources=frozenset({"ths"}))
        retry = await notifier.notify({"ths": _report()})

        assert first["delivery_failures"] == 1
        assert first["recoveries_sent"] == 0
        assert retry["delivery_failures"] == 1
        assert attempts[1:] == [("key_health_recovered", BOTH)] * 2

    async def test_expired_evidence_retries_under_its_own_event_type(self) -> None:
        attempts: list[tuple[str, frozenset[str]]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            attempts.append((str(event["notification_type"]), pending))
            if len(attempts) == 2:
                return WS_ONLY
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _rejected()})

        partial = await notifier.notify(
            {"ths": _report()},
            last_observed_at={"ths": NOW.isoformat()},
            expired_sources=frozenset({"ths"}),
        )
        assert partial["delivery_failures"] == 1
        assert partial["observations_expired"] == 0

        settled = await notifier.notify({"ths": _report()})
        assert settled["observations_expired"] == 1
        assert settled["recoveries_sent"] == 0
        assert attempts == [
            ("key_health_alert", BOTH),
            ("key_health_observation_expired", BOTH),
            ("key_health_observation_expired", EMAIL_ONLY),
        ]

    async def test_alert_event_carries_severity_timestamp_and_report(self) -> None:
        calls: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            calls.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        expired_key = _report(
            "ths",
            (FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401"),),
            expires_at=date(2026, 9, 29),
        )
        fred_later = NOW + timedelta(minutes=5)

        counts = await notifier.notify(
            {"ths": expired_key, "fred": _spent("fred")},
            last_observed_at={"ths": NOW.isoformat(), "fred": fred_later.isoformat()},
        )

        assert counts["alerts_sent"] == 2
        assert [str(event["source"]) for event in calls] == ["fred", "ths"]
        fred, ths = calls
        assert fred["notification_type"] == "key_health_alert"
        assert fred["level"] == "warn"
        assert fred["last_observed_at"] == fred_later.isoformat()
        assert ths["level"] == "alert"
        assert ths["last_observed_at"] == NOW.isoformat()
        assert ths["remaining_quota"] is None
        report = ths["report"]
        assert isinstance(report, dict)
        assert report["expiry_state"] == "expired"
        assert report["owner"] == "凭据负责人"

    async def test_sink_exception_is_counted_and_never_propagates(
        self,
        log_records: list[str],
    ) -> None:
        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            raise RuntimeError("smtp unavailable")

        notifier = notifications.KeyHealthNotifier(sink)

        counts = await notifier.notify({"ths": _rejected()})

        assert counts["alerts_sent"] == 0
        assert counts["delivery_failures"] == 1
        assert log_records == ["key health notification failed for ths: RuntimeError"]

    async def test_a_recovery_that_raises_counts_as_a_failure(
        self,
        log_records: list[str],
    ) -> None:
        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            if event["notification_type"] == "key_health_alert":
                return pending
            raise ConnectionResetError("hub closed")

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _rejected()})

        counts = await notifier.notify({"ths": _report()}, recovered_sources=frozenset({"ths"}))

        assert counts["recoveries_sent"] == 0
        assert counts["delivery_failures"] == 1
        assert log_records == ["key health notification failed for ths: ConnectionResetError"]

    async def test_partial_delivery_names_the_pending_channels(
        self,
        log_records: list[str],
    ) -> None:
        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            return WS_ONLY

        notifier = notifications.KeyHealthNotifier(sink)

        counts = await notifier.notify({"ths": _spent()})

        assert counts["delivery_failures"] == 1
        assert log_records == ["key health notification channels pending for ths: email"]

    async def test_a_superseded_partial_state_is_forgotten(self) -> None:
        attempts: list[frozenset[str]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            attempts.append(pending)
            if len(attempts) == 1:
                return WS_ONLY
            return pending

        notifier = notifications.KeyHealthNotifier(sink)

        assert (await notifier.notify({"ths": _rejected()}))["delivery_failures"] == 1
        assert (await notifier.notify({"ths": _spent()}))["alerts_sent"] == 1
        assert (await notifier.notify({"ths": _rejected()}))["alerts_sent"] == 1

        assert attempts == [BOTH, BOTH, BOTH]

    async def test_a_partial_state_of_another_source_is_kept_for_retry(self) -> None:
        attempts: list[tuple[str, frozenset[str]]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            attempts.append((str(event["source"]), pending))
            if len(attempts) == 1:
                return WS_ONLY
            return pending

        notifier = notifications.KeyHealthNotifier(sink)

        assert (await notifier.notify({"fred": _spent("fred")}))["delivery_failures"] == 1
        assert (await notifier.notify({"ths": _rejected()}))["alerts_sent"] == 1
        assert (await notifier.notify({"fred": _spent("fred")}))["alerts_sent"] == 1

        assert attempts == [("fred", BOTH), ("ths", BOTH), ("fred", EMAIL_ONLY)]


class TestScheduledHelpers:
    """The module-level helpers read configuration and never probe a provider."""

    def test_record_and_read_helpers_use_the_configured_ttl(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _settings(monkeypatch, key_health_observation_ttl_hours=2)
        store = notifications._OBSERVATIONS
        store.clear()
        try:
            active = notifications.record_patrol_observations(
                [
                    _result(
                        failure_class=CLASS_QUOTA_EXHAUSTED,
                        attribution="status=429 https://api.stlouisfed.org?api_key=secret",
                    )
                ]
            )
            snapshot = notifications.recent_patrol_observations()
        finally:
            store.clear()

        assert active == 1
        assert [item.failure_class for item in snapshot.observations] == [CLASS_QUOTA_EXHAUSTED]
        assert [item.attribution for item in snapshot.observations] == ["status=429"]
        assert set(snapshot.last_observed_at) == {"ths"}
        assert snapshot.recovered_sources == frozenset()
        assert snapshot.expired_sources == frozenset()

    async def test_scheduled_check_returns_grades_counters_and_sources(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opendata.pipeline import patrol as patrol_module

        observation = FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401")
        snapshot = notifications.ObservationSnapshot(
            observations=(observation,),
            last_observed_at={"ths": NOW.isoformat()},
            recovered_sources=frozenset(),
            expired_sources=frozenset({"fred"}),
        )
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            sent.append(event)
            return pending

        monkeypatch.setattr(notifications, "recent_patrol_observations", lambda: snapshot)
        monkeypatch.setattr(
            patrol_module,
            "credential_health",
            lambda recent_observations: {"ths": _report("ths", recent_observations)},
        )
        monkeypatch.setattr(notifications, "_NOTIFIER", notifications.KeyHealthNotifier(sink))

        result = await notifications.notify_scheduled_key_health()

        assert result["alerts_sent"] == 1
        assert result["observations"] == 1
        assert result["unchanged"] == 0
        assert result["last_observed_at"] == {"ths": NOW.isoformat()}
        sources = result["sources"]
        assert isinstance(sources, dict)
        ths = sources["ths"]
        assert isinstance(ths, dict)
        assert ths["level"] == "alert"
        assert sent[0]["notification_type"] == "key_health_alert"


class TestChannelDispatch:
    """_dispatch_notification decides per channel and never leaks a secret."""

    async def test_websocket_is_broadcast_once_and_email_is_complete_without_recipients(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub = RecordingWebSocketManager()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        _settings(monkeypatch, key_health_notification_emails=[], smtp_host=None, smtp_user=None)

        event = _event()
        delivered = await notifications._dispatch_notification(event, BOTH)

        assert delivered == BOTH
        assert hub.payloads == [{"type": "task_notification", "data": event}]

    async def test_a_channel_already_sent_is_not_touched_again(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub = RecordingWebSocketManager()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        _settings(monkeypatch, key_health_notification_emails=[])

        delivered = await notifications._dispatch_notification(_event(), EMAIL_ONLY)

        assert delivered == EMAIL_ONLY
        assert hub.payloads == []

    async def test_websocket_failure_is_logged_and_leaves_the_event_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_records: list[str],
    ) -> None:
        hub = RecordingWebSocketManager(fail_with=ConnectionError("no subscribers"))
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        _settings(monkeypatch, key_health_notification_emails=[])

        delivered = await notifications._dispatch_notification(_event(), BOTH)

        assert delivered == EMAIL_ONLY
        assert hub.payloads == []
        assert log_records == ["key health WebSocket notification failed: ConnectionError"]

    async def test_configured_recipients_without_an_smtp_transport_stay_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_records: list[str],
    ) -> None:
        hub = RecordingWebSocketManager()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        _settings(
            monkeypatch,
            key_health_notification_emails=["ops@example.invalid"],
            smtp_host=None,
            smtp_user=None,
        )

        delivered = await notifications._dispatch_notification(_event(), BOTH)

        assert delivered == WS_ONLY
        assert log_records == ["key health email pending: SMTP transport is not configured"]

    async def test_email_is_composed_for_every_configured_recipient(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub = RecordingWebSocketManager()
        sender = RecordingSmtpSend()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        monkeypatch.setattr("opendata.services.notification_service._smtp_send", sender)
        settings = _settings(
            monkeypatch,
            key_health_notification_emails=["ops@example.invalid", "duty@example.invalid"],
            smtp_host="smtp.example.invalid",
            smtp_port=465,
            smtp_user="bot@example.invalid",
            smtp_password="smtp-secret",
            emails_from_email="alerts@example.invalid",
        )

        event = _event("key_health_observation_expired", "fred")
        delivered = await notifications._dispatch_notification(event, BOTH)

        assert delivered == BOTH
        assert len(sender.calls) == 1
        assert len(hub.payloads) == 1
        call = sender.calls[0]
        assert call["host"] == settings.smtp_host
        assert call["port"] == 465
        assert call["user"] == "bot@example.invalid"
        assert call["password"] == "smtp-secret"
        message = _message(call)
        assert message["From"] == "alerts@example.invalid"
        assert message["To"] == "ops@example.invalid, duty@example.invalid"
        assert message["Subject"] == "[opendata] key_health_observation_expired: fred"
        body = _body_text(message)
        assert json.loads(body) == event
        assert "smtp-secret" not in body

    async def test_from_header_falls_back_to_the_smtp_user(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub = RecordingWebSocketManager()
        sender = RecordingSmtpSend()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        monkeypatch.setattr("opendata.services.notification_service._smtp_send", sender)
        _settings(
            monkeypatch,
            key_health_notification_emails=["ops@example.invalid"],
            smtp_host="smtp.example.invalid",
            smtp_user="bot@example.invalid",
            emails_from_email=None,
        )

        delivered = await notifications._dispatch_notification(_event(), EMAIL_ONLY)

        assert delivered == EMAIL_ONLY
        assert hub.payloads == []
        assert _message(sender.calls[0])["From"] == "bot@example.invalid"

    async def test_a_failing_transport_leaves_only_email_pending(
        self,
        monkeypatch: pytest.MonkeyPatch,
        log_records: list[str],
    ) -> None:
        hub = RecordingWebSocketManager()
        monkeypatch.setattr(
            "opendata.services.notification_service._smtp_send",
            RecordingSmtpSend(fail_with=OSError("connection refused")),
        )
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        _settings(
            monkeypatch,
            key_health_notification_emails=["ops@example.invalid"],
            smtp_host="smtp.example.invalid",
            smtp_user="bot@example.invalid",
            smtp_password="smtp-secret",
        )

        delivered = await notifications._dispatch_notification(_event(), BOTH)

        assert delivered == WS_ONLY
        assert log_records == ["key health email notification failed: OSError"]

    async def test_email_recipients_are_ignored_when_the_channel_already_sent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub = RecordingWebSocketManager()
        sender = RecordingSmtpSend()
        monkeypatch.setattr("opendata.api.websocket.ws_manager", hub)
        monkeypatch.setattr("opendata.services.notification_service._smtp_send", sender)
        _settings(
            monkeypatch,
            key_health_notification_emails=["ops@example.invalid"],
            smtp_host="smtp.example.invalid",
            smtp_user="bot@example.invalid",
        )

        delivered = await notifications._dispatch_notification(_event(), WS_ONLY)

        assert delivered == WS_ONLY
        assert sender.calls == []
