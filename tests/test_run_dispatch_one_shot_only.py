"""Offline dispatcher tests for the dedicated one-shot-only entry point."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from opendata.pipeline.run_dispatch import (
    APSchedulerSubmissionCallback,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
    RunService,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

NOW = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
QUERY_VALUE = "SENSITIVE_QUERY_VALUE_42"
PARTITION_VALUE = "SENSITIVE_PARTITION_VALUE_20261029"
REQUEST_PAYLOAD = {
    "idempotency_key": "one-shot-request-20261029",
    "query": {"symbol": QUERY_VALUE},
    "partition": {"source_batch": PARTITION_VALUE},
}
Dispatchers = tuple[RunDispatcher, list[RunRequest], list[RunRequest]]


class _UnusedLedger:
    """Satisfy RunService's protocol without opening storage or network access."""

    def enqueue(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests must use the service spy")

    def claim(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests do not execute runs")

    def heartbeat(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests do not execute runs")

    def fail(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests do not execute runs")

    def get(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests do not execute runs")

    def commit_result(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("dispatcher tests do not execute runs")


def _fetch(request: RunRequest, context: RunExecutionContext) -> object:
    raise AssertionError("dispatch must not call a source adapter")


def _normalize(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    raise AssertionError("dispatch must not normalize source data")


def _publish(
    connection: Connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    raise AssertionError("dispatch must not publish source data")


def _job(*, one_shot_only: bool, version: str = "v1") -> ReviewedJob:
    """Build a reviewed synthetic job with no callable source implementation."""
    task = TaskDefinition(
        job_id="synthetic-one-shot",
        version=version,
        dataset_id="synthetic.dataset",
        provider="synthetic-provider",
        model="synthetic-model",
        timezone="UTC",
        schedule=None if one_shot_only else ScheduleMetadata(kind="cron", expression="0 9 * * *"),
        enabled=not one_shot_only,
    )
    return ReviewedJob(
        task=task,
        fetch=_fetch,
        normalize=_normalize,
        publish=_publish,
        reviewed=True,
        one_shot_only=one_shot_only,
    )


@pytest.fixture
def dispatchers(monkeypatch: pytest.MonkeyPatch) -> Dispatchers:
    """Create no-storage dispatchers and record service submission calls."""
    one_shot_requests: list[RunRequest] = []
    ordinary_requests: list[RunRequest] = []
    catalog = ReviewedTaskCatalog(
        (_job(one_shot_only=True), _job(one_shot_only=False, version="v2"))
    )
    dispatcher = RunDispatcher(_UnusedLedger(), catalog, clock=lambda: NOW)  # type: ignore[arg-type]

    def submit_one_shot(
        _service: RunService,
        _job_id: str,
        _version: str,
        request: RunRequest,
    ) -> SimpleNamespace:
        one_shot_requests.append(request)
        return SimpleNamespace(status="queued", created_at=NOW)

    def submit(
        _service: RunService,
        _job_id: str,
        _version: str,
        request: RunRequest,
    ) -> SimpleNamespace:
        ordinary_requests.append(request)
        return SimpleNamespace(status="queued", created_at=NOW)

    monkeypatch.setattr(RunService, "submit_one_shot", submit_one_shot)
    monkeypatch.setattr(RunService, "submit", submit)
    return dispatcher, one_shot_requests, ordinary_requests


def test_one_shot_dry_run_returns_only_safe_identity_without_service_write(
    dispatchers: Dispatchers,
) -> None:
    dispatcher, one_shot_requests, ordinary_requests = dispatchers

    receipt = dispatcher.dispatch_one_shot(
        "synthetic-one-shot",
        "v1",
        REQUEST_PAYLOAD,
        dry_run=True,
    )

    assert receipt.status == "dry_run"
    assert receipt.created_at is None
    assert receipt.identity.run_type == "manual"
    assert receipt.identity.job_id == "synthetic-one-shot"
    assert receipt.identity.version == "v1"
    assert receipt.identity.request_bytes > 0
    assert one_shot_requests == []
    assert ordinary_requests == []
    rendered = repr(receipt)
    assert QUERY_VALUE not in rendered
    assert PARTITION_VALUE not in rendered
    assert "query=" not in rendered
    assert "partition=" not in rendered


def test_one_shot_submission_uses_manual_request_and_stable_receipt_identity(
    dispatchers: Dispatchers,
) -> None:
    dispatcher, one_shot_requests, ordinary_requests = dispatchers

    first = dispatcher.dispatch_one_shot("synthetic-one-shot", "v1", REQUEST_PAYLOAD)
    retry = dispatcher.dispatch_one_shot("synthetic-one-shot", "v1", REQUEST_PAYLOAD)

    assert first == retry
    assert first.status == "queued"
    assert first.created_at == NOW
    assert first.identity.run_key == retry.identity.run_key
    assert len(one_shot_requests) == 2
    assert ordinary_requests == []
    assert all(request.run_type == "manual" for request in one_shot_requests)
    assert all(request.query == {"symbol": QUERY_VALUE} for request in one_shot_requests)
    assert all(
        request.partition == {"source_batch": PARTITION_VALUE} for request in one_shot_requests
    )
    assert all(
        request.idempotency_key == "one-shot-request-20261029" for request in one_shot_requests
    )
    rendered = repr(first)
    assert QUERY_VALUE not in rendered
    assert PARTITION_VALUE not in rendered


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("mode", ["manual", "backfill", "scheduled", "callback"])
def test_ordinary_dispatch_modes_reject_one_shot_job_even_for_dry_run(
    dispatchers: Dispatchers,
    mode: str,
    dry_run: bool,
) -> None:
    dispatcher, one_shot_requests, ordinary_requests = dispatchers

    with pytest.raises(ValueError, match="one-shot-only"):
        if mode == "manual":
            dispatcher.dispatch_manual("synthetic-one-shot", "v1", REQUEST_PAYLOAD, dry_run=dry_run)
        elif mode == "backfill":
            dispatcher.dispatch_backfill(
                "synthetic-one-shot", "v1", REQUEST_PAYLOAD, dry_run=dry_run
            )
        elif mode == "scheduled":
            dispatcher.dispatch_scheduled(
                "synthetic-one-shot",
                "v1",
                occurrence=NOW,
                payload=REQUEST_PAYLOAD,
                dry_run=dry_run,
            )
        else:
            dispatcher.apscheduler_callback("synthetic-one-shot", "v1")

    assert one_shot_requests == []
    assert ordinary_requests == []


def test_direct_callback_construction_rejects_one_shot_job(
    dispatchers: Dispatchers,
) -> None:
    dispatcher, one_shot_requests, ordinary_requests = dispatchers
    task = dispatcher._catalog.resolve("synthetic-one-shot", "v1")
    assert task is not None

    with pytest.raises(ValueError, match="one-shot-only"):
        APSchedulerSubmissionCallback(dispatcher, task, {})

    assert one_shot_requests == []
    assert ordinary_requests == []


def test_one_shot_entry_rejects_unmarked_jobs_and_regular_jobs_keep_manual_path(
    dispatchers: Dispatchers,
) -> None:
    dispatcher, one_shot_requests, ordinary_requests = dispatchers

    with pytest.raises(ValueError, match="not approved for one-shot"):
        dispatcher.dispatch_one_shot("synthetic-one-shot", "v2", REQUEST_PAYLOAD)

    normal = dispatcher.dispatch_manual("synthetic-one-shot", "v2", REQUEST_PAYLOAD)
    assert normal.status == "queued"
    assert one_shot_requests == []
    assert len(ordinary_requests) == 1
    assert ordinary_requests[0].run_type == "manual"
