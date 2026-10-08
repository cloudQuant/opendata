"""Offline behavior tests for the scheduled P0 provider patrol."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.core.config import Settings
from opendata.data.capability import Capability
from opendata.data.domains import DomainSpec, load_domains
from opendata.pipeline.key_health import CLASS_PATROL_GAP, CLASS_SOURCE_DEGRADED
from opendata.pipeline.patrol import PatrolResult
from opendata.pipeline.scheduled_patrol import PatrolFailureStore, execute_scheduled_patrol
from opendata.pipeline.templates import PIPELINE_TEMPLATES, ScheduleTemplate, TemplateKind

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_key_observations(monkeypatch):
    """Keep scheduled patrol observations local to each test."""
    from opendata.pipeline import key_health_notifications

    monkeypatch.setattr(
        key_health_notifications, "_OBSERVATIONS", key_health_notifications.RecentObservationStore()
    )


class _Registry:
    """Small registry view used to prove which capabilities the job selects."""

    def __init__(self, capabilities: list[Capability]) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> list[Capability]:
        """Return the registered capabilities in deterministic order."""
        return list(self._capabilities)


def _capability(domain: str, source: str = "ths", *, verified: bool = True) -> Capability:
    """Create a capability fixture without constructing a real provider."""
    return Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="cn",
        source=source,
        verified=verified,
    )


def _result(
    domain: str,
    *,
    source: str = "ths",
    ok: bool = False,
    failure_class: str | None = CLASS_SOURCE_DEGRADED,
    error: str | None = "CANARY-SECRET provider response",
) -> PatrolResult:
    """Build one classified or unknown test result."""
    return PatrolResult(
        domain=domain,
        source=source,
        ok=ok,
        error=error,
        latency_ms=4.0,
        verified=True,
        rows=1 if ok else None,
        attempts=1,
        failure_class=failure_class,
    )


def _p0_domains(*names: str) -> dict[str, DomainSpec]:
    """Build the explicit P0 metadata needed by one isolated test."""
    return {
        name: DomainSpec(
            display_name=name,
            rest_path=f"test/{name}",
            contract="Bar",
            priority="P0",
        )
        for name in names
    }


def _settings(directory: Path, *, enabled: bool = True, threshold: int = 2) -> Settings:
    """Create settings without reading a project dotenv file."""
    return Settings(
        _env_file=None,
        app_env="testing",
        data_dir=directory,
        patrol_enabled=enabled,
        patrol_failure_alert_threshold=threshold,
    )


def _template() -> ScheduleTemplate:
    """Return the actual scheduled-patrol template loaded from YAML."""
    return next(
        template
        for template in PIPELINE_TEMPLATES
        if template.kind is TemplateKind.SCHEDULED_PATROL
    )


class TestPatrolFailureStore:
    async def test_two_failures_cross_threshold_once_and_survive_store_recreation(self, tmp_path):
        capability = _capability("stock_daily")
        registry = _Registry([capability])
        domains = _p0_domains("stock_daily")
        state_path = tmp_path / "provider_patrol_failures.json"
        notifications: list[dict[str, object]] = []
        calls: list[list[str]] = []

        async def run(_registry, selected):
            calls.append([item.domain for item in selected])
            return [_result("stock_daily")]

        async def sink(event: dict[str, object]) -> None:
            notifications.append(event)

        first = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=domains,
            patrol_runner=run,
            notification_sink=sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        second = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=domains,
            patrol_runner=run,
            notification_sink=sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        third = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=domains,
            patrol_runner=run,
            notification_sink=sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )

        assert first["alerts_broadcast"] == 0
        assert second["alerts_broadcast"] == 1
        assert third["alerts_broadcast"] == 0
        assert len(notifications) == 1
        assert notifications[0]["notification_type"] == "provider_patrol_threshold"
        assert notifications[0]["consecutive_failures"] == 2
        assert calls == [["stock_daily"], ["stock_daily"], ["stock_daily"]]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["entries"]["ths/stock_daily"]["failure_count"] == 3
        assert "CANARY-SECRET" not in state_path.read_text(encoding="utf-8")
        assert "CANARY-SECRET" not in json.dumps(second)
        from opendata.pipeline.key_health_notifications import recent_patrol_observations

        assert [
            observation.source for observation in recent_patrol_observations(now=_NOW).observations
        ] == ["ths"]

    async def test_success_clears_only_its_capability_and_emits_recovery(self, tmp_path):
        capabilities = [_capability("stock_daily"), _capability("stock_action")]
        registry = _Registry(capabilities)
        domains = _p0_domains("stock_daily", "stock_action")
        state_path = tmp_path / "provider_patrol_failures.json"
        notifications: list[dict[str, object]] = []

        async def failed_run(_registry, _selected):
            return [_result("stock_daily"), _result("stock_action")]

        async def recovery_run(_registry, _selected):
            return [
                _result("stock_daily", ok=True, failure_class=None, error=None),
                _result(
                    "stock_action",
                    failure_class=CLASS_PATROL_GAP,
                    error="query not configured",
                ),
            ]

        async def sink(event: dict[str, object]) -> None:
            notifications.append(event)

        await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=domains,
            patrol_runner=failed_run,
            notification_sink=sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=domains,
            patrol_runner=recovery_run,
            notification_sink=sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )

        assert report["active_failure_streaks"] == 1
        assert report["recoveries_broadcast"] == 1
        assert notifications[-1]["notification_type"] == "provider_patrol_recovered"
        assert notifications[-1]["domain"] == "stock_daily"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert set(state["entries"]) == {"ths/stock_action"}
        assert state["entries"]["ths/stock_action"]["failure_count"] == 1

    async def test_threshold_notification_retries_after_failed_delivery_and_store_restart(
        self, tmp_path
    ):
        state_path = tmp_path / "provider_patrol_failures.json"

        async def failed_run(_registry, _selected):
            return [_result("stock_daily")]

        async def failing_sink(_event):
            raise OSError("offline notification sink")

        delivered: list[dict[str, object]] = []

        async def working_sink(event: dict[str, object]) -> None:
            delivered.append(event)

        first = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=_Registry([_capability("stock_daily")]),
            domains=_p0_domains("stock_daily"),
            patrol_runner=failed_run,
            notification_sink=failing_sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        persisted_after_failure = json.loads(state_path.read_text(encoding="utf-8"))

        class ForbiddenRegistry:
            def capabilities(self):
                raise AssertionError("pending-only retry must not inspect providers")

        async def forbidden_runner(*_args):
            raise AssertionError("pending-only retry must not call provider probes")

        retried = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, enabled=False, threshold=1),
            registry=ForbiddenRegistry(),
            domains=_p0_domains("stock_daily"),
            patrol_runner=forbidden_runner,
            notification_sink=working_sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )

        assert first["alerts_broadcast"] == 0
        assert first["notification_failures"] == 1
        assert len(persisted_after_failure["pending_notifications"]) == 1
        assert retried["status"] == "disabled"
        assert retried["alerts_broadcast"] == 1
        assert delivered[0]["notification_type"] == "provider_patrol_threshold"
        assert (
            delivered[0]["notification_id"]
            == persisted_after_failure["pending_notifications"][0]["notification_id"]
        )
        assert json.loads(state_path.read_text(encoding="utf-8"))["pending_notifications"] == []

    async def test_recovery_notification_retries_after_failed_delivery_and_store_restart(
        self, tmp_path
    ):
        state_path = tmp_path / "provider_patrol_failures.json"
        registry = _Registry([_capability("stock_daily")])
        domains = _p0_domains("stock_daily")

        async def failed_run(_registry, _selected):
            return [_result("stock_daily")]

        async def recovery_run(_registry, _selected):
            return [_result("stock_daily", ok=True, failure_class=None, error=None)]

        async def empty_run(_registry, _selected):
            return []

        events_before_recovery: list[dict[str, object]] = []

        async def recording_sink(event: dict[str, object]) -> None:
            events_before_recovery.append(event)

        async def failing_sink(_event):
            raise OSError("offline notification sink")

        await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=registry,
            domains=domains,
            patrol_runner=failed_run,
            notification_sink=recording_sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        failed_recovery = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=registry,
            domains=domains,
            patrol_runner=recovery_run,
            notification_sink=failing_sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )
        pending_after_failure = json.loads(state_path.read_text(encoding="utf-8"))
        delivered: list[dict[str, object]] = []

        async def working_sink(event: dict[str, object]) -> None:
            delivered.append(event)

        retried = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=registry,
            domains=domains,
            patrol_runner=empty_run,
            notification_sink=working_sink,
            failure_store=PatrolFailureStore(state_path),
            now=_NOW,
        )

        assert failed_recovery["recoveries_broadcast"] == 0
        assert failed_recovery["notification_failures"] == 1
        assert pending_after_failure["entries"] == {}
        assert len(pending_after_failure["pending_notifications"]) == 1
        assert retried["recoveries_broadcast"] == 1
        assert delivered[0]["notification_type"] == "provider_patrol_recovered"
        assert (
            delivered[0]["notification_id"]
            == pending_after_failure["pending_notifications"][0]["notification_id"]
        )
        assert len(events_before_recovery) == 1
        assert events_before_recovery[0]["notification_type"] == "provider_patrol_threshold"
        assert json.loads(state_path.read_text(encoding="utf-8"))["pending_notifications"] == []

    async def test_full_outbox_fails_closed_without_committing_the_new_transition(self, tmp_path):
        state_path = tmp_path / "provider_patrol_failures.json"
        pending = [
            {
                "notification_id": str(uuid.UUID(int=index + 1)),
                "notification_type": "provider_patrol_threshold",
                "domain": "stock_daily",
                "source": "ths",
                "failure_count": 1,
                "failure_class": CLASS_SOURCE_DEGRADED,
                "observed_at": _NOW.isoformat(),
                "threshold": 1,
            }
            for index in range(1000)
        ]
        state_path.write_text(
            json.dumps({"version": 2, "entries": {}, "pending_notifications": pending}),
            encoding="utf-8",
        )

        async def failed_run(_registry, _selected):
            return [_result("stock_daily")]

        with pytest.raises(RuntimeError, match="outbox is full"):
            await execute_scheduled_patrol(
                _template(),
                settings_override=_settings(tmp_path, threshold=1),
                registry=_Registry([_capability("stock_daily")]),
                domains=_p0_domains("stock_daily"),
                patrol_runner=failed_run,
                failure_store=PatrolFailureStore(state_path),
                now=_NOW,
            )

        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert final_state["entries"] == {}
        assert len(final_state["pending_notifications"]) == 1000


class TestScheduledPatrolExecution:
    async def test_disabled_default_reports_without_registry_or_provider_access(self, tmp_path):
        class ForbiddenRegistry:
            def capabilities(self):
                raise AssertionError("disabled patrol must not inspect provider capabilities")

        async def forbidden_runner(*_args):
            raise AssertionError("disabled patrol must not request providers")

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, enabled=False),
            registry=ForbiddenRegistry(),
            patrol_runner=forbidden_runner,
        )

        assert report == {
            "status": "disabled",
            "reason": "PATROL_ENABLED=false",
            "probed": 0,
            "alerts_broadcast": 0,
        }

    async def test_only_explicit_verified_p0_capabilities_are_probed(self, tmp_path):
        p0_capability = _capability("stock_daily")
        unknown_capability = _capability("economy_gdp", source="ecb")
        registry = _Registry([p0_capability, unknown_capability])
        called: list[list[str]] = []

        async def run(_registry, selected):
            called.append([item.domain for item in selected])
            return [_result("stock_daily", ok=True, failure_class=None, error=None)]

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains={
                **_p0_domains("stock_daily"),
                "economy_gdp": DomainSpec(
                    display_name="GDP",
                    rest_path="economy/gdp",
                    contract="MacroSeries",
                ),
            },
            patrol_runner=run,
            failure_store=PatrolFailureStore(tmp_path / "state.json"),
            now=_NOW,
        )

        assert called == [["stock_daily"]]
        assert report["selected_capabilities"] == 1
        assert report["unknown_priority_domains"] == ["economy_gdp"]
        assert report["results"] == [
            {
                "domain": "stock_daily",
                "source": "ths",
                "status": "healthy",
                "failure_class": None,
                "attempts": 1,
                "rows": 1,
                "latency_ms": 4.0,
            }
        ]

    async def test_partial_failure_gap_and_missing_result_remain_distinct(self, tmp_path):
        registry = _Registry(
            [
                _capability("stock_daily"),
                _capability("stock_action"),
                _capability("financial_statement"),
            ]
        )

        async def run(_registry, _selected):
            return [
                _result("stock_daily"),
                _result("stock_action", failure_class=CLASS_PATROL_GAP),
            ]

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=_p0_domains("stock_daily", "stock_action", "financial_statement"),
            patrol_runner=run,
            failure_store=PatrolFailureStore(tmp_path / "state.json"),
            now=_NOW,
        )

        results = report["results"]
        assert isinstance(results, list)
        assert [row["status"] for row in results] == ["failed", "unknown", "unknown"]
        assert report["failed"] == 1
        assert report["skipped"] == 1
        assert report["unknown"] == 2
        assert report["unreported_capabilities"] == ["ths/financial_statement"]
        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert set(state["entries"]) == {"ths/stock_daily"}

    async def test_threshold_event_uses_the_existing_websocket_notification_channel(
        self, tmp_path, monkeypatch
    ):
        from opendata.api.websocket import ws_manager

        messages: list[dict[str, object]] = []

        async def record(message: dict[str, object]) -> None:
            messages.append(message)

        monkeypatch.setattr(ws_manager, "broadcast", record)

        async def run(_registry, _selected):
            return [_result("stock_daily")]

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path, threshold=1),
            registry=_Registry([_capability("stock_daily")]),
            domains=_p0_domains("stock_daily"),
            patrol_runner=run,
            failure_store=PatrolFailureStore(tmp_path / "state.json"),
            now=_NOW,
        )

        assert report["alerts_broadcast"] == 1
        assert messages[0]["type"] == "task_notification"
        data = messages[0]["data"]
        assert isinstance(data, dict)
        assert data["notification_type"] == "provider_patrol_threshold"
        assert data["source"] == "ths"

    async def test_unverified_or_unconfigured_p0_legs_are_not_probed(self, tmp_path):
        registry = _Registry(
            [
                _capability("stock_daily", verified=False),
                _capability("stock_adjust", source="ecb"),
            ]
        )
        called = False

        async def run(_registry, selected):
            nonlocal called
            called = True
            return []

        report = await execute_scheduled_patrol(
            _template(),
            settings_override=_settings(tmp_path),
            registry=registry,
            domains=_p0_domains("stock_daily", "stock_adjust"),
            patrol_runner=run,
        )

        assert called is False
        assert report["status"] == "no_eligible_capabilities"
        assert report["selected_capabilities"] == 0
        assert report["p0_domains_without_eligible_probe"] == ["stock_adjust", "stock_daily"]

    def test_patrol_is_disabled_by_default_and_threshold_must_be_positive(self, monkeypatch):
        monkeypatch.delenv("PATROL_ENABLED", raising=False)
        monkeypatch.delenv("PATROL_FAILURE_ALERT_THRESHOLD", raising=False)
        assert Settings(_env_file=None, app_env="testing").patrol_enabled is False
        with pytest.raises(ValidationError):
            Settings(
                _env_file=None,
                app_env="testing",
                patrol_failure_alert_threshold=0,
            )

    def test_shipped_template_targets_p0_and_names_placeholder_time(self):
        template = _template()

        assert template.cron == "0 18 * * *"
        assert template.payload == {"tier": "P0"}
        assert "UTC" in template.note
        assert "校准" in template.note

    def test_production_domain_priority_is_the_requirement_defined_eight(self):
        expected = {
            "stock_daily",
            "stock_adjust",
            "stock_action",
            "financial_statement",
            "financial_indicator",
            "index_constituent",
            "futures_daily",
            "futures_fundamentals",
        }

        assert {name for name, spec in load_domains().items() if spec.priority == "P0"} == (
            expected
        )
        assert all(
            spec.priority is None for name, spec in load_domains().items() if name not in expected
        )
