"""C85 unit coverage for :mod:`opendata.pipeline.scheduled_patrol`.

The durable streak store is driven entirely through its public API against a
``tmp_path`` file, with corrupt documents written by hand so the fail-closed
state validation actually runs. The job itself runs with an injected registry,
settings, failure store and notification sink: no provider is constructed, no
network is reached, and the production ``patrol`` entrypoint is exercised
through a recorded call rather than a live probe. Assertions read the rendered
event payloads, because a mis-shaped or mis-sorted alert is the bug here.
"""

from __future__ import annotations

import importlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

import opendata.pipeline.scheduled_patrol as scheduled_patrol
from opendata.core.config import Settings
from opendata.data.capability import Capability
from opendata.data.domains import DomainSpec
from opendata.data.registry import ProviderRegistry
from opendata.pipeline.key_health import CLASS_PATROL_GAP, CLASS_SOURCE_DEGRADED
from opendata.pipeline.patrol import PatrolResult
from opendata.pipeline.scheduled_patrol import PatrolFailureStore, execute_scheduled_patrol
from opendata.pipeline.templates import PIPELINE_TEMPLATES, ScheduleTemplate, TemplateKind

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_NAIVE = datetime(2026, 9, 30, 12, 0)
_STATE_NAME = "provider_patrol_failures.json"
_THRESHOLD_ID = "ab12cd34-5678-49ab-8cde-f0123456789a"
_RECOVERY_ID = "22222222-3333-4444-8555-666666666666"


@pytest.fixture(autouse=True)
def _isolate_key_observations(monkeypatch):
    """Keep scheduled patrol observations local to each test."""
    from opendata.pipeline import key_health_notifications

    monkeypatch.setattr(
        key_health_notifications, "_OBSERVATIONS", key_health_notifications.RecentObservationStore()
    )


class _Registry:
    """Minimal registry view: proves which legs the job hands to the patrol."""

    def __init__(self, capabilities):
        self._capabilities = list(capabilities)

    def capabilities(self):
        return list(self._capabilities)


class _Sink:
    """Async WebSocket-sink double recording every delivered event payload."""

    def __init__(self, *, fail_for=()):
        self.events = []
        self._fail_for = set(fail_for)

    async def __call__(self, event):
        self.events.append(event)
        if event["notification_type"] in self._fail_for:
            raise RuntimeError("websocket channel closed")


def _capability(domain, source="ths", *, verified=True):
    return Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="cn",
        source=source,
        verified=verified,
    )


def _result(
    domain,
    *,
    source="ths",
    ok=False,
    verified=True,
    failure_class=CLASS_SOURCE_DEGRADED,
    attempts=1,
    rows=None,
):
    return PatrolResult(
        domain=domain,
        source=source,
        ok=ok,
        error=None if ok else "CANARY-SECRET provider response",
        latency_ms=4.0,
        verified=verified,
        rows=rows if rows is not None else (1 if ok else None),
        attempts=attempts,
        failure_class=failure_class,
    )


def _p0_domains(*names):
    return {
        name: DomainSpec(
            display_name=name,
            rest_path=f"c85/{name}",
            contract="Bar",
            priority="P0",
        )
        for name in names
    }


def _settings(directory, *, enabled=True, threshold=2):
    return Settings(
        _env_file=None,
        app_env="testing",
        data_dir=directory,
        patrol_enabled=enabled,
        patrol_failure_alert_threshold=threshold,
    )


def _template():
    return next(
        template
        for template in PIPELINE_TEMPLATES
        if template.kind is TemplateKind.SCHEDULED_PATROL
    )


def _store(tmp_path: Path) -> PatrolFailureStore:
    return PatrolFailureStore(tmp_path / _STATE_NAME)


def _write_document(tmp_path: Path, document: object) -> Path:
    """Drop a raw state document, bypassing the store's own writer."""
    path = tmp_path / _STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _read_document(tmp_path: Path) -> dict:
    return json.loads((tmp_path / _STATE_NAME).read_text(encoding="utf-8"))


def _pending(**overrides):
    """One canonical threshold outbox entry."""
    item = {
        "notification_id": _THRESHOLD_ID,
        "notification_type": "provider_patrol_threshold",
        "domain": "stock_daily",
        "source": "ths",
        "failure_count": 2,
        "failure_class": CLASS_SOURCE_DEGRADED,
        "observed_at": _NOW.isoformat(),
        "threshold": 2,
    }
    item.update(overrides)
    return item


def _recovery(**overrides):
    base = _pending()
    base.update(
        {
            "notification_id": _RECOVERY_ID,
            "notification_type": "provider_patrol_recovered",
            "failure_count": 0,
            "failure_class": None,
            "threshold": None,
        }
    )
    base.update(overrides)
    return base


def _v2(**overrides):
    document = {"version": 2, "entries": {}, "pending_notifications": []}
    document.update(overrides)
    return document


class TestDeferredTypeImports:
    def test_type_only_imports_resolve_when_evaluated_at_runtime(self):
        """A renamed module behind ``TYPE_CHECKING`` must not rot silently."""
        import typing

        original = typing.TYPE_CHECKING
        typing.TYPE_CHECKING = True
        try:
            reloaded = importlib.reload(scheduled_patrol)
            hints = typing.get_type_hints(reloaded.execute_scheduled_patrol)
            store_type = reloaded.PatrolFailureStore
        finally:
            typing.TYPE_CHECKING = original
            importlib.reload(scheduled_patrol)

        from collections.abc import Awaitable, Callable, Mapping, Sequence

        assert hints["settings_override"] == Settings | None
        assert hints["registry"] == ProviderRegistry | None
        assert hints["domains"] == Mapping[str, DomainSpec] | None
        assert hints["failure_store"] == store_type | None
        assert hints["patrol_runner"] == Callable[..., Awaitable[Sequence[PatrolResult]]] | None
        assert hints["notification_sink"] == Callable[[dict[str, object]], Awaitable[None]] | None
        assert hints["return"] == dict[str, object]


class TestTemplateGuard:
    @pytest.mark.parametrize(
        ("kind", "payload", "expected"),
        [
            (TemplateKind.SCHEDULED_PATROL, {"tier": "P1"}, "payload must be exactly"),
            (TemplateKind.SCHEDULED_PATROL, {}, "payload must be exactly"),
        ],
    )
    async def test_payload_other_than_p0_is_refused_before_any_work(
        self, tmp_path, kind, payload, expected
    ):
        template = ScheduleTemplate(name="c85_patrol", cron="0 8 * * *", kind=kind, payload=payload)

        with pytest.raises(ValueError, match=expected):
            await execute_scheduled_patrol(
                template,
                settings_override=_settings(tmp_path),
                failure_store=_store(tmp_path),
                notification_sink=_Sink(),
                now=_NOW,
            )

        assert not (tmp_path / _STATE_NAME).exists()

    async def test_foreign_job_kind_is_refused(self, tmp_path):
        foreign = next(
            template
            for template in PIPELINE_TEMPLATES
            if template.kind is not TemplateKind.SCHEDULED_PATROL
        )

        with pytest.raises(ValueError, match="requires a scheduled_patrol template"):
            await execute_scheduled_patrol(
                foreign,
                settings_override=_settings(tmp_path),
                failure_store=_store(tmp_path),
                notification_sink=_Sink(),
                now=_NOW,
            )

    async def test_naive_timestamp_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="timestamps must be timezone-aware"):
            await execute_scheduled_patrol(
                _template(),
                settings_override=_settings(tmp_path),
                failure_store=_store(tmp_path),
                notification_sink=_Sink(),
                now=_NAIVE,
            )


class TestStoreThresholdAndKeyGuards:
    @pytest.mark.parametrize("threshold", [0, -3])
    def test_nonpositive_threshold_is_refused(self, tmp_path, threshold):
        store = _store(tmp_path)

        with pytest.raises(ValueError, match="patrol failure threshold must be positive"):
            store.apply([_result("stock_daily")], threshold=threshold, now=_NOW)

        assert not store.path.exists()
        assert not store.lock_path.exists()

    def test_naive_timestamp_never_reaches_the_state_file(self, tmp_path):
        store = _store(tmp_path)

        with pytest.raises(ValueError, match="timestamps must be timezone-aware"):
            store.apply([_result("stock_daily")], threshold=2, now=_NAIVE)

        assert not store.path.exists()

    def test_unsafe_provider_identifiers_are_skipped_and_never_persisted(self, tmp_path):
        """A key that could escape the state file's ``source/domain`` shape is dropped."""
        store = _store(tmp_path)
        results = [
            _result("stock_daily", source="THS Bad"),
            _result("stock-daily!", source="ths"),
            _result("futures_daily", source=""),
        ]

        update = store.apply(results, threshold=2, now=_NOW)

        assert update.failed_results == 0
        assert update.skipped_results == 3
        assert update.active_failures == 0
        assert update.pending_notifications == ()
        assert not store.path.exists()

    def test_two_results_for_one_leg_are_ambiguous_and_leave_the_streak_alone(self, tmp_path):
        store = _store(tmp_path)
        first = _result("stock_daily")
        second = _result("stock_daily", failure_class=CLASS_PATROL_GAP)

        update = store.apply([first, second], threshold=1, now=_NOW)

        assert update.failed_results == 0
        assert update.active_failures == 0
        # one skip for the ambiguity itself; the duplicate group is then untouched
        assert update.skipped_results == 1
        assert not store.path.exists()

    def test_probe_gap_and_unverified_legs_are_never_counted_as_failures(self, tmp_path):
        store = _store(tmp_path)
        results = [
            _result("stock_daily", failure_class=CLASS_PATROL_GAP),
            _result("futures_daily", verified=False),
        ]

        update = store.apply(results, threshold=1, now=_NOW)

        assert update.failed_results == 0
        assert update.skipped_results == 2
        assert update.active_failures == 0

    def test_a_clean_pass_with_no_recorded_streak_stays_quiet(self, tmp_path):
        """Recovery alerts only fire for a streak that existed; a healthy leg is a no-op."""
        store = _store(tmp_path)

        update = store.apply([_result("futures_daily", ok=True)], threshold=1, now=_NOW)

        assert update.failed_results == 0
        assert update.skipped_results == 0
        assert update.active_failures == 0
        assert update.pending_notifications == ()
        assert not store.path.exists()

    def test_unclassified_and_blank_failure_classes_are_skipped(self, tmp_path):
        """A failed result with no health class cannot raise a classified alert."""
        store = _store(tmp_path)
        results = [
            _result("stock_daily", failure_class=None),
            _result("futures_daily", failure_class="invented-class"),
            _result("option_daily", failure_class=""),
        ]

        update = store.apply(results, threshold=1, now=_NOW)

        assert update.failed_results == 0
        assert update.skipped_results == 3
        assert update.active_failures == 0
        assert not store.path.exists()

    def test_offset_timestamps_are_normalised_to_utc_in_the_stored_entry(self, tmp_path):
        """The stored ``updated_at`` is UTC, whatever offset the caller passed."""
        store = _store(tmp_path)
        beijing = datetime(2026, 9, 30, 20, 0, tzinfo=timezone(timedelta(hours=8)))

        store.apply([_result("stock_daily")], threshold=2, now=beijing)

        entry = _read_document(tmp_path)["entries"]["ths/stock_daily"]
        assert entry["updated_at"] == "2026-09-30T12:00:00+00:00"
        assert entry["failure_count"] == 1
        assert entry["failure_class"] == CLASS_SOURCE_DEGRADED


class TestStoreCorruptState:
    def test_unreadable_json_fails_closed(self, tmp_path):
        store = _store(tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{not json at all", encoding="utf-8")

        with pytest.raises(RuntimeError, match="provider patrol state is unreadable"):
            store.apply((), threshold=2, now=_NOW)

    def test_symlinked_state_file_is_refused(self, tmp_path):
        """The no-follow open must reject a swapped-in state file."""
        store = _store(tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("/etc/hosts", store.path)

        with pytest.raises(RuntimeError, match="state cannot be opened safely"):
            store.apply((), threshold=2, now=_NOW)

    def test_directory_lock_file_is_refused(self, tmp_path):
        store = _store(tmp_path)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(store.lock_path)

        with pytest.raises(RuntimeError, match="lock cannot be opened safely"):
            store.apply((), threshold=2, now=_NOW)

    @pytest.mark.parametrize(
        "document",
        [
            {"version": 3, "entries": {}, "pending_notifications": []},
            {"version": 2, "entries": {}, "pending_notifications": [], "extra": 1},
            {"version": 2, "entries": {}},
            {"version": 1, "entries": {}, "pending_notifications": []},
            {"version": 1, "entries": []},
            [{"version": 2, "entries": {}, "pending_notifications": []}],
            _v2(entries="not-a-mapping"),
        ],
    )
    def test_unsupported_shapes_fail_closed(self, tmp_path, document):
        _write_document(tmp_path, document)
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="state has an unsupported shape"):
            store.apply((), threshold=2, now=_NOW)

    def test_version_one_state_is_readable_and_upgraded_on_write(self, tmp_path):
        """A pre-outbox v1 file stays valid and is rewritten as v2."""
        _write_document(
            tmp_path,
            {
                "version": 1,
                "entries": {
                    "ths/stock_daily": {
                        "failure_count": 1,
                        "failure_class": CLASS_SOURCE_DEGRADED,
                        "updated_at": _NOW.isoformat(),
                    }
                },
            },
        )
        store = _store(tmp_path)

        update = store.apply([_result("stock_daily")], threshold=2, now=_NOW)

        assert update.failed_results == 1
        assert update.active_failures == 1
        document = _read_document(tmp_path)
        assert document["version"] == 2
        assert document["entries"]["ths/stock_daily"]["failure_count"] == 2
        # the 1 -> 2 crossing is queued in the new outbox field on the way out
        [pending] = document["pending_notifications"]
        assert pending["notification_type"] == "provider_patrol_threshold"
        assert pending["failure_count"] == 2
        assert pending["threshold"] == 2
        assert uuid.UUID(pending["notification_id"])

    @pytest.mark.parametrize(
        "entry",
        [
            {"failure_count": 1, "failure_class": CLASS_SOURCE_DEGRADED},
            {"failure_count": 1, "failure_class": CLASS_SOURCE_DEGRADED, "updated_at": 17},
            {"failure_count": 0, "failure_class": CLASS_SOURCE_DEGRADED, "updated_at": ""},
            {"failure_count": True, "failure_class": CLASS_SOURCE_DEGRADED, "updated_at": ""},
            {"failure_count": 1.5, "failure_class": CLASS_SOURCE_DEGRADED, "updated_at": ""},
            {"failure_count": 1, "failure_class": "invented", "updated_at": ""},
            {"failure_count": 1, "failure_class": None, "updated_at": ""},
            "not-a-mapping",
        ],
    )
    def test_invalid_entries_fail_closed(self, tmp_path, entry):
        _write_document(tmp_path, _v2(entries={"ths/stock_daily": entry}))
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="state contains an invalid entry"):
            store.apply((), threshold=2, now=_NOW)

    @pytest.mark.parametrize(
        "key", ["no_slash", "ths/stock-daily", "Ths/stock_daily", "/stock_daily"]
    )
    def test_invalid_entry_keys_fail_closed(self, tmp_path, key):
        _write_document(
            tmp_path,
            _v2(
                entries={
                    key: {
                        "failure_count": 1,
                        "failure_class": CLASS_SOURCE_DEGRADED,
                        "updated_at": _NOW.isoformat(),
                    }
                }
            ),
        )
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="state contains an invalid entry"):
            store.apply((), threshold=2, now=_NOW)

    @pytest.mark.parametrize(
        "pending",
        [
            {"not": "a-list"},
            [_pending() for _ in range(1001)],
        ],
    )
    def test_invalid_outbox_container_fails_closed(self, tmp_path, pending):
        _write_document(tmp_path, _v2(pending_notifications=pending))
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="invalid notification outbox"):
            store.apply((), threshold=2, now=_NOW)

    def test_duplicate_notification_ids_fail_closed(self, tmp_path):
        _write_document(tmp_path, _v2(pending_notifications=[_pending(), _pending()]))
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match="duplicate notification ids"):
            store.apply((), threshold=2, now=_NOW)

    @pytest.mark.parametrize(
        "item, expected",
        [
            ({k: v for k, v in _pending().items() if k != "threshold"}, "invalid notification"),
            (_pending(notification_id="not-a-uuid"), "invalid notification id"),
            (_pending(notification_id=None), "invalid notification id"),
            (_pending(notification_id=_THRESHOLD_ID.upper()), "noncanonical notification id"),
            (_pending(notification_type="provider_patrol_mystery"), "invalid notification"),
            (_pending(source="THS Bad"), "invalid notification"),
            (_pending(failure_count=-1), "invalid notification"),
            (_pending(failure_class="invented"), "invalid notification"),
            (_pending(threshold=0), "invalid notification"),
            (_pending(failure_count=0), "invalid threshold event"),
            (_pending(failure_class=None), "invalid threshold event"),
            (_pending(threshold=None), "invalid threshold event"),
            (_recovery(failure_count=3), "invalid recovery event"),
            (_recovery(failure_class=CLASS_SOURCE_DEGRADED), "invalid recovery event"),
            (_recovery(threshold=2), "invalid recovery event"),
        ],
    )
    def test_invalid_outbox_entries_fail_closed(self, tmp_path, item, expected):
        _write_document(
            tmp_path,
            _v2(pending_notifications=[_recovery(), item]),
        )
        store = _store(tmp_path)

        with pytest.raises(RuntimeError, match=expected):
            store.apply((), threshold=2, now=_NOW)

    def test_canonical_outbox_entries_are_replayed_after_a_restart(self, tmp_path):
        """A well-formed v2 document round-trips into the delivery queue intact."""
        _write_document(tmp_path, _v2(pending_notifications=[_pending(), _recovery()]))
        store = _store(tmp_path)

        update = store.apply((), threshold=2, now=_NOW)

        assert [item.notification_type for item in update.pending_notifications] == [
            "provider_patrol_threshold",
            "provider_patrol_recovered",
        ]
        first = update.pending_notifications[0]
        assert first.threshold == 2
        assert first.transition.domain == "stock_daily"
        assert first.transition.source == "ths"
        assert update.skipped_results == 0


class TestInvalidFailureCountGuard:
    """Fault injection on the read step: the write-side guard must still hold.

    ``_read_state`` validates persisted counts, so the only way to reach the
    last defensive check in :meth:`PatrolFailureStore.apply` is to hand it a
    doctored entry mapping and prove the run aborts without writing.
    """

    @pytest.mark.parametrize("bad_count", ["3", None, 1.5, True])
    def test_doctored_entry_aborts_the_run(self, tmp_path, monkeypatch, bad_count):
        store = _store(tmp_path)
        monkeypatch.setattr(
            store,
            "_read_state",
            lambda: ({"ths/stock_daily": {"failure_count": bad_count}}, []),
        )

        with pytest.raises(RuntimeError, match="state contains an invalid failure count"):
            store.apply([_result("stock_daily")], threshold=2, now=_NOW)

        assert not store.path.exists()

    def test_valid_previous_count_still_extends_the_streak(self, tmp_path):
        store = _store(tmp_path)
        store.apply([_result("stock_daily")], threshold=3, now=_NOW)

        update = store.apply([_result("stock_daily")], threshold=3, now=_NOW)

        assert update.failed_results == 1
        assert _read_document(tmp_path)["entries"]["ths/stock_daily"]["failure_count"] == 2


class TestNotificationAcknowledgement:
    def test_a_full_outbox_fails_closed_before_anything_is_committed(self, tmp_path):
        """1000 queued alerts is the ceiling: the 1001st must not be written."""
        filled = [
            _pending(notification_id=str(uuid.uuid5(uuid.NAMESPACE_OID, f"c85-{index}")))
            for index in range(1000)
        ]
        _write_document(tmp_path, _v2(pending_notifications=filled))
        store = _store(tmp_path)
        before = _read_document(tmp_path)

        with pytest.raises(RuntimeError, match="notification outbox is full"):
            store.apply([_result("stock_daily")], threshold=1, now=_NOW)

        assert _read_document(tmp_path) == before
        assert len(before["pending_notifications"]) == 1000

    def test_unknown_id_returns_false_without_touching_the_state(self, tmp_path):
        store = _store(tmp_path)
        store.apply([_result("stock_daily")], threshold=1, now=_NOW)
        before = _read_document(tmp_path)

        assert store.acknowledge_notification(str(uuid.uuid4())) is False

        assert _read_document(tmp_path) == before

    def test_empty_store_acknowledges_nothing(self, tmp_path):
        assert _store(tmp_path).acknowledge_notification(str(uuid.uuid4())) is False

    def test_delivered_id_is_removed_and_a_second_ack_is_a_no_op(self, tmp_path):
        store = _store(tmp_path)
        update = store.apply([_result("stock_daily")], threshold=1, now=_NOW)
        notification_id = update.pending_notifications[0].notification_id

        assert store.acknowledge_notification(notification_id) is True
        assert store.acknowledge_notification(notification_id) is False

        assert _read_document(tmp_path)["pending_notifications"] == []


class TestScheduledPatrolRun:
    async def test_selection_keeps_only_p0_legs_with_an_implemented_probe(self, tmp_path):
        """Other priorities, unverified legs and probe-less legs are all reported apart."""
        seen = {}

        async def runner(registry, capabilities):
            seen["registry"] = registry
            seen["capabilities"] = tuple(capabilities)
            return [_result("futures_daily")]

        registry = _Registry(
            [
                _capability("futures_daily"),
                _capability("stock_action", source="probeless"),
                _capability("index_constituent", verified=False),
                _capability("economy_cpi"),
                _capability("trading_calendar"),
            ]
        )
        domains = {
            **_p0_domains("futures_daily", "stock_action", "index_constituent"),
            "economy_cpi": DomainSpec(
                display_name="economy_cpi",
                rest_path="c85/economy_cpi",
                contract="Bar",
                priority="P1",
            ),
            "trading_calendar": DomainSpec(
                display_name="trading_calendar",
                rest_path="c85/trading_calendar",
                contract="Bar",
                priority=None,
            ),
        }
        sink = _Sink()

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=registry,
            domains=domains,
            patrol_runner=runner,
            notification_sink=sink,
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert seen["capabilities"] == (_capability("futures_daily"),)
        assert report["selected_capabilities"] == 1
        assert report["ineligible_capabilities"] == [
            "probeless/stock_action:no-probe",
            "ths/index_constituent:unverified-or-disabled",
        ]
        assert report["unknown_priority_domains"] == ["trading_calendar"]
        assert report["p0_domains_without_registered_capability"] == []
        assert report["p0_domains_without_eligible_probe"] == ["index_constituent", "stock_action"]
        assert report["alerts_broadcast"] == 1
        assert sink.events[0]["domain"] == "futures_daily"

    async def test_a_leg_with_no_result_is_reported_unknown_not_healthy(self, tmp_path):
        """The patrol returning nothing says nothing: the leg must stay unknown."""
        calls = []

        async def runner(registry, capabilities):
            calls.append((registry, capabilities))
            return []

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=_Registry([_capability("option_daily")]),
            domains=_p0_domains("option_daily"),
            patrol_runner=runner,
            notification_sink=_Sink(),
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert report["probed"] == 0
        assert report["failed"] == 0
        assert report["unknown"] == 1
        assert report["unreported_capabilities"] == ["ths/option_daily"]
        assert report["key_observations_active"] == 0
        assert report["results"] == [
            {
                "domain": "option_daily",
                "source": "ths",
                "status": "unknown",
                "failure_class": None,
            }
        ]
        # no probe ran, so the rolling option leg consumed no catalog request either
        assert report["metadata_dependency_provider_requests"] == 0
        assert report["metadata_dependency_legs"] == []

    async def test_default_sink_broadcasts_the_event_on_the_websocket_channel(
        self, monkeypatch, tmp_path
    ):
        from opendata.api import websocket

        broadcasts = []

        class _WsManager:
            async def broadcast(self, message):
                broadcasts.append(message)

        monkeypatch.setattr(websocket, "ws_manager", _WsManager())
        event = {
            "notification_type": "provider_patrol_threshold",
            "domain": "futures_daily",
            "source": "ths",
        }

        await scheduled_patrol._websocket_notification_sink(event)

        assert broadcasts == [{"type": "task_notification", "data": event}]

    async def test_production_patrol_entrypoint_receives_the_selected_legs(
        self, tmp_path, monkeypatch
    ):
        """``patrol_runner=None`` must call the real patrol with only P0 legs."""
        from opendata.pipeline import patrol as patrol_module

        seen = {}

        async def fake_patrol(registry, *, capabilities):
            seen["registry"] = registry
            seen["capabilities"] = tuple(capabilities)
            return [_result("option_daily"), _result("futures_daily", attempts=2)]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)
        capabilities = [_capability("option_daily"), _capability("futures_daily")]
        sink = _Sink()

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=3),
            registry=_Registry(capabilities),
            domains=_p0_domains("option_daily", "futures_daily"),
            notification_sink=sink,
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert seen["registry"] is not None
        assert seen["capabilities"] == tuple(capabilities)
        assert report["status"] == "completed"
        assert report["selected_capabilities"] == 2
        assert report["probed"] == 2
        assert report["failed"] == 2
        assert report["unknown"] == 0
        assert report["alerts_broadcast"] == 0
        assert report["active_failure_streaks"] == 2
        # only the rolling option leg is declared as a catalog-dependency probe
        assert report["metadata_dependency_provider_requests"] == 1
        assert report["metadata_dependency_legs"] == ["ths/instrument"]
        assert report["key_observations_active"] == 2
        assert sink.events == []

    async def test_threshold_crossing_broadcasts_once_with_a_credential_safe_event(
        self, tmp_path, monkeypatch
    ):
        from opendata.pipeline import patrol as patrol_module

        async def fake_patrol(registry, *, capabilities):
            return [_result("futures_daily")]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)
        store = _store(tmp_path)
        sink = _Sink()
        args = {
            "settings_override": _settings(tmp_path, threshold=2),
            "registry": _Registry([_capability("futures_daily")]),
            "domains": _p0_domains("futures_daily"),
            "notification_sink": sink,
            "failure_store": store,
            "now": _NOW,
        }

        first = await execute_scheduled_patrol(_template(), **args)
        second = await execute_scheduled_patrol(_template(), **args)

        assert first["alerts_broadcast"] == 0
        assert second["alerts_broadcast"] == 1
        assert second["active_failure_streaks"] == 1
        event = sink.events[0]
        assert event["notification_type"] == "provider_patrol_threshold"
        assert event["source"] == "ths"
        assert event["domain"] == "futures_daily"
        assert event["consecutive_failures"] == 2
        assert event["failure_class"] == CLASS_SOURCE_DEGRADED
        assert event["threshold"] == 2
        assert event["timestamp"] == _NOW.isoformat()
        assert "CANARY-SECRET" not in json.dumps(event)
        assert _read_document(tmp_path)["pending_notifications"] == []

    async def test_recovered_leg_broadcasts_a_cleared_streak(self, tmp_path, monkeypatch):
        from opendata.pipeline import patrol as patrol_module

        outcomes = {"ok": False}

        async def fake_patrol(registry, *, capabilities):
            healthy = outcomes["ok"]
            return [
                _result(
                    "futures_daily",
                    ok=healthy,
                    failure_class=None if healthy else CLASS_SOURCE_DEGRADED,
                )
            ]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)
        args = {
            "settings_override": _settings(tmp_path, threshold=1),
            "registry": _Registry([_capability("futures_daily")]),
            "domains": _p0_domains("futures_daily"),
            "notification_sink": _Sink(),
            "failure_store": _store(tmp_path),
            "now": _NOW,
        }

        first = await execute_scheduled_patrol(_template(), **args)
        outcomes["ok"] = True
        second = await execute_scheduled_patrol(_template(), **args)

        assert first["alerts_broadcast"] == 1
        assert second["recoveries_broadcast"] == 1
        assert second["active_failure_streaks"] == 0
        assert second["results"] == [
            {
                "domain": "futures_daily",
                "source": "ths",
                "status": "healthy",
                "failure_class": None,
                "attempts": 1,
                "rows": 1,
                "latency_ms": 4.0,
            }
        ]
        assert second["unreported_capabilities"] == []

    async def test_delivery_failure_blocks_the_rest_of_the_outbox(self, tmp_path, monkeypatch):
        """Order matters: a blocked alert must stay durable and newer items wait."""
        from opendata.pipeline import patrol as patrol_module

        async def fake_patrol(registry, *, capabilities):
            return [_result("futures_daily"), _result("option_daily")]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)
        sink = _Sink(fail_for={"provider_patrol_threshold"})

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=_Registry([_capability("futures_daily"), _capability("option_daily")]),
            domains=_p0_domains("futures_daily", "option_daily"),
            notification_sink=sink,
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert report["notification_failures"] == 1
        assert report["alerts_broadcast"] == 0
        assert len(sink.events) == 1
        pending = _read_document(tmp_path)["pending_notifications"]
        # neither ack ran: the blocked alert and the newer one both stay durable
        assert len(pending) == 2
        assert [item["domain"] for item in pending] == ["futures_daily", "option_daily"]

    async def test_disabled_patrol_still_drains_a_durable_pending_alert(self, tmp_path):
        """PATROL_ENABLED=false skips probing but must not drop a queued alert."""
        store = _store(tmp_path)
        store.apply([_result("stock_daily")], threshold=1, now=_NOW)
        sink = _Sink()

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, enabled=False),
            notification_sink=sink,
            failure_store=store,
            now=_NOW,
        )

        assert report["status"] == "disabled"
        assert report["reason"] == "PATROL_ENABLED=false"
        assert report["probed"] == 0
        assert report["alerts_broadcast"] == 1
        assert sink.events[0]["notification_type"] == "provider_patrol_threshold"
        assert _read_document(tmp_path)["pending_notifications"] == []

    async def test_disabled_patrol_without_pending_alerts_stays_minimal(self, tmp_path):
        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, enabled=False),
            notification_sink=_Sink(),
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert report == {
            "status": "disabled",
            "reason": "PATROL_ENABLED=false",
            "probed": 0,
            "alerts_broadcast": 0,
        }

    async def test_no_eligible_capability_skips_the_patrol_entirely(self, tmp_path):
        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=_Registry([_capability("stock_daily", verified=False)]),
            domains=_p0_domains("stock_daily"),
            patrol_runner=None,
            notification_sink=_Sink(),
            failure_store=_store(tmp_path),
            now=_NOW,
        )

        assert report["status"] == "no_eligible_capabilities"
        assert report["selected_capabilities"] == 0
        assert report["ineligible_capabilities"] == ["ths/stock_daily:unverified-or-disabled"]
        assert report["p0_domains_without_eligible_probe"] == ["stock_daily"]
        assert report["probed"] == 0
        assert report["alerts_broadcast"] == 0

    def test_settings_singleton_is_loaded_lazily(self):
        """The default settings path only resolves the shared singleton."""
        from opendata.core.config import settings

        resolved = scheduled_patrol._settings()

        assert resolved is settings
        assert isinstance(resolved.patrol_failure_alert_threshold, int)
