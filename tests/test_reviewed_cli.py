"""Offline behavior tests for the injected reviewed CLI and role plan."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from apscheduler.events import EVENT_JOB_SUBMITTED, JobSubmissionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, func, select

from opendata.pipeline.run_dispatch import (
    DispatchIdentity,
    DispatchReceipt,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
    RunRecord,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunOutcome,
    RunService,
)
from opendata.pipeline.runtime_roles import RuntimeRolePlan, build_runtime_role_plan
from opendata.reviewed_cli import (
    MAX_REQUEST_BYTES,
    ReviewedCLIApplication,
    run_reviewed_cli,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
SCHEDULED_LOCAL = datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def cli_setup(tmp_path) -> tuple[Engine, RunDispatcher, RunService]:
    """Bind the CLI to a private SQLite ledger and fixture-only handlers."""
    engine = create_engine(f"sqlite:///{tmp_path / 'reviewed-cli.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    task = TaskDefinition(
        job_id="dispatch-daily",
        version="v1",
        dataset_id="market.daily",
        provider="fixture-provider",
        model="daily-bars",
        timezone="Asia/Shanghai",
        schedule=ScheduleMetadata(kind="cron", expression="30 17 * * *"),
        enabled=True,
    )

    def fetch(_request, _context):
        return {"fixture": True}

    def normalize(_raw, _request, _context):
        return NormalizedBatch(
            records=(
                NormalizedRecord(
                    business_key="000001.SZ:2026-10-26",
                    payload={"symbol": "000001.SZ", "close": 12.5},
                    business_date=date(2026, 10, 26),
                ),
            ),
            schema_version="1",
            semantic_version="1",
            unit="CNY/share",
            adjustment_basis="none",
            quality={"rejected_rows": 0},
            provenance={"source": "fixture"},
        )

    def publish(_connection, current_task, _request, batch, _run_key):
        return PublicationReceipt(
            "fixture-publication", current_task.dataset_id, len(batch.records)
        )

    catalog = ReviewedTaskCatalog(
        [ReviewedJob(task=task, fetch=fetch, normalize=normalize, publish=publish, reviewed=True)]
    )
    dispatcher = RunDispatcher.from_engine(engine, catalog, clock=lambda: NOW)
    worker = RunService(
        RunLedger(engine),
        catalog.registry,
        clock=lambda: NOW,
    )
    yield engine, dispatcher, worker
    engine.dispose()


def reviewed_plan(*roles: str):
    """Create a reviewed test plan with explicit writer ownership."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles=roles,
        reviewed_config_id="test.reviewed-app",
        reviewed_sink_ids={"market.daily"},
    )


def cli_app(dispatcher: RunDispatcher, worker: RunService | None = None) -> ReviewedCLIApplication:
    """Bind the concrete test application boundaries enabled by the plan."""
    roles = ("api", "scheduler", "worker") if worker is not None else ("api", "scheduler")
    return ReviewedCLIApplication(
        runtime_plan=reviewed_plan(*roles),
        dispatcher=dispatcher,
        worker_once=worker.run_once if worker is not None else None,
    )


def invoke(
    body: bytes,
    application: ReviewedCLIApplication | None,
    *,
    stdin: object | None = None,
    source: str = "-",
) -> tuple[int, dict[str, object], str]:
    """Run one CLI request and decode only its safe receipt stream."""
    out = io.StringIO()
    err = io.StringIO()
    if stdin is None:
        stdin = io.BytesIO(body)
    code = run_reviewed_cli(
        ["--request", source],
        application=application,
        stdin=stdin,
        stdout=out,
        stderr=err,
    )
    text = out.getvalue() or err.getvalue()
    return code, json.loads(text), text


def row_count(engine: Engine) -> int:
    """Count the fixture-owned run ledger rows."""
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(RUN_LEDGER_TABLE)).scalar_one()
        )


def make_run_record(status: str = "failed") -> RunRecord:
    """Create a minimal ledger state for worker receipt coverage."""
    return RunRecord(
        run_key="a" * 64,
        job_snapshot_json="contains-private-request-values",
        status=status,  # type: ignore[arg-type]
        worker="fixture-worker",
        lease_id="lease-1",
        fencing_token=1,
        lease_expires_at=NOW + timedelta(minutes=1),
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        heartbeat_at=NOW,
        finished_at=None,
        error_text="secret error text",
    )


def scheduled_request(*, dry_run: bool = False) -> bytes:
    """Encode a valid scheduled slot request using the exact UTC instant."""
    return json.dumps(
        {
            "schema_version": 1,
            "operation": "scheduled",
            "job_id": "dispatch-daily",
            "version": "v1",
            "occurrence": "2026-10-26T09:30:00Z",
            "dry_run": dry_run,
        }
    ).encode()


def test_default_roles_preserve_legacy_and_reviewed_roles_require_explicit_plan() -> None:
    """Keep rollout defaults legacy-compatible and reject mixed writer modes."""
    default = build_runtime_role_plan()
    assert default.profile == "legacy_compatible"
    assert default.roles == frozenset({"api", "legacy"})
    assert default.enables("legacy")
    assert not default.enables("worker")

    with pytest.raises(ValueError, match="explicit role selection"):
        build_runtime_role_plan(profile="reviewed")
    with pytest.raises(ValueError, match="explicit reviewed config"):
        build_runtime_role_plan(profile="reviewed", roles={"scheduler"})
    with pytest.raises(ValueError, match="mutually exclusive"):
        build_runtime_role_plan(
            profile="reviewed",
            roles={"legacy", "scheduler"},
            reviewed_config_id="test.app",
            reviewed_sink_ids={"market.daily"},
        )
    with pytest.raises(ValueError, match="declared sink"):
        build_runtime_role_plan(profile="reviewed", roles={"worker"}, reviewed_config_id="test.app")
    with pytest.raises(ValueError, match="profile is unsupported"):
        RuntimeRolePlan("unknown", frozenset({"api"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty frozen set"):
        RuntimeRolePlan("legacy_compatible", set())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported value"):
        RuntimeRolePlan("legacy_compatible", frozenset({"shell"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="frozen set"):
        RuntimeRolePlan("legacy_compatible", frozenset({"api"}), reviewed_sink_ids=set())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sink id is invalid"):
        build_runtime_role_plan(
            profile="reviewed",
            roles={"worker"},
            reviewed_config_id="test.app",
            reviewed_sink_ids={"bad sink"},
        )
    with pytest.raises(ValueError, match="reviewed roles"):
        RuntimeRolePlan("legacy_compatible", frozenset({"api", "worker"}))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot select reviewed configuration"):
        RuntimeRolePlan("legacy_compatible", frozenset({"api"}), reviewed_config_id="test.app")
    with pytest.raises(ValueError, match="unsupported"):
        default.enables("shell")  # type: ignore[arg-type]


def test_missing_reviewed_application_fails_closed_before_opening_request() -> None:
    """Do not inspect files, settings, or environment when app injection is absent."""
    code, response, _ = invoke(b"not-json", None, source="/no/such/request.json")
    assert code == 78
    assert response == {"code": "reviewed_application_required", "status": "rejected"}
    code, response, _ = invoke(b"{}", object())  # type: ignore[arg-type]
    assert code == 78 and response["code"] == "reviewed_application_invalid"

    out = io.StringIO()
    err = io.StringIO()
    assert run_reviewed_cli(["--request", "-"], application=None, stdout=out, stderr=err) == 78
    assert json.loads(err.getvalue())["code"] == "reviewed_application_required"

    for bad_source in ("", "x\x00y", "x" * 4_097):
        out = io.StringIO()
        err = io.StringIO()
        assert (
            run_reviewed_cli(
                ["--request", bad_source],
                application=None,
                stdout=out,
                stderr=err,
            )
            == 2
        )


def test_reviewed_application_requires_enabled_injected_boundaries(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Reject legacy plans and missing or excess runtime injections at construction."""
    _, dispatcher, _ = cli_setup
    with pytest.raises(ValueError, match="explicit reviewed runtime plan"):
        ReviewedCLIApplication(runtime_plan=build_runtime_role_plan(), dispatcher=dispatcher)
    with pytest.raises(ValueError, match="injected RunDispatcher"):
        ReviewedCLIApplication(runtime_plan=reviewed_plan("api"))
    with pytest.raises(ValueError, match="while dispatch roles are disabled"):
        ReviewedCLIApplication(runtime_plan=reviewed_plan("worker"), dispatcher=dispatcher)
    with pytest.raises(ValueError, match="injected worker-once SPI"):
        ReviewedCLIApplication(runtime_plan=reviewed_plan("worker"))
    with pytest.raises(ValueError, match="while worker role is disabled"):
        ReviewedCLIApplication(
            runtime_plan=reviewed_plan("api"), dispatcher=dispatcher, worker_once=lambda *_: None
        )
    with pytest.raises(TypeError, match="RuntimeRolePlan"):
        ReviewedCLIApplication(runtime_plan=object())  # type: ignore[arg-type]


def test_cli_and_apscheduler_use_the_same_slot_key_and_dry_run_writes_nothing(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Prove CLI and APScheduler share the occurrence identity from the real slot."""
    engine, dispatcher, _ = cli_setup
    application = cli_app(dispatcher)

    dry_code, dry_receipt, dry_text = invoke(scheduled_request(dry_run=True), application)
    assert dry_code == 0
    assert dry_receipt["status"] == "dry_run"
    assert row_count(engine) == 0

    cli_code, cli_receipt, _ = invoke(scheduled_request(), application)
    assert cli_code == 0
    assert cli_receipt["run_key"] == dry_receipt["run_key"]
    assert row_count(engine) == 1

    binding = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler_job_id = binding.register(scheduler)
    event = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [SCHEDULED_LOCAL],
    )
    aps_receipt = binding(event)[0]
    assert aps_receipt.identity.run_key == cli_receipt["run_key"]
    assert row_count(engine) == 1
    assert "2026-10-26T09:30:00Z" not in dry_text


@pytest.mark.parametrize(
    "request_doc",
    [
        {
            "schema_version": 1,
            "operation": "manual",
            "job_id": "dispatch-daily",
            "version": "v1",
            "payload": {"idempotency_key": "manual-safe", "query": {"symbol": "SECRET-SYMBOL"}},
        },
        {
            "schema_version": 1,
            "operation": "backfill",
            "job_id": "dispatch-daily",
            "version": "v1",
            "payload": {
                "idempotency_key": "backfill-safe",
                "backfill_start": "2026-10-01",
                "backfill_end": "2026-10-26",
                "query": {"symbol": "SECRET-SYMBOL"},
            },
        },
    ],
)
def test_manual_and_backfill_queue_only_safe_receipts(
    cli_setup: tuple[Engine, RunDispatcher, RunService], request_doc: dict[str, object]
) -> None:
    """Route manual and backfill modes to the same dispatcher without echoing data."""
    engine, dispatcher, _ = cli_setup
    code, receipt, text = invoke(json.dumps(request_doc).encode(), cli_app(dispatcher))
    assert code == 0
    assert receipt["status"] == "queued"
    assert "SECRET-SYMBOL" not in text
    assert "query" not in text
    assert row_count(engine) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"script":"evil"}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x"},"module":"bad"}',
        b'{"schema_version":1,"schema_version":1,"operation":"worker_once","worker":"w","lease_seconds":1}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"n":NaN}}}',
        b'{"schema_version":1,"operation":"worker_once","worker":"w","lease_seconds":true}',
        b'{"schema_version":1,"operation":"worker_once","worker":"bad worker","lease_seconds":1}',
        b'{"schema_version":1,"operation":"worker_once","lease_seconds":1}',
        b'{"schema_version":1,"operation":"scheduled","job_id":"daily","version":"v1"}',
        b'{"schema_version":1,"operation":"scheduled","job_id":"bad job",'
        b'"version":"v1","occurrence":"2026-10-26T09:30:00Z"}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","occurrence":"2026-10-26T09:30:00Z","payload":{}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1"}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"x":1e999}}}',
        b'{"schema_version":2,"operation":"manual","job_id":"daily","version":"v1","payload":{}}',
        b'{"schema_version":1,"operation":"unknown","job_id":"daily","version":"v1"}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"nested":{"dsn":"mysql://private"}}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"database_url":"mysql://readonly:credential-canary@db.invalid/warehouse"}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"nested":{"connection_string":"mysql://readonly:credential-canary@db.invalid/warehouse"}}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"nested":{"DATABASE-URI":"mysql://readonly@db.invalid/warehouse"}}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"url":"postgresql://readonly:credential-canary@db.invalid/warehouse"}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"urls":["postgresql://readonly:credential-canary@db.invalid/warehouse"]}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"url":"mysql://[invalid:credential-canary@db.invalid/warehouse"}}}',
        b'{"schema_version":1,"operation":"manual","job_id":"daily","version":"v1","payload":{"idempotency_key":"x","query":{"commands":[{"func-tion":"bad"}]}}}',
        b'["not","an","object"]',
        b"\xff",
    ],
)
def test_malformed_or_executable_requests_are_rejected_without_persistence(
    cli_setup: tuple[Engine, RunDispatcher, RunService], raw: bytes
) -> None:
    """Reject duplicate, extra, non-finite, wrongly typed, and executable fields."""
    engine, dispatcher, worker = cli_setup
    code, receipt, text = invoke(raw, cli_app(dispatcher, worker))
    assert code == 2
    assert receipt == {"code": "invalid_request", "status": "rejected"}
    assert "evil" not in text and "bad" not in text
    assert "credential-canary" not in text
    assert row_count(engine) == 0


def test_request_depth_and_byte_caps_fail_before_dispatch(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Enforce request complexity budgets for both stdin and file sources."""
    engine, dispatcher, worker = cli_setup
    nested: object = "leaf"
    for _ in range(20):
        nested = {"x": nested}
    deep = json.dumps(
        {
            "schema_version": 1,
            "operation": "manual",
            "job_id": "dispatch-daily",
            "version": "v1",
            "payload": {"idempotency_key": "deep", "query": {"v": nested}},
        }
    ).encode()
    code, response, _ = invoke(deep, cli_app(dispatcher, worker))
    assert code == 2 and response["code"] == "invalid_request"

    too_large = b" " * (MAX_REQUEST_BYTES + 1)
    code, response, _ = invoke(too_large, cli_app(dispatcher, worker))
    assert code == 2 and response["code"] == "invalid_request"
    too_many_nodes = json.dumps(
        {
            "schema_version": 1,
            "operation": "manual",
            "job_id": "dispatch-daily",
            "version": "v1",
            "payload": {"idempotency_key": "nodes", "query": {"values": list(range(8_200))}},
        }
    ).encode()
    code, response, _ = invoke(too_many_nodes, cli_app(dispatcher, worker))
    assert code == 2 and response["code"] == "invalid_request"
    invalid_offset = json.dumps(
        {
            "schema_version": 1,
            "operation": "scheduled",
            "job_id": "dispatch-daily",
            "version": "v1",
            "occurrence": "2026-10-26T17:30:00+08:00",
        }
    ).encode()
    code, response, _ = invoke(invalid_offset, cli_app(dispatcher, worker))
    assert code == 2 and response["code"] == "request_rejected"
    assert row_count(engine) == 0


def test_worker_once_calls_only_injected_spi_and_reports_terminal_state(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Use the provided RunService SPI once and avoid returning snapshots/errors."""
    engine, dispatcher, worker = cli_setup
    manual = {
        "schema_version": 1,
        "operation": "manual",
        "job_id": "dispatch-daily",
        "version": "v1",
        "payload": {"idempotency_key": "worker-once-key", "query": {"private": "hide-me"}},
    }
    assert invoke(json.dumps(manual).encode(), cli_app(dispatcher, worker))[0] == 0

    worker_request = {
        "schema_version": 1,
        "operation": "worker_once",
        "worker": "fixture-worker",
        "lease_seconds": 60,
    }
    code, receipt, text = invoke(json.dumps(worker_request).encode(), cli_app(dispatcher, worker))
    assert code == 0
    assert receipt["status"] == "succeeded"
    assert "hide-me" not in text and "job_snapshot" not in text
    assert row_count(engine) == 1

    idle_code, idle_receipt, _ = invoke(
        json.dumps(worker_request).encode(), cli_app(dispatcher, worker)
    )
    assert idle_code == 0 and idle_receipt == {"status": "idle"}


def test_worker_role_must_be_explicit_and_cancellation_uses_exit_130(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Enforce role activation and map Ctrl-C to one sanitized cancellation status."""
    body = json.dumps(
        {"schema_version": 1, "operation": "worker_once", "worker": "w", "lease_seconds": 1}
    ).encode()
    _, dispatcher, _ = cli_setup
    api_only = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("api"),
        dispatcher=dispatcher,
    )
    code, receipt, _ = invoke(body, api_only)
    assert code == 78 and receipt["code"] == "role_not_enabled"

    def interrupted(_worker: str, _ttl: timedelta) -> RunOutcome | None:
        raise KeyboardInterrupt

    worker_only = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("worker"),
        worker_once=interrupted,
    )
    code, receipt, _ = invoke(body, worker_only)
    assert code == 130
    assert receipt == {"code": "interrupted", "status": "cancelled"}

    class InterruptedReader:
        """Represent Ctrl-C while a launcher is reading stdin."""

        def read(self, _size: int) -> bytes:
            raise KeyboardInterrupt

    out = io.StringIO()
    err = io.StringIO()
    code = run_reviewed_cli(
        ["--request", "-"],
        application=worker_only,
        stdin=InterruptedReader(),
        stdout=out,
        stderr=err,
    )
    assert code == 130
    assert json.loads(err.getvalue()) == {"code": "interrupted", "status": "cancelled"}


@pytest.mark.parametrize(
    ("outcome", "expected_code", "expected_status"),
    [
        (None, 0, "idle"),
        (RunOutcome(record=None, publication=None), 1, "failed"),
        (RunOutcome(record=make_run_record("failed"), publication=None), 1, "failed"),
        (RunOutcome(record=make_run_record("cancelled"), publication=None), 130, "cancelled"),
    ],
)
def test_worker_receipts_match_terminal_state_and_hide_snapshot_details(
    outcome: RunOutcome | None, expected_code: int, expected_status: str
) -> None:
    """Reflect worker status in the exit code and omit snapshots or error text."""
    worker_only = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("worker"),
        worker_once=lambda _worker, _ttl: outcome,
    )
    worker_request = json.dumps(
        {"schema_version": 1, "operation": "worker_once", "worker": "w", "lease_seconds": 1}
    ).encode()
    code, receipt, text = invoke(worker_request, worker_only)
    assert code == expected_code and receipt["status"] == expected_status
    assert "private-request-values" not in text and "secret error text" not in text


def test_worker_spi_invalid_result_is_sanitized() -> None:
    """Reject an injected worker that violates its RunOutcome contract."""
    worker_only = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("worker"),
        worker_once=lambda _worker, _ttl: "secret-payload",  # type: ignore[arg-type]
    )
    worker_request = json.dumps(
        {"schema_version": 1, "operation": "worker_once", "worker": "w", "lease_seconds": 1}
    ).encode()
    code, receipt, text = invoke(worker_request, worker_only)
    assert code == 1 and receipt["code"] == "operation_failed"
    assert "secret-payload" not in text


def test_file_request_source_is_regular_and_bounded(
    cli_setup: tuple[Engine, RunDispatcher, RunService], tmp_path
) -> None:
    """Read a valid JSON file while refusing symlinks and non-regular sources."""
    engine, dispatcher, worker = cli_setup
    request_file = tmp_path / "request.json"
    request_file.write_bytes(scheduled_request(dry_run=True))
    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), source=str(request_file))
    assert code == 0 and receipt["status"] == "dry_run"
    assert row_count(engine) == 0

    symlink = tmp_path / "request-link.json"
    symlink.symlink_to(request_file)
    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), source=str(symlink))
    assert code == 2 and receipt["code"] == "invalid_request"

    directory = tmp_path / "request-dir"
    directory.mkdir()
    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), source=str(directory))
    assert code == 2 and receipt["code"] == "invalid_request"

    large_file = tmp_path / "large-request.json"
    large_file.write_bytes(b" " * (MAX_REQUEST_BYTES + 1))
    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), source=str(large_file))
    assert code == 2 and receipt["code"] == "invalid_request"

    missing = tmp_path / "missing-request.json"
    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), source=str(missing))
    assert code == 2 and receipt["code"] == "invalid_request"

    out = io.StringIO()
    err = io.StringIO()
    text_stdin = io.StringIO(scheduled_request(dry_run=True).decode())
    code = run_reviewed_cli(
        ["--request", "-"],
        application=cli_app(dispatcher, worker),
        stdin=text_stdin,
        stdout=out,
        stderr=err,
    )
    assert code == 0 and json.loads(out.getvalue())["status"] == "dry_run"

    class Unreadable:
        """Represent a broken stream supplied by a launcher."""

    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), stdin=Unreadable())
    assert code == 2 and receipt["code"] == "invalid_request"

    class UnsupportedReader:
        """Return an unsupported stream value to exercise input validation."""

        def read(self, _size: int) -> int:
            return 1

    code, receipt, _ = invoke(b"", cli_app(dispatcher, worker), stdin=UnsupportedReader())
    assert code == 2 and receipt["code"] == "invalid_request"
    assert row_count(engine) == 0


def test_invalid_cli_arguments_and_worker_exceptions_are_sanitized(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Keep argv and unexpected injected failures out of output receipts."""
    engine, dispatcher, worker = cli_setup
    out = io.StringIO()
    err = io.StringIO()
    code = run_reviewed_cli(
        ["--request", "-", "--shell", "secret-command"],
        application=cli_app(dispatcher, worker),
        stdin=io.BytesIO(b"{}"),
        stdout=out,
        stderr=err,
    )
    assert code == 2
    assert json.loads(err.getvalue()) == {"code": "invalid_arguments", "status": "rejected"}
    assert "secret-command" not in err.getvalue()

    def failing(_worker: str, _ttl: timedelta) -> RunOutcome | None:
        raise RuntimeError("credential=never-print-this")

    failing_app = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("worker"),
        worker_once=failing,
    )
    worker_request = json.dumps(
        {"schema_version": 1, "operation": "worker_once", "worker": "w", "lease_seconds": 1}
    ).encode()
    code, receipt, text = invoke(worker_request, failing_app)
    assert code == 1 and receipt == {"code": "operation_failed", "status": "failed"}
    assert "never-print-this" not in text
    assert row_count(engine) == 0


def test_manual_retry_conflict_and_disabled_role_fail_closed(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
) -> None:
    """Keep idempotency conflicts distinct and reject dispatch outside enabled roles."""
    engine, dispatcher, _ = cli_setup
    first = {
        "schema_version": 1,
        "operation": "manual",
        "job_id": "dispatch-daily",
        "version": "v1",
        "payload": {"idempotency_key": "stable-key", "query": {"symbol": "AAA"}},
    }
    second = {
        **first,
        "payload": {"idempotency_key": "stable-key", "query": {"symbol": "BBB"}},
    }
    assert invoke(json.dumps(first).encode(), cli_app(dispatcher))[0] == 0
    code, receipt, text = invoke(json.dumps(second).encode(), cli_app(dispatcher))
    assert code == 1 and receipt["code"] == "identity_conflict"
    assert "BBB" not in text and row_count(engine) == 1

    scheduler_only = ReviewedCLIApplication(
        runtime_plan=reviewed_plan("scheduler"),
        dispatcher=dispatcher,
    )
    code, receipt, _ = invoke(json.dumps(first).encode(), scheduler_only)
    assert code == 78 and receipt["code"] == "role_not_enabled"


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [("queued", 0), ("succeeded", 0), ("failed", 1), ("cancelled", 130)],
)
def test_dispatch_receipt_status_matches_process_exit_code(
    cli_setup: tuple[Engine, RunDispatcher, RunService],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_code: int,
) -> None:
    """Make queued-run failures and cancellation visible to service managers."""
    _, dispatcher, _ = cli_setup
    receipt = DispatchReceipt(
        identity=DispatchIdentity(
            run_key="a" * 64,
            run_type="manual",
            job_id="dispatch-daily",
            version="v1",
            request_bytes=0,
        ),
        status=status,  # type: ignore[arg-type]
        created_at=NOW,
    )
    monkeypatch.setattr(dispatcher, "dispatch_manual", lambda *_args, **_kwargs: receipt)
    manual = {
        "schema_version": 1,
        "operation": "manual",
        "job_id": "dispatch-daily",
        "version": "v1",
        "payload": {"idempotency_key": "receipt-status"},
    }
    code, response, _ = invoke(json.dumps(manual).encode(), cli_app(dispatcher))
    assert code == expected_code
    assert response["status"] == status


def test_main_and_dispatcher_contract_failures_remain_sanitized(
    cli_setup: tuple[Engine, RunDispatcher, RunService], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep module startup fail-closed and avoid leaking malformed handler results."""
    from opendata import reviewed_cli

    monkeypatch.setattr(reviewed_cli.sys, "argv", ["reviewed_cli", "--request", "-"])
    monkeypatch.setattr(reviewed_cli.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(reviewed_cli.sys, "stdout", io.StringIO())
    monkeypatch.setattr(reviewed_cli.sys, "stderr", io.StringIO())
    assert reviewed_cli.main() == 78
    assert json.loads(reviewed_cli.sys.stderr.getvalue())["code"] == "reviewed_application_required"

    engine, dispatcher, _ = cli_setup
    monkeypatch.setattr(dispatcher, "dispatch_manual", lambda *_args, **_kwargs: None)
    manual = {
        "schema_version": 1,
        "operation": "manual",
        "job_id": "dispatch-daily",
        "version": "v1",
        "payload": {"idempotency_key": "invalid-return"},
    }
    code, receipt, _ = invoke(json.dumps(manual).encode(), cli_app(dispatcher))
    assert code == 1 and receipt["code"] == "operation_failed"
    assert row_count(engine) == 0


def test_module_entrypoint_fails_closed_without_loading_a_request() -> None:
    """Keep ``python -m`` fail-closed until a reviewed composition root exists."""
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.update({"APP_ENV": "testing", "TESTING": "true", "REDIS_URL": ""})
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(project_root), str(project_root / "opendata_client"))
    )
    result = subprocess.run(
        [sys.executable, "-m", "opendata.reviewed_cli", "--request", "-"],
        cwd=project_root,
        env=environment,
        input="not-json",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 78
    assert json.loads(result.stderr) == {
        "code": "reviewed_application_required",
        "status": "rejected",
    }
