"""Opt-in, scheduled patrol of explicitly prioritized provider capabilities.

The job reads domain priority from :mod:`opendata.data.domains`, probes only
verified legs with a configured patrol query, persists failed-run streaks in
``DATA_DIR``, and sends threshold transitions through the existing WebSocket
notification channel. Provider errors and credential material are never
copied into the durable state or returned report.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from opendata.pipeline.key_health import CLASS_LEVELS, CLASS_PATROL_GAP
from opendata.pipeline.templates import ScheduleTemplate, TemplateKind

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence

    from opendata.core.config import Settings
    from opendata.data.capability import Capability
    from opendata.data.domains import DomainSpec
    from opendata.data.registry import ProviderRegistry
    from opendata.pipeline.patrol import PatrolResult

_STATE_VERSION = 2
_MAX_PENDING_NOTIFICATIONS = 1000
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class _Transition:
    """One safe state transition ready for WebSocket delivery."""

    domain: str
    source: str
    failure_count: int
    failure_class: str | None
    observed_at: str


@dataclass(frozen=True)
class _PendingNotification:
    """One durable, credential-safe WebSocket notification awaiting delivery."""

    notification_id: str
    notification_type: str
    transition: _Transition
    threshold: int | None


@dataclass(frozen=True)
class _StoreUpdate:
    """Counts and pending outbox events produced by one durable store update."""

    pending_notifications: tuple[_PendingNotification, ...]
    failed_results: int
    skipped_results: int
    active_failures: int


def _configured_probe_keys() -> frozenset[tuple[str, str]]:
    """Return the provider/domain legs for which patrol queries exist."""
    from opendata.pipeline.patrol import PROBE_PARAMS, PROBE_RESOLVERS

    return frozenset(PROBE_PARAMS) | frozenset(PROBE_RESOLVERS)


class PatrolFailureStore:
    """Persist consecutive scheduled-patrol failures with a local file lock.

    Args:
        path: JSON state path. Its parent is created as needed. A sibling lock
            file serializes readers and writers across processes on one host.
    """

    def __init__(self, path: Path) -> None:
        """Bind the durable state to ``path`` without reading it yet."""
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")

    def apply(
        self,
        results: Sequence[PatrolResult],
        *,
        threshold: int,
        now: datetime | None = None,
    ) -> _StoreUpdate:
        """Apply one run, alert on threshold crossings, and clear exact passes.

        Only verified, classified failed results increase a streak. A probe
        gap, unverified result, missing result, or otherwise unknown state is
        left unchanged. A successful result clears only its source/domain key.

        Args:
            results: Results returned by the existing patrol implementation.
            threshold: Positive count required for an alert transition.
            now: Optional aware timestamp for deterministic tests.

        Returns:
            Safe threshold and recovery transitions with run counters.

        Raises:
            ValueError: If the threshold is nonpositive or ``now`` is naive.
            RuntimeError: If the on-disk state is malformed or unsafe.
        """
        if threshold < 1:
            raise ValueError("patrol failure threshold must be positive")
        observed_at = _aware_utc(now or datetime.now(timezone.utc)).isoformat()
        grouped: dict[str, list[PatrolResult]] = defaultdict(list)
        skipped = 0
        for result in results:
            key = _state_key(result.source, result.domain)
            if key is None:
                skipped += 1
                continue
            grouped[key].append(result)

        new_notifications: list[_PendingNotification] = []
        failed = 0
        skipped += sum(max(len(group) - 1, 0) for group in grouped.values())

        with self._locked():
            entries, pending_notifications = self._read_state()
            changed = False
            for key, group in grouped.items():
                if len(group) != 1:
                    continue
                result = group[0]
                if not result.verified or result.failure_class == CLASS_PATROL_GAP:
                    skipped += 1
                    continue
                if result.ok:
                    previous = entries.pop(key, None)
                    if previous is not None:
                        domain, source = _split_state_key(key)
                        new_notifications.append(
                            _PendingNotification(
                                notification_id=str(uuid.uuid4()),
                                notification_type="provider_patrol_recovered",
                                transition=_Transition(
                                    domain=domain,
                                    source=source,
                                    failure_count=0,
                                    failure_class=None,
                                    observed_at=observed_at,
                                ),
                                threshold=None,
                            )
                        )
                        changed = True
                    continue
                if result.failure_class not in CLASS_LEVELS or not result.failure_class:
                    skipped += 1
                    continue

                failed += 1
                previous = entries.get(key)
                previous_count_value = previous.get("failure_count") if previous else 0
                if not isinstance(previous_count_value, int) or isinstance(
                    previous_count_value, bool
                ):
                    raise RuntimeError("provider patrol state contains an invalid failure count")
                previous_count = previous_count_value
                failure_count = previous_count + 1
                failure_class = result.failure_class
                entries[key] = {
                    "failure_count": failure_count,
                    "failure_class": failure_class,
                    "updated_at": observed_at,
                }
                changed = True
                if previous_count < threshold <= failure_count:
                    domain, source = _split_state_key(key)
                    new_notifications.append(
                        _PendingNotification(
                            notification_id=str(uuid.uuid4()),
                            notification_type="provider_patrol_threshold",
                            transition=_Transition(
                                domain=domain,
                                source=source,
                                failure_count=failure_count,
                                failure_class=failure_class,
                                observed_at=observed_at,
                            ),
                            threshold=threshold,
                        )
                    )
            if len(pending_notifications) + len(new_notifications) > _MAX_PENDING_NOTIFICATIONS:
                raise RuntimeError("provider patrol notification outbox is full")
            if changed or new_notifications:
                pending_notifications.extend(new_notifications)
                self._write_state(entries, pending_notifications)

        return _StoreUpdate(
            pending_notifications=tuple(pending_notifications),
            failed_results=failed,
            skipped_results=skipped,
            active_failures=len(entries),
        )

    def acknowledge_notification(self, notification_id: str) -> bool:
        """Remove an outbox event only after its sink reports success.

        A process crash after the sink succeeds but before this method commits
        can cause a retry. Delivery is therefore at least once, not exactly
        once.
        """
        with self._locked():
            entries, pending_notifications = self._read_state()
            remaining = [
                item for item in pending_notifications if item.notification_id != notification_id
            ]
            if len(remaining) == len(pending_notifications):
                return False
            self._write_state(entries, remaining)
            return True

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold a no-follow POSIX lock for one state read/modify/write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError("provider patrol lock cannot be opened safely") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_state(self) -> tuple[dict[str, dict[str, object]], list[_PendingNotification]]:
        """Read and validate the complete streak state and notification outbox."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return {}, []
        except OSError as exc:
            raise RuntimeError("provider patrol state cannot be opened safely") from exc
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("provider patrol state is unreadable") from exc
        if (
            not isinstance(document, dict)
            or document.get("version") not in {1, _STATE_VERSION}
            or set(document)
            != (
                {"version", "entries"}
                if document.get("version") == 1
                else {
                    "version",
                    "entries",
                    "pending_notifications",
                }
            )
            or not isinstance(document.get("entries"), dict)
        ):
            raise RuntimeError("provider patrol state has an unsupported shape")
        entries: dict[str, dict[str, object]] = {}
        for key, raw_entry in document["entries"].items():
            if (
                not isinstance(key, str)
                or _split_state_key_or_none(key) is None
                or not isinstance(raw_entry, dict)
                or set(raw_entry) != {"failure_count", "failure_class", "updated_at"}
                or type(raw_entry.get("failure_count")) is not int
                or raw_entry["failure_count"] < 1
                or raw_entry.get("failure_class") not in CLASS_LEVELS
                or not isinstance(raw_entry.get("updated_at"), str)
            ):
                raise RuntimeError("provider patrol state contains an invalid entry")
            entries[key] = {
                "failure_count": raw_entry["failure_count"],
                "failure_class": raw_entry["failure_class"],
                "updated_at": raw_entry["updated_at"],
            }
        raw_pending = document.get("pending_notifications", [])
        if not isinstance(raw_pending, list) or len(raw_pending) > _MAX_PENDING_NOTIFICATIONS:
            raise RuntimeError("provider patrol state contains an invalid notification outbox")
        pending_notifications = [_read_pending_notification(item) for item in raw_pending]
        if len({item.notification_id for item in pending_notifications}) != len(
            pending_notifications
        ):
            raise RuntimeError("provider patrol state contains duplicate notification ids")
        return entries, pending_notifications

    def _write_state(
        self,
        entries: Mapping[str, Mapping[str, object]],
        pending_notifications: Sequence[_PendingNotification],
    ) -> None:
        """Atomically replace streak state and outbox and fsync its folder."""
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".provider-patrol-", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": _STATE_VERSION,
                        "entries": entries,
                        "pending_notifications": [
                            _pending_notification_json(item) for item in pending_notifications
                        ],
                    },
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(self.path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)


async def execute_scheduled_patrol(
    template: ScheduleTemplate,
    *,
    settings_override: Settings | None = None,
    registry: ProviderRegistry | None = None,
    domains: Mapping[str, DomainSpec] | None = None,
    patrol_runner: Callable[..., Awaitable[Sequence[PatrolResult]]] | None = None,
    notification_sink: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    failure_store: PatrolFailureStore | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run the configured P0 provider patrol and report safe state transitions.

    The disabled path returns before resolving providers or calling the patrol.
    Enabled runs use the full registry for probe metadata lookups while passing
    only eligible, verified P0 legs to the active patrol loop.

    Args:
        template: Registered ``scheduled_patrol`` schedule template.
        settings_override: Optional settings object for isolated tests.
        registry: Provider registry override; defaults to the process registry.
        domains: Domain metadata override; defaults to ``domains.yaml``.
        patrol_runner: Offline-test override receiving the registry and selected
            capabilities. Production uses :func:`opendata.pipeline.patrol.patrol`.
        notification_sink: Optional async WebSocket-event sink for tests.
        failure_store: Durable streak store override; defaults below ``DATA_DIR``.
        now: Optional aware timestamp for deterministic tests.

    Returns:
        A JSON-safe report with selected, failed, skipped and alert counts.

    Raises:
        ValueError: If the template or timestamp is invalid.
        RuntimeError: If domain metadata or durable state is invalid.
    """
    _validate_template(template)
    app_settings = settings_override or _settings()
    store = failure_store or PatrolFailureStore(
        app_settings.data_dir / "provider_patrol_failures.json"
    )
    sink = notification_sink or _websocket_notification_sink
    current = _aware_utc(now or datetime.now(timezone.utc))
    if not app_settings.patrol_enabled:
        logger.info("scheduled provider patrol disabled by PATROL_ENABLED")
        disabled_report: dict[str, object] = {
            "status": "disabled",
            "reason": "PATROL_ENABLED=false",
            "probed": 0,
            "alerts_broadcast": 0,
        }
        update = store.apply((), threshold=app_settings.patrol_failure_alert_threshold, now=current)
        sent_alerts, sent_recoveries, delivery_failures = await _deliver_pending_notifications(
            store, update.pending_notifications, sink
        )
        if sent_alerts or sent_recoveries or delivery_failures:
            disabled_report.update(
                {
                    "alerts_broadcast": sent_alerts,
                    "recoveries_broadcast": sent_recoveries,
                    "notification_failures": delivery_failures,
                }
            )
        return disabled_report

    from opendata.data.domains import load_domains
    from opendata.data.registry import get_registry
    from opendata.pipeline.key_health_notifications import record_patrol_observations
    from opendata.pipeline.patrol import patrol

    domain_specs = domains if domains is not None else load_domains()
    provider_registry = registry if registry is not None else get_registry()
    selected, selection_report = _select_p0_capabilities(provider_registry, domain_specs)
    base_report: dict[str, object] = {
        "template": template.name,
        "tier": "P0",
        **selection_report,
        "selected_capabilities": len(selected),
        "probed": 0,
        "failed": 0,
        "skipped": 0,
        "unknown": 0,
        "unreported_capabilities": [],
        "alerts_broadcast": 0,
        "recoveries_broadcast": 0,
        "metadata_dependency_provider_requests": 0,
        "results": [],
    }
    if not selected:
        base_report["status"] = "no_eligible_capabilities"
        update = store.apply((), threshold=app_settings.patrol_failure_alert_threshold, now=current)
        sent_alerts, sent_recoveries, delivery_failures = await _deliver_pending_notifications(
            store, update.pending_notifications, sink
        )
        base_report.update(
            {
                "alerts_broadcast": sent_alerts,
                "recoveries_broadcast": sent_recoveries,
                "notification_failures": delivery_failures,
                "active_failure_streaks": update.active_failures,
            }
        )
        return base_report

    if patrol_runner is None:
        results = await patrol(provider_registry, capabilities=selected)
    else:
        results = await patrol_runner(provider_registry, selected)
    recorded_observations = record_patrol_observations(results) if results else 0
    update = store.apply(
        results,
        threshold=app_settings.patrol_failure_alert_threshold,
        now=current,
    )
    sent_alerts, sent_recoveries, delivery_failures = await _deliver_pending_notifications(
        store, update.pending_notifications, sink
    )

    results_by_key = {(item.domain, item.source): item for item in results}
    selected_keys = {(item.domain, item.source) for item in selected}
    unreported = sorted(
        f"{source}/{domain}" for domain, source in selected_keys - set(results_by_key)
    )
    result_rows = [
        _result_summary(capability, results_by_key.get((capability.domain, capability.source)))
        for capability in selected
    ]
    metadata_dependencies = _metadata_dependency_summary(selected, results_by_key)
    base_report.update(
        {
            "status": "completed",
            "probed": len(results),
            "failed": update.failed_results,
            "skipped": update.skipped_results,
            "unknown": update.skipped_results + len(unreported),
            "unreported_capabilities": unreported,
            "alerts_broadcast": sent_alerts,
            "recoveries_broadcast": sent_recoveries,
            "notification_failures": delivery_failures,
            "active_failure_streaks": update.active_failures,
            "key_observations_active": recorded_observations,
            "metadata_dependency_provider_requests": metadata_dependencies[0],
            "metadata_dependency_legs": list(metadata_dependencies[1]),
            "results": result_rows,
        }
    )
    return base_report


async def _deliver_pending_notifications(
    store: PatrolFailureStore,
    pending_notifications: Sequence[_PendingNotification],
    sink: Callable[[dict[str, object]], Awaitable[None]],
) -> tuple[int, int, int]:
    """Deliver a FIFO outbox prefix, acknowledging only successful sink calls.

    A failed item blocks newer items so alert/recovery order stays stable. The
    failed item remains durable for the next scheduled run.
    """
    sent_alerts = 0
    sent_recoveries = 0
    delivery_failures = 0
    for pending in pending_notifications:
        transition = pending.transition
        event = _transition_event(
            pending.notification_type,
            transition,
            pending.threshold,
            notification_id=pending.notification_id,
        )
        try:
            await sink(event)
        except Exception as exc:  # the run report retains the delivery failure safely
            delivery_failures += 1
            logger.warning(
                "scheduled provider patrol notification failed type={}", type(exc).__name__
            )
            break
        store.acknowledge_notification(pending.notification_id)
        if event["notification_type"] == "provider_patrol_threshold":
            sent_alerts += 1
            logger.warning(
                "scheduled provider patrol threshold reached source={} domain={} count={}",
                transition.source,
                transition.domain,
                transition.failure_count,
            )
        else:
            sent_recoveries += 1
    return sent_alerts, sent_recoveries, delivery_failures


def _settings() -> Settings:
    """Load the application settings singleton lazily."""
    from opendata.core.config import settings

    return settings


def _validate_template(template: ScheduleTemplate) -> None:
    """Reject wrong job kinds and payloads before any local or provider work."""
    if template.kind is not TemplateKind.SCHEDULED_PATROL:
        raise ValueError("scheduled patrol requires a scheduled_patrol template")
    if dict(template.payload) != {"tier": "P0"}:
        raise ValueError("scheduled patrol template payload must be exactly {tier: P0}")


def _select_p0_capabilities(
    registry: ProviderRegistry,
    domains: Mapping[str, DomainSpec],
) -> tuple[tuple[Capability, ...], dict[str, object]]:
    """Select verified P0 legs with explicit, implemented probe queries."""
    probe_keys = _configured_probe_keys()
    p0_domains = {name for name, spec in domains.items() if spec.priority == "P0"}
    registered_domains = {capability.domain for capability in registry.capabilities()}
    unknown_domains = {
        capability.domain
        for capability in registry.capabilities()
        if capability.domain not in domains or domains[capability.domain].priority is None
    }
    selected: list[Capability] = []
    unprobeable: list[str] = []
    p0_registered_domains: set[str] = set()
    for capability in registry.capabilities():
        if capability.domain not in p0_domains:
            continue
        p0_registered_domains.add(capability.domain)
        key = (capability.domain, capability.source)
        if not capability.participates_in_auto():
            unprobeable.append(f"{capability.source}/{capability.domain}:unverified-or-disabled")
            continue
        if key not in probe_keys:
            unprobeable.append(f"{capability.source}/{capability.domain}:no-probe")
            continue
        selected.append(capability)
    missing_registry = sorted(p0_domains - registered_domains)
    p0_without_probe = sorted(
        domain
        for domain in p0_registered_domains
        if not any(capability.domain == domain for capability in selected)
    )
    return tuple(selected), {
        "unknown_priority_domains": sorted(unknown_domains),
        "p0_domains_without_registered_capability": missing_registry,
        "p0_domains_without_eligible_probe": p0_without_probe,
        "ineligible_capabilities": sorted(set(unprobeable)),
    }


def _state_key(source: str, domain: str) -> str | None:
    """Build a safe, stable file key from provider identifiers."""
    if not _SAFE_ID.fullmatch(source) or not _SAFE_ID.fullmatch(domain):
        return None
    return f"{source}/{domain}"


def _split_state_key(key: str) -> tuple[str, str]:
    """Split a validated ``source/domain`` key into display fields."""
    source, domain = key.split("/", maxsplit=1)
    return domain, source


def _split_state_key_or_none(key: str) -> tuple[str, str] | None:
    """Return parsed identifiers only for a valid persisted state key."""
    parts = key.split("/", maxsplit=1)
    if len(parts) != 2 or _state_key(parts[0], parts[1]) is None:
        return None
    return parts[1], parts[0]


def _aware_utc(value: datetime) -> datetime:
    """Validate and normalize one timezone-aware timestamp."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled patrol timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _transition_event(
    notification_type: str,
    transition: _Transition,
    threshold: int | None,
    *,
    notification_id: str,
) -> dict[str, object]:
    """Render a credential-safe threshold or recovery event."""
    event: dict[str, object] = {
        "notification_id": notification_id,
        "notification_type": notification_type,
        "source": transition.source,
        "domain": transition.domain,
        "consecutive_failures": transition.failure_count,
        "failure_class": transition.failure_class,
        "timestamp": transition.observed_at,
    }
    if threshold is not None:
        event["threshold"] = threshold
    return event


def _pending_notification_json(item: _PendingNotification) -> dict[str, object]:
    """Serialize only the allowlisted event metadata needed for retry."""
    return {
        "notification_id": item.notification_id,
        "notification_type": item.notification_type,
        "domain": item.transition.domain,
        "source": item.transition.source,
        "failure_count": item.transition.failure_count,
        "failure_class": item.transition.failure_class,
        "observed_at": item.transition.observed_at,
        "threshold": item.threshold,
    }


def _read_pending_notification(raw: object) -> _PendingNotification:
    """Validate one persisted outbox entry and rebuild its safe event."""
    expected_keys = {
        "notification_id",
        "notification_type",
        "domain",
        "source",
        "failure_count",
        "failure_class",
        "observed_at",
        "threshold",
    }
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise RuntimeError("provider patrol state contains an invalid notification")
    notification_id = raw.get("notification_id")
    try:
        parsed_id = uuid.UUID(str(notification_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("provider patrol state contains an invalid notification id") from exc
    if str(parsed_id) != notification_id:
        raise RuntimeError("provider patrol state contains a noncanonical notification id")
    notification_type = raw.get("notification_type")
    domain = raw.get("domain")
    source = raw.get("source")
    failure_count = raw.get("failure_count")
    failure_class = raw.get("failure_class")
    observed_at = raw.get("observed_at")
    threshold = raw.get("threshold")
    if (
        not isinstance(notification_type, str)
        or notification_type not in {"provider_patrol_threshold", "provider_patrol_recovered"}
        or not isinstance(domain, str)
        or not isinstance(source, str)
        or _state_key(source, domain) is None
        or type(failure_count) is not int
        or failure_count < 0
        or (
            failure_class is not None
            and (not isinstance(failure_class, str) or failure_class not in CLASS_LEVELS)
        )
        or not isinstance(observed_at, str)
        or (threshold is not None and (type(threshold) is not int or threshold < 1))
    ):
        raise RuntimeError("provider patrol state contains an invalid notification")
    if notification_type == "provider_patrol_threshold":
        if failure_count < 1 or failure_class is None or threshold is None:
            raise RuntimeError("provider patrol state contains an invalid threshold event")
    elif failure_count != 0 or failure_class is not None or threshold is not None:
        raise RuntimeError("provider patrol state contains an invalid recovery event")
    return _PendingNotification(
        notification_id=str(notification_id),
        notification_type=notification_type,
        transition=_Transition(
            domain=domain,
            source=source,
            failure_count=failure_count,
            failure_class=failure_class,
            observed_at=observed_at,
        ),
        threshold=threshold,
    )


async def _websocket_notification_sink(event: dict[str, object]) -> None:
    """Publish a safe provider-patrol event on the existing WS channel."""
    from opendata.api.websocket import ws_manager

    await ws_manager.broadcast({"type": "task_notification", "data": event})


def _result_summary(
    capability: Capability,
    result: PatrolResult | None,
) -> dict[str, object]:
    """Summarize one selected leg without its provider error body."""
    if result is None:
        return {
            "domain": capability.domain,
            "source": capability.source,
            "status": "unknown",
            "failure_class": None,
        }
    failure_class = result.failure_class if result.failure_class in CLASS_LEVELS else None
    return {
        "domain": capability.domain,
        "source": capability.source,
        "status": (
            "healthy"
            if result.ok
            else "failed"
            if failure_class is not None and failure_class != CLASS_PATROL_GAP
            else "unknown"
        ),
        "failure_class": failure_class,
        "attempts": result.attempts,
        "rows": result.rows,
        "latency_ms": round(result.latency_ms, 2),
    }


def _metadata_dependency_summary(
    selected: Sequence[Capability],
    results_by_key: Mapping[tuple[str, str], PatrolResult],
) -> tuple[int, tuple[str, ...]]:
    """Count rolling-probe catalog request attempts and name their provider legs."""
    from opendata.pipeline.patrol import PROBE_RESOLVERS

    dependency_specs = {
        ("option_daily", "ths"): ("ths/instrument",),
    }
    requests = 0
    dependencies: set[str] = set()
    for capability in selected:
        key = (capability.domain, capability.source)
        dependency_legs = dependency_specs.get(key, ())
        if not dependency_legs or key not in PROBE_RESOLVERS:
            continue
        result = results_by_key.get(key)
        if result is not None:
            requests += result.attempts
            dependencies.update(dependency_legs)
    return requests, tuple(sorted(dependencies))


__all__ = ["PatrolFailureStore", "execute_scheduled_patrol"]
