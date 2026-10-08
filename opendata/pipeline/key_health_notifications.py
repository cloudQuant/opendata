"""Passive, de-duplicated notifications for the credential health plane.

Manual source patrols feed this process-local store with classified metadata
only. The scheduled check reads recent observations and issuer-supplied expiry
dates; it never starts provider requests of its own.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from loguru import logger

from opendata.pipeline.key_health import (
    CLASS_LEVELS,
    CLASS_PATROL_GAP,
    CLASS_UNCLASSIFIED,
    LEVEL_ALERT,
    LEVEL_WARN,
    FailureObservation,
    KeyReport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opendata.pipeline.patrol import PatrolResult

NotificationSink = Callable[[dict[str, object], frozenset[str]], Awaitable[frozenset[str]]]
_KEY_SOURCES = frozenset({"ths", "fred"})
_STATUS_ATTRIBUTION = re.compile(r"\bstatus=(401|403|429)\b")


@dataclass(frozen=True)
class ObservationSnapshot:
    """Recent classified key failures and their newest observation times."""

    observations: tuple[FailureObservation, ...]
    last_observed_at: dict[str, str]
    recovered_sources: frozenset[str]
    expired_sources: frozenset[str]


@dataclass(frozen=True)
class _StoredObservation:
    """One sanitized failure, keyed by source and probed domain."""

    observation: FailureObservation
    observed_at: datetime


class RecentObservationStore:
    """Keep only current, classified observations for key-bearing sources."""

    def __init__(self) -> None:
        """Initialize an empty process-local observation store."""
        self._by_capability: dict[tuple[str, str], _StoredObservation] = {}
        self._expired_capabilities: set[tuple[str, str]] = set()
        self._recovered_sources: set[str] = set()
        self._expired_sources: set[str] = set()

    def record(
        self,
        results: Sequence[PatrolResult],
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> int:
        """Apply one patrol result set and clear only capabilities that passed.

        Args:
            results: Results from one completed manual patrol.
            ttl: Maximum age of an observation retained for the scheduler.
            now: Timestamp override for deterministic tests.

        Returns:
            Number of active stored failure observations.
        """
        if ttl <= timedelta(0):
            raise ValueError("observation TTL must be positive")
        observed_at = _utc(now)
        self._prune(ttl=ttl, now=observed_at)
        for result in results:
            if result.source not in _KEY_SOURCES or not result.verified:
                continue
            key = (result.source, result.domain)
            if result.failure_class is None:
                prior = self._by_capability.pop(key, None)
                was_expired = key in self._expired_capabilities
                if prior is not None or was_expired:
                    self._recovered_sources.add(result.source)
                self._expired_capabilities.discard(key)
                if not any(source == result.source for source, _ in self._expired_capabilities):
                    self._expired_sources.discard(result.source)
                continue
            if result.failure_class == CLASS_PATROL_GAP:
                continue
            self._recovered_sources.discard(result.source)
            self._expired_capabilities.discard(key)
            if not any(source == result.source for source, _ in self._expired_capabilities):
                self._expired_sources.discard(result.source)
            failure_class = (
                result.failure_class if result.failure_class in CLASS_LEVELS else CLASS_UNCLASSIFIED
            )
            self._by_capability[key] = _StoredObservation(
                observation=FailureObservation(
                    source=result.source,
                    failure_class=failure_class,
                    attribution=_safe_status_attribution(result.attribution),
                ),
                observed_at=observed_at,
            )
        return len(self._by_capability)

    def snapshot(
        self,
        *,
        ttl: timedelta,
        now: datetime | None = None,
    ) -> ObservationSnapshot:
        """Return current failures after expiring stale observations.

        Args:
            ttl: Maximum age of a stored observation.
            now: Timestamp override for deterministic tests.

        Returns:
            Sanitized observations and the latest active timestamp per source.
        """
        if ttl <= timedelta(0):
            raise ValueError("observation TTL must be positive")
        current = _utc(now)
        self._prune(ttl=ttl, now=current)
        ordered = sorted(self._by_capability.items())
        latest: dict[str, datetime] = {}
        for (source, _), stored in ordered:
            latest[source] = max(latest.get(source, stored.observed_at), stored.observed_at)
        active_sources = set(latest)
        recovered_sources = frozenset(self._recovered_sources - active_sources)
        expired_sources = frozenset(self._expired_sources - active_sources - recovered_sources)
        self._recovered_sources.clear()
        self._expired_sources.clear()
        return ObservationSnapshot(
            observations=tuple(stored.observation for _, stored in ordered),
            last_observed_at={
                source: stamp.isoformat() for source, stamp in sorted(latest.items())
            },
            recovered_sources=recovered_sources,
            expired_sources=expired_sources,
        )

    def clear(self) -> None:
        """Remove every stored observation; intended for isolated tests."""
        self._by_capability.clear()
        self._expired_capabilities.clear()
        self._recovered_sources.clear()
        self._expired_sources.clear()

    def _prune(self, *, ttl: timedelta, now: datetime) -> None:
        cutoff = now - ttl
        expired = [
            key for key, stored in self._by_capability.items() if stored.observed_at <= cutoff
        ]
        for key in expired:
            stored = self._by_capability.pop(key, None)
            if stored is not None:
                self._expired_sources.add(stored.observation.source)
                self._expired_capabilities.add(key)


class KeyHealthNotifier:
    """Send state changes once and emit a single recovery when an issue clears."""

    def __init__(self, sink: NotificationSink) -> None:
        """Create a notifier with an injectable asynchronous delivery sink.

        Args:
            sink: Event receiver; production uses the WebSocket/SMTP adapter.
        """
        self._sink = sink
        self._active: dict[str, tuple[object, ...]] = {}
        self._pending_clear: dict[str, str] = {}
        self._delivered: dict[tuple[object, ...], set[str]] = {}
        self._lock = asyncio.Lock()

    async def notify(
        self,
        reports: Mapping[str, KeyReport],
        *,
        last_observed_at: Mapping[str, str] | None = None,
        recovered_sources: frozenset[str] = frozenset(),
        expired_sources: frozenset[str] = frozenset(),
    ) -> dict[str, int]:
        """Deliver changed key alerts or recovery events.

        Args:
            reports: Graded report per source.
            last_observed_at: Optional source timestamps from recent patrols.
            recovered_sources: Sources cleared by an explicit successful probe.
            expired_sources: Sources whose stored evidence aged out without a pass.

        Returns:
            Counts of new alerts, recoveries, unchanged alerts and failures.
        """
        async with self._lock:
            timestamps = last_observed_at or {}
            alert_count = 0
            recovery_count = 0
            expired_count = 0
            unchanged_count = 0
            failure_count = 0
            for source, report in sorted(reports.items()):
                fingerprint = _issue_fingerprint(report)
                active = self._active.get(source)
                if fingerprint is None:
                    if active is None:
                        continue
                    reason = self._pending_clear.get(source)
                    if source in recovered_sources:
                        reason = "recovered"
                    elif source in expired_sources:
                        reason = "observation-expired"
                    if reason is None:
                        continue
                    self._pending_clear[source] = reason
                    event_type = (
                        "key_health_recovered"
                        if reason == "recovered"
                        else "key_health_observation_expired"
                    )
                    state_key = (source, event_type, active)
                    if await self._deliver(
                        _notification_event(event_type, report, timestamps.get(source)),
                        state_key,
                    ):
                        self._active.pop(source, None)
                        self._pending_clear.pop(source, None)
                        if reason == "recovered":
                            recovery_count += 1
                        else:
                            expired_count += 1
                    else:
                        failure_count += 1
                    continue
                self._pending_clear.pop(source, None)
                if active == fingerprint:
                    unchanged_count += 1
                    continue
                event = _notification_event("key_health_alert", report, timestamps.get(source))
                state_key = (source, "key_health_alert", fingerprint)
                if await self._deliver(event, state_key):
                    self._discard_other_delivery_states(source, state_key)
                    self._active[source] = fingerprint
                    alert_count += 1
                else:
                    failure_count += 1
            return {
                "alerts_sent": alert_count,
                "recoveries_sent": recovery_count,
                "observations_expired": expired_count,
                "unchanged": unchanged_count,
                "delivery_failures": failure_count,
            }

    async def _deliver(self, event: dict[str, object], state_key: tuple[object, ...]) -> bool:
        """Send only channels still pending for this source-state transition."""
        sent = self._delivered.setdefault(state_key, set())
        pending = frozenset({"websocket", "email"} - sent)
        try:
            sent.update(await self._sink(event, pending))
        except Exception as exc:
            logger.warning(
                f"key health notification failed for {event['source']}: {type(exc).__name__}"
            )
            return False
        if {"websocket", "email"}.issubset(sent):
            self._delivered.pop(state_key, None)
            return True
        logger.warning(
            f"key health notification channels pending for {event['source']}: "
            f"{', '.join(sorted({'websocket', 'email'} - sent))}"
        )
        return False

    def _discard_other_delivery_states(self, source: str, keep: tuple[object, ...]) -> None:
        """Forget superseded partial deliveries for this source."""
        stale = [key for key in self._delivered if key[0] == source and key != keep]
        for key in stale:
            self._delivered.pop(key, None)


def record_patrol_observations(results: Sequence[PatrolResult]) -> int:
    """Record sanitized failures and successes from one manual source patrol.

    Args:
        results: Completed patrol results; raw error text is never copied.

    Returns:
        The count of active recent failure observations.
    """
    from opendata.core.config import settings

    return _OBSERVATIONS.record(
        results,
        ttl=timedelta(hours=settings.key_health_observation_ttl_hours),
    )


def recent_patrol_observations(*, now: datetime | None = None) -> ObservationSnapshot:
    """Read current sanitized failures for a scheduled passive health check.

    Args:
        now: Timestamp override for deterministic tests.

    Returns:
        Recent classified failures; empty means no current observation, not pass.
    """
    from opendata.core.config import settings

    return _OBSERVATIONS.snapshot(
        ttl=timedelta(hours=settings.key_health_observation_ttl_hours), now=now
    )


async def notify_scheduled_key_health() -> dict[str, object]:
    """Grade current metadata and deliver changes without provider requests.

    Returns:
        Notification counters, observation age metadata and current key reports.
    """
    from opendata.pipeline.patrol import credential_health

    snapshot = recent_patrol_observations()
    reports = credential_health(recent_observations=snapshot.observations)
    notification_counts = await _NOTIFIER.notify(
        reports,
        last_observed_at=snapshot.last_observed_at,
        recovered_sources=snapshot.recovered_sources,
        expired_sources=snapshot.expired_sources,
    )
    return {
        **notification_counts,
        "observations": len(snapshot.observations),
        "last_observed_at": dict(snapshot.last_observed_at),
        "sources": {source: report.as_dict() for source, report in reports.items()},
    }


async def _dispatch_notification(
    event: dict[str, object], pending_channels: frozenset[str]
) -> frozenset[str]:
    """Send an event to pending channels and report successful delivery.

    Email is considered complete when no recipients are configured. A
    configured but unavailable or failed SMTP transport stays pending so a
    later scheduled run can retry it without resending a successful WebSocket
    notification.
    """
    from opendata.core.config import settings

    delivered: set[str] = set()
    if "websocket" in pending_channels:
        try:
            from opendata.api.websocket import ws_manager

            await ws_manager.broadcast({"type": "task_notification", "data": event})
            delivered.add("websocket")
        except Exception as exc:
            logger.warning(f"key health WebSocket notification failed: {type(exc).__name__}")

    recipients = settings.key_health_notification_emails
    if not recipients:
        delivered.add("email")
    elif "email" in pending_channels:
        if not settings.smtp_host or not settings.smtp_user:
            logger.warning("key health email pending: SMTP transport is not configured")
        else:
            message = MIMEMultipart()
            message["From"] = settings.emails_from_email or settings.smtp_user
            message["To"] = ", ".join(recipients)
            message["Subject"] = f"[opendata] {event['notification_type']}: {event['source']}"
            message.attach(
                MIMEText(json.dumps(event, ensure_ascii=False, sort_keys=True), "plain", "utf-8")
            )
            try:
                from opendata.services.notification_service import _smtp_send

                await asyncio.to_thread(
                    _smtp_send,
                    host=settings.smtp_host,
                    port=settings.smtp_port,
                    user=settings.smtp_user,
                    password=settings.smtp_password,
                    msg=message,
                )
                delivered.add("email")
            except Exception as exc:
                logger.warning(f"key health email notification failed: {type(exc).__name__}")
    return frozenset(delivered)


def _issue_fingerprint(report: KeyReport) -> tuple[object, ...] | None:
    """Build a stable issue key that ignores changing counters and age days."""
    classes = tuple(
        name for name, _ in report.classes if CLASS_LEVELS.get(name) in {LEVEL_ALERT, LEVEL_WARN}
    )
    expiry_issue = report.expiry_state in {"expired", "expiring-soon"}
    if not classes and not expiry_issue:
        return None
    return (
        report.level,
        report.configured,
        classes,
        report.expiry_state if expiry_issue else "unknown",
        report.expires_at if expiry_issue else None,
    )


def _notification_event(
    notification_type: str,
    report: KeyReport,
    last_observed_at: str | None,
) -> dict[str, object]:
    """Render a credential-safe event for the existing notification channels."""
    return {
        "notification_type": notification_type,
        "source": report.source,
        "level": report.level,
        "report": report.as_dict(),
        "last_observed_at": last_observed_at,
        "remaining_quota": None,
    }


def _safe_status_attribution(value: str | None) -> str | None:
    """Retain only the explicit auth/quota status from attribution metadata."""
    if not value:
        return None
    match = _STATUS_ATTRIBUTION.search(value)
    return f"status={match.group(1)}" if match else None


def _utc(value: datetime | None) -> datetime:
    """Normalize a supplied observation clock to timezone-aware UTC."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


_OBSERVATIONS = RecentObservationStore()
_NOTIFIER = KeyHealthNotifier(_dispatch_notification)


__all__ = [
    "KeyHealthNotifier",
    "ObservationSnapshot",
    "RecentObservationStore",
    "notify_scheduled_key_health",
    "recent_patrol_observations",
    "record_patrol_observations",
]
