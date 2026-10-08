"""Behavior tests for passive, sanitized key-health notifications."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from opendata.pipeline import key_health_notifications as notifications
from opendata.pipeline.key_health import (
    CLASS_CREDENTIAL_REJECTED,
    CLASS_QUOTA_EXHAUSTED,
    FailureObservation,
    KeyReport,
    build_report,
)
from opendata.pipeline.patrol import PatrolResult

if TYPE_CHECKING:
    from collections.abc import Iterator


AS_OF = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def _report(
    observations: tuple[FailureObservation, ...] = (),
    *,
    expires_at: date | None = None,
) -> KeyReport:
    """Create one deterministic THS report without reading application secrets."""
    return build_report(
        "ths",
        required=True,
        configured=True,
        endpoint="fuyao.aicubes.cn",
        observations=observations,
        expires_at=expires_at,
        as_of=AS_OF,
        expiry_warning_days=14,
    )


def _result(
    *,
    failure_class: str | None,
    attribution: str | None,
    domain: str = "stock_daily",
    source: str = "ths",
) -> PatrolResult:
    """Build a classified manual-patrol result without a provider call."""
    return PatrolResult(
        domain=domain,
        source=source,
        ok=failure_class is None,
        error="private provider message must not be stored",
        latency_ms=1.0,
        verified=True,
        failure_class=failure_class,
        attribution=attribution,
    )


@pytest.fixture
def fresh_store() -> Iterator[notifications.RecentObservationStore]:
    """Yield an isolated in-memory store for TTL and capability tests."""
    store = notifications.RecentObservationStore()
    yield store
    store.clear()


class TestRecentObservationStore:
    """The scheduled report consumes only recent, classified metadata."""

    def test_stores_auth_and_quota_status_without_raw_errors(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [
                _result(
                    failure_class=CLASS_CREDENTIAL_REJECTED,
                    attribution="status=401 https://example.invalid?key=secret",
                ),
                _result(
                    failure_class=CLASS_QUOTA_EXHAUSTED,
                    attribution="status=429 https://example.invalid?key=secret",
                    domain="index_daily",
                ),
            ],
            ttl=timedelta(hours=1),
            now=NOW,
        )

        snapshot = fresh_store.snapshot(ttl=timedelta(hours=1), now=NOW)

        assert {(item.failure_class, item.attribution) for item in snapshot.observations} == {
            (CLASS_CREDENTIAL_REJECTED, "status=401"),
            (CLASS_QUOTA_EXHAUSTED, "status=429"),
        }
        assert snapshot.last_observed_at == {"ths": NOW.isoformat()}
        assert all("secret" not in str(item) for item in snapshot.observations)

    def test_ttl_expiry_is_unknown_and_is_not_a_confirmed_recovery(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [_result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401")],
            ttl=timedelta(hours=1),
            now=NOW,
        )

        expired = fresh_store.snapshot(
            ttl=timedelta(hours=1),
            now=NOW + timedelta(hours=1, seconds=1),
        )

        assert expired.observations == ()
        assert expired.expired_sources == frozenset({"ths"})
        assert expired.recovered_sources == frozenset()

    def test_success_clears_only_the_exact_capability_and_marks_observed_recovery(
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
            ttl=timedelta(hours=1),
            now=NOW,
        )

        fresh_store.record(
            [_result(failure_class=None, attribution=None)],
            ttl=timedelta(hours=1),
            now=NOW + timedelta(minutes=1),
        )
        still_active = fresh_store.snapshot(ttl=timedelta(hours=1), now=NOW + timedelta(minutes=1))
        assert len(still_active.observations) == 1
        assert still_active.recovered_sources == frozenset()

        fresh_store.record(
            [
                _result(
                    failure_class=None,
                    attribution=None,
                    domain="index_daily",
                )
            ],
            ttl=timedelta(hours=1),
            now=NOW + timedelta(minutes=2),
        )
        recovered = fresh_store.snapshot(ttl=timedelta(hours=1), now=NOW + timedelta(minutes=2))
        assert recovered.observations == ()
        assert recovered.recovered_sources == frozenset({"ths"})

    def test_success_on_an_unrelated_capability_does_not_recover_an_expired_observation(
        self,
        fresh_store: notifications.RecentObservationStore,
    ) -> None:
        fresh_store.record(
            [_result(failure_class=CLASS_CREDENTIAL_REJECTED, attribution="status=401")],
            ttl=timedelta(hours=1),
            now=NOW,
        )

        fresh_store.record(
            [
                _result(
                    failure_class=None,
                    attribution=None,
                    domain="index_daily",
                )
            ],
            ttl=timedelta(hours=1),
            now=NOW + timedelta(hours=1, seconds=1),
        )
        snapshot = fresh_store.snapshot(
            ttl=timedelta(hours=1),
            now=NOW + timedelta(hours=1, seconds=1),
        )

        assert snapshot.expired_sources == frozenset({"ths"})
        assert snapshot.recovered_sources == frozenset()


class TestKeyHealthNotifier:
    """Transitions are de-duplicated and retries are channel-specific."""

    async def test_unchanged_alert_is_not_sent_twice_and_quota_stays_unknown(self) -> None:
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            sent.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        observation = FailureObservation("ths", CLASS_QUOTA_EXHAUSTED, "status=429")
        report = _report((observation, observation))

        first = await notifier.notify({"ths": report})
        second = await notifier.notify({"ths": report})

        assert first["alerts_sent"] == 1
        assert second["unchanged"] == 1
        assert len(sent) == 1
        rendered = sent[0]["report"]
        assert isinstance(rendered, dict)
        assert rendered["remaining_quota"] is None
        assert rendered["expiry_state"] == "unknown"

    async def test_ttl_expiry_event_is_not_labeled_as_recovery(self) -> None:
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            sent.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        active = _report((FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401"),))
        await notifier.notify({"ths": active})

        counts = await notifier.notify(
            {"ths": _report()},
            expired_sources=frozenset({"ths"}),
        )

        assert counts["observations_expired"] == 1
        assert counts["recoveries_sent"] == 0
        assert sent[-1]["notification_type"] == "key_health_observation_expired"
        assert all(item["notification_type"] != "key_health_recovered" for item in sent)

    async def test_successful_probe_emits_one_confirmed_recovery(self) -> None:
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            sent.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        active = _report((FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=403"),))
        await notifier.notify({"ths": active})
        first = await notifier.notify({"ths": _report()}, recovered_sources=frozenset({"ths"}))
        repeated = await notifier.notify({"ths": _report()})

        assert first["recoveries_sent"] == 1
        assert repeated["recoveries_sent"] == 0
        assert sent[-1]["notification_type"] == "key_health_recovered"

    async def test_successful_channel_is_not_resent_while_email_retries(self) -> None:
        attempts: list[frozenset[str]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            attempts.append(pending)
            if len(attempts) == 1:
                return frozenset({"websocket"})
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        report = _report((FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401"),))

        first = await notifier.notify({"ths": report})
        second = await notifier.notify({"ths": report})

        assert first["delivery_failures"] == 1
        assert second["alerts_sent"] == 1
        assert attempts == [frozenset({"websocket", "email"}), frozenset({"email"})]

    async def test_concurrent_identical_alerts_are_serialized(self) -> None:
        sends = 0

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            nonlocal sends
            sends += 1
            await asyncio.sleep(0)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        report = _report((FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401"),))

        await asyncio.gather(
            notifier.notify({"ths": report}),
            notifier.notify({"ths": report}),
        )

        assert sends == 1

    def test_expiry_metadata_is_reported_and_triggers_warning(self) -> None:
        soon = _report(expires_at=date(2026, 10, 3))
        expired = _report(expires_at=date(2026, 9, 29))
        unknown = _report()

        assert soon.expiry_state == "expiring-soon"
        assert soon.days_until_expiry == 3
        assert expired.expiry_state == "expired"
        assert expired.level == "alert"
        assert soon.level == "warn"
        assert unknown.expiry_state == "unknown"
        assert unknown.remaining_quota is None

    async def test_expiry_severity_is_carried_into_notification(self) -> None:
        sent: list[dict[str, object]] = []

        async def sink(event: dict[str, object], pending: frozenset[str]) -> frozenset[str]:
            sent.append(event)
            return pending

        notifier = notifications.KeyHealthNotifier(sink)
        await notifier.notify({"ths": _report(expires_at=date(2026, 9, 29))})

        assert sent[0]["level"] == "alert"
        report = sent[0]["report"]
        assert isinstance(report, dict)
        assert report["expiry_state"] == "expired"
        assert report["remaining_quota"] is None

    def test_expiry_settings_parse_iso_dates_and_reject_nonpositive_windows(self) -> None:
        from pydantic import ValidationError

        from opendata.core.config import Settings

        configured = Settings(
            _env_file=None,
            fuyao_api_key_expires_at="2026-10-03",
            fred_api_key_expires_at="2026-11-01T00:00:00Z",
        )
        assert configured.fuyao_api_key_expires_at == date(2026, 10, 3)
        assert configured.fred_api_key_expires_at == date(2026, 11, 1)
        with pytest.raises(ValidationError, match="key_expiry_warning_days"):
            Settings(_env_file=None, key_expiry_warning_days=0)

    async def test_scheduled_check_consumes_observations_without_running_patrol(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from opendata.pipeline import patrol as patrol_module

        observation = FailureObservation("ths", CLASS_CREDENTIAL_REJECTED, "status=401")
        snapshot = notifications.ObservationSnapshot(
            observations=(observation,),
            last_observed_at={"ths": NOW.isoformat()},
            recovered_sources=frozenset(),
            expired_sources=frozenset(),
        )
        seen: dict[str, object] = {}
        reports = {"ths": _report((observation,))}

        def forbidden_patrol() -> None:
            pytest.fail("scheduled key health must not start provider patrols")

        async def capture_notify(
            scheduled_reports: dict[str, KeyReport],
            *,
            last_observed_at: dict[str, str],
            recovered_sources: frozenset[str],
            expired_sources: frozenset[str],
        ) -> dict[str, int]:
            seen.update(
                {
                    "reports": scheduled_reports,
                    "last_observed_at": last_observed_at,
                    "recovered_sources": recovered_sources,
                    "expired_sources": expired_sources,
                }
            )
            return {"alerts_sent": 0, "recoveries_sent": 0}

        monkeypatch.setattr(patrol_module, "patrol", forbidden_patrol)
        monkeypatch.setattr(notifications, "recent_patrol_observations", lambda: snapshot)
        monkeypatch.setattr(
            patrol_module,
            "credential_health",
            lambda recent_observations: reports,
        )
        monkeypatch.setattr(notifications._NOTIFIER, "notify", capture_notify)

        result = await notifications.notify_scheduled_key_health()

        assert result["observations"] == 1
        assert seen["reports"] == reports
        assert seen["last_observed_at"] == {"ths": NOW.isoformat()}
