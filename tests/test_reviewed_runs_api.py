"""Isolated API checks for the reviewed-run administrative endpoints."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from sqlalchemy import Table, create_engine, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opendata.api.dependencies import get_db
from opendata.api.reviewed_runs import router
from opendata.core.config import settings
from opendata.core.security import create_access_token
from opendata.models.user import User, UserRole
from opendata.pipeline.run_dispatch import (
    DispatchIdentity,
    DispatchReceipt,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
    RunRecord,
)
from opendata.pipeline.run_service import (
    FetchHandler,
    NormalizedBatch,
    NormalizeHandler,
    PublicationReceipt,
    PublishHandler,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.runtime_roles import RuntimeRolePlan

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    import pytest
    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
ADMIN_SECRET = "reviewed-api-test-signing-key-32bytes-long"
ENABLED_JOB_ID = "fixture-daily"
DISABLED_JOB_ID = "fixture-disabled"
JOB_VERSION = "v1"
DATASET_ID = "fixture.market.daily"
_RUN_KEY = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True)
class APIHarness:
    """Resources owned by one reviewed-run API test."""

    app: FastAPI
    client: AsyncClient
    runtime: ReviewedRuntime
    metadata_engine: Engine
    admin_token: str
    user_token: str


class _FetchHandler(FetchHandler):
    """Provide a source adapter registered but never executed by API tests."""

    def __call__(self, _request: RunRequest, _context: RunExecutionContext) -> object:
        """Return a synthetic payload if a test unexpectedly invokes the handler."""
        return {"synthetic": True}


class _NormalizeHandler(NormalizeHandler):
    """Provide a normalizer that is registered but never executed by API tests."""

    def __call__(
        self, _raw: object, _request: RunRequest, _context: RunExecutionContext
    ) -> NormalizedBatch:
        """Fail if manual API submission unexpectedly runs a normalizer."""
        raise AssertionError("API submission must not execute a job normalizer")


class _PublishHandler(PublishHandler):
    """Provide a publisher that is registered but never executed by API tests."""

    def __call__(
        self,
        _connection: Connection,
        task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        _idempotency_key: str,
    ) -> PublicationReceipt:
        """Return a bounded synthetic receipt if a test invokes the publisher."""
        return PublicationReceipt("fixture-publication", task.dataset_id, len(batch.records))


def _catalog() -> ReviewedTaskCatalog:
    """Create two explicitly reviewed test jobs, one enabled and one disabled."""
    schedule = ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri")
    tasks = (
        TaskDefinition(
            job_id=ENABLED_JOB_ID,
            version=JOB_VERSION,
            dataset_id=DATASET_ID,
            provider="fixture-provider",
            model="daily-bars",
            schedule=schedule,
            enabled=True,
        ),
        TaskDefinition(
            job_id=DISABLED_JOB_ID,
            version=JOB_VERSION,
            dataset_id=DATASET_ID,
            provider="fixture-provider",
            model="disabled-bars",
            schedule=schedule,
            enabled=False,
        ),
    )
    return ReviewedTaskCatalog(
        [
            ReviewedJob(
                task=task,
                fetch=_FetchHandler(),
                normalize=_NormalizeHandler(),
                publish=_PublishHandler(),
                reviewed=True,
            )
            for task in tasks
        ]
    )


def _api_runtime(metadata_engine: Engine, catalog: ReviewedTaskCatalog) -> ReviewedRuntime:
    """Bind the test-owned metadata engine to an API-only reviewed runtime."""
    plan = RuntimeRolePlan(
        profile="reviewed",
        roles=frozenset({"api"}),
        reviewed_config_id="test.reviewed-runs",
    )
    return build_reviewed_runtime(
        metadata_engine=metadata_engine,
        catalog=catalog,
        runtime_plan=plan,
    )


@pytest_asyncio.fixture
async def api_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[APIHarness, None]:
    """Create a private auth database and a separate SQLite run ledger."""
    monkeypatch.setattr(settings, "secret_key", ADMIN_SECRET)
    auth_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.sqlite'}")
    user_table = User.__table__
    if not isinstance(user_table, Table):
        raise AssertionError("User.__table__ must be a SQLAlchemy Table")
    async with auth_engine.begin() as connection:
        await connection.run_sync(user_table.create)
    auth_sessions = async_sessionmaker(auth_engine, class_=AsyncSession, expire_on_commit=False)

    async with auth_sessions() as session:
        admin = User(
            username="reviewed-admin",
            email="reviewed-admin@example.test",
            hashed_password=f"fixture-{hashlib.sha256(b'not-a-password').hexdigest()}",
            role=UserRole.ADMIN,
            is_active=True,
        )
        regular_user = User(
            username="reviewed-user",
            email="reviewed-user@example.test",
            hashed_password=f"fixture-{hashlib.sha256(b'not-a-password').hexdigest()}",
            role=UserRole.USER,
            is_active=True,
        )
        session.add_all([admin, regular_user])
        await session.commit()
        await session.refresh(admin)
        await session.refresh(regular_user)
        admin_token = create_access_token({"sub": str(admin.id)})
        user_token = create_access_token({"sub": str(regular_user.id)})

    metadata_engine = create_engine(f"sqlite:///{tmp_path / 'reviewed-runs.sqlite'}")
    RUN_LEDGER_METADATA.create_all(metadata_engine)
    runtime = _api_runtime(metadata_engine, _catalog())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.reviewed_runtime = runtime

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with auth_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://reviewed.test",
        ) as client:
            yield APIHarness(
                app=app,
                client=client,
                runtime=runtime,
                metadata_engine=metadata_engine,
                admin_token=admin_token,
                user_token=user_token,
            )
    finally:
        app.dependency_overrides.clear()
        metadata_engine.dispose()
        await auth_engine.dispose()


def _admin_headers(harness: APIHarness) -> dict[str, str]:
    """Return a real JWT header for the owned admin row."""
    return {"Authorization": f"Bearer {harness.admin_token}"}


def _ledger_count(engine: Engine) -> int:
    """Read the row count from this test's private SQLite ledger."""
    with engine.connect() as connection:
        count = connection.scalar(select(func.count()).select_from(RUN_LEDGER_TABLE))
    assert isinstance(count, int)
    return count


def _request_body(**overrides: object) -> dict[str, object]:
    """Build the minimal public request shape and apply test-specific changes."""
    body: dict[str, object] = {
        "job_id": ENABLED_JOB_ID,
        "version": JOB_VERSION,
        "idempotency_key": "api-test-001",
    }
    body.update(overrides)
    return body


async def test_routes_require_admin_jwt_and_reject_api_keys(api_harness: APIHarness) -> None:
    """The admin alias rejects anonymous, API-key, and ordinary-user callers."""
    url = "/api/v1/reviewed/jobs"
    unauthenticated = await api_harness.client.get(url)
    api_key_header = await api_harness.client.get(url, headers={"X-API-Key": "od-test-key"})
    bearer_api_key = await api_harness.client.get(
        url,
        headers={"Authorization": "Bearer od-test-key"},
    )
    regular_user = await api_harness.client.get(
        url,
        headers={"Authorization": f"Bearer {api_harness.user_token}"},
    )

    assert unauthenticated.status_code == 401
    assert api_key_header.status_code == 401
    assert bearer_api_key.status_code == 401
    assert regular_user.status_code == 403


async def test_unconfigured_wrong_type_and_api_disabled_runtime_fail_closed(
    api_harness: APIHarness,
) -> None:
    """Runtime absence, impostor objects, and a worker-only plan return 503."""
    app = api_harness.app
    headers = _admin_headers(api_harness)

    delattr(app.state, "reviewed_runtime")
    missing = await api_harness.client.get("/api/v1/reviewed/jobs", headers=headers)
    assert missing.status_code == 503
    assert missing.json() == {"detail": "reviewed_runtime_unavailable"}

    app.state.reviewed_runtime = object()
    impostor = await api_harness.client.get("/api/v1/reviewed/jobs", headers=headers)
    assert impostor.status_code == 503

    catalog = _catalog()
    worker_only_plan = RuntimeRolePlan(
        profile="reviewed",
        roles=frozenset({"worker"}),
        reviewed_config_id="test.worker-only",
        reviewed_sink_ids=frozenset({DATASET_ID}),
    )
    worker_only_runtime = build_reviewed_runtime(
        metadata_engine=api_harness.metadata_engine,
        catalog=catalog,
        runtime_plan=worker_only_plan,
    )
    app.state.reviewed_runtime = worker_only_runtime
    disabled = await api_harness.client.get("/api/v1/reviewed/jobs", headers=headers)
    assert disabled.status_code == 503
    assert disabled.json() == {"detail": "reviewed_runtime_unavailable"}

    api_harness.runtime.close()
    app.state.reviewed_runtime = api_harness.runtime
    closed = await api_harness.client.get("/api/v1/reviewed/jobs", headers=headers)
    assert closed.status_code == 503
    assert closed.json() == {"detail": "reviewed_runtime_unavailable"}


async def test_job_listing_contains_only_reviewed_safe_fields(api_harness: APIHarness) -> None:
    """The job directory has deterministic ordering and no executable metadata."""
    response = await api_harness.client.get(
        "/api/v1/reviewed/jobs",
        headers=_admin_headers(api_harness),
    )

    assert response.status_code == 200
    assert response.json() == {
        "jobs": [
            {
                "job_id": ENABLED_JOB_ID,
                "version": JOB_VERSION,
                "dataset_id": DATASET_ID,
                "provider": "fixture-provider",
                "model": "daily-bars",
                "enabled": True,
                "reviewed": True,
            },
            {
                "job_id": DISABLED_JOB_ID,
                "version": JOB_VERSION,
                "dataset_id": DATASET_ID,
                "provider": "fixture-provider",
                "model": "disabled-bars",
                "enabled": False,
                "reviewed": True,
            },
        ]
    }


def test_openapi_documents_strict_manual_body_and_safe_responses(api_harness: APIHarness) -> None:
    """The public operation publishes inline schemas with bounded error codes."""
    operation = api_harness.app.openapi()["paths"]["/api/v1/reviewed/runs"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]

    assert request_schema["additionalProperties"] is False
    assert set(request_schema["required"]) == {"job_id", "version", "idempotency_key"}
    assert set(request_schema["properties"]) == {
        "job_id",
        "version",
        "idempotency_key",
        "query",
        "partition",
        "shard",
    }
    assert "$ref" not in request_schema
    assert {
        "202",
        "400",
        "401",
        "403",
        "404",
        "409",
        "413",
        "415",
        "422",
        "503",
    }.issubset(operation["responses"])
    run_operation = api_harness.app.openapi()["paths"]["/api/v1/reviewed/runs/{run_key}"]["get"]
    cancel_operation = api_harness.app.openapi()["paths"]["/api/v1/reviewed/runs/{run_key}/cancel"][
        "post"
    ]
    expected_pattern = r"^[0-9a-f]{64}$"
    for operation_with_run_key in (run_operation, cancel_operation):
        run_key_parameter = next(
            parameter
            for parameter in operation_with_run_key["parameters"]
            if parameter["name"] == "run_key"
        )
        assert run_key_parameter["schema"]["pattern"] == expected_pattern
        assert "Lowercase 64-character hexadecimal SHA-256" in run_key_parameter["description"]
        assert "404" in operation_with_run_key["responses"]


async def test_manual_submit_replays_same_identity_and_conflicts_on_payload_drift(
    api_harness: APIHarness,
) -> None:
    """The dispatcher owns idempotency, including conflict on changed payload."""
    url = "/api/v1/reviewed/runs"
    headers = _admin_headers(api_harness)
    body = _request_body(query={"market": "equity", "limit": 5})

    first = await api_harness.client.post(url, headers=headers, json=body)
    replay = await api_harness.client.post(url, headers=headers, json=body)
    drift = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(query={"market": "equity", "limit": 6}),
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    first_body = first.json()
    replay_body = replay.json()
    assert set(first_body) == {"run_key", "status", "job_id", "version", "created_at"}
    assert first_body["run_key"] == replay_body["run_key"]
    assert first_body["status"] == replay_body["status"] == "queued"
    assert first_body["job_id"] == ENABLED_JOB_ID
    assert first_body["version"] == JOB_VERSION
    assert _RUN_KEY.fullmatch(first_body["run_key"])
    assert drift.status_code == 409
    assert drift.json() == {"detail": "idempotency_conflict"}
    assert _ledger_count(api_harness.metadata_engine) == 1


async def test_dispatcher_errors_are_sanitized_and_leave_ledger_unchanged(
    api_harness: APIHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate expected and unexpected dispatch failures without leaking input."""

    def reject_request(
        _dispatcher: RunDispatcher,
        _job_id: str,
        _version: str,
        _payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        del dry_run
        raise ValueError("private credential must not reach the response")

    monkeypatch.setattr(RunDispatcher, "dispatch_manual", reject_request)
    invalid = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="dispatch-value-error"),
    )

    def fail_backend(
        _dispatcher: RunDispatcher,
        _job_id: str,
        _version: str,
        _payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        del dry_run
        raise RuntimeError("private database credential must not reach the response")

    monkeypatch.setattr(RunDispatcher, "dispatch_manual", fail_backend)
    unavailable = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="dispatch-runtime-error"),
    )

    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "invalid_request"}
    assert "private credential" not in invalid.text
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "reviewed_run_service_unavailable"}
    assert "private database credential" not in unavailable.text
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_invalid_dispatch_receipt_returns_safe_unavailable_without_writes(
    api_harness: APIHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an invalid dry-run receipt instead of exposing or persisting it."""
    invalid_receipt = DispatchReceipt(
        identity=DispatchIdentity(
            run_key="a" * 64,
            run_type="manual",
            job_id=ENABLED_JOB_ID,
            version=JOB_VERSION,
            request_bytes=1,
        ),
        status="dry_run",
        created_at=None,
    )

    def return_invalid_receipt(
        _dispatcher: RunDispatcher,
        _job_id: str,
        _version: str,
        _payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        del dry_run
        return invalid_receipt

    monkeypatch.setattr(RunDispatcher, "dispatch_manual", return_invalid_receipt)
    response = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="invalid-receipt"),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "reviewed_run_service_unavailable"}
    assert "dry_run" not in response.text
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_unknown_and_disabled_jobs_are_rejected_before_ledger_writes(
    api_harness: APIHarness,
) -> None:
    """Unknown jobs return 404 and disabled versions return 409 without writes."""
    headers = _admin_headers(api_harness)
    unknown = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=headers,
        json=_request_body(job_id="not-registered"),
    )
    disabled = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=headers,
        json=_request_body(job_id=DISABLED_JOB_ID),
    )

    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "job_not_found"}
    assert disabled.status_code == 409
    assert disabled.json() == {"detail": "job_not_runnable"}
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_request_size_duplicate_nonfinite_depth_and_field_guards(
    api_harness: APIHarness,
) -> None:
    """Malformed or oversized JSON fails before any ledger write."""
    url = "/api/v1/reviewed/runs"
    headers = {**_admin_headers(api_harness), "Content-Type": "application/json"}
    valid_prefix = b'{"job_id":"fixture-daily","version":"v1","idempotency_key":"raw-body","query":'
    duplicate = valid_prefix + b'{"market":"equity","market":"rates"}}'
    nonfinite = valid_prefix + b'{"number":NaN}}'
    too_deep = valid_prefix + b'{"nested":' * 10 + b"0" + b"}" * 10 + b"}"
    oversized = _json_bytes(_request_body(query={"padding": "x" * (64 * 1024)}))
    extra_field = _json_bytes(_request_body(script="not-allowed"))

    responses = [
        await api_harness.client.post(url, headers=headers, content=duplicate),
        await api_harness.client.post(url, headers=headers, content=nonfinite),
        await api_harness.client.post(url, headers=headers, content=too_deep),
        await api_harness.client.post(url, headers=headers, content=oversized),
        await api_harness.client.post(url, headers=headers, content=extra_field),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422, 413, 422]
    assert all("script" not in response.text for response in responses)
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_execution_and_connection_payloads_are_rejected(api_harness: APIHarness) -> None:
    """CLI-forbidden execution selectors and credential-bearing URLs are refused."""
    url = "/api/v1/reviewed/runs"
    headers = _admin_headers(api_harness)
    script = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(query={"Script": "collect.py"}),
    )
    selector_url = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(partition={"source": "https://example.test/?module=worker"}),
    )
    credential_key = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(query={"access_key": "must-not-be-stored"}),
    )
    passphrase_direct = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(query={"passphrase": "must-not-be-stored"}),
    )
    passphrase_key = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(query={"nested": {"passphrase": "must-not-be-stored"}}),
    )
    passphrase_url = await api_harness.client.post(
        url,
        headers=headers,
        json=_request_body(partition={"source": "https://example.test/?passphrase=secret-value"}),
    )

    assert [
        script.status_code,
        selector_url.status_code,
        credential_key.status_code,
        passphrase_direct.status_code,
        passphrase_key.status_code,
        passphrase_url.status_code,
    ] == [422, 422, 422, 422, 422, 422]
    assert "collect.py" not in script.text
    assert "example.test" not in selector_url.text
    assert "must-not-be-stored" not in credential_key.text
    assert "must-not-be-stored" not in passphrase_direct.text
    assert "must-not-be-stored" not in passphrase_key.text
    assert "secret-value" not in passphrase_url.text
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_run_status_redacts_snapshot_and_internal_failure_text(
    api_harness: APIHarness,
) -> None:
    """A failed ledger row exposes only fixed status fields and failure code."""
    submitted = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(query={"market": "equity"}),
    )
    run_key = submitted.json()["run_key"]
    claim = api_harness.runtime.ledger.claim(
        "fixture-worker",
        datetime.now(UTC),
        timedelta(seconds=30),
    )
    assert claim is not None
    api_harness.runtime.ledger.fail(
        run_key,
        "fixture-worker",
        claim.fencing_token,
        datetime.now(UTC),
        "credential=do-not-return; raw query included",
    )

    response = await api_harness.client.get(
        f"/api/v1/reviewed/runs/{run_key}",
        headers=_admin_headers(api_harness),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "run_key",
        "status",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "failure_code",
    }
    assert body["status"] == "failed"
    assert body["failure_code"] == "run_failed"
    assert "credential" not in response.text
    assert "market" not in response.text
    assert "worker" not in response.text
    assert "lease" not in response.text


async def test_ledger_read_failure_returns_safe_unavailable(
    api_harness: APIHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger exception is translated to a fixed API error."""

    def fail_read(_ledger: RunLedger, _run_key: str) -> RunRecord | None:
        raise RuntimeError("private ledger connection string")

    monkeypatch.setattr(RunLedger, "get", fail_read)
    response = await api_harness.client.get(
        f"/api/v1/reviewed/runs/{'a' * 64}",
        headers=_admin_headers(api_harness),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "reviewed_run_service_unavailable"}
    assert "connection string" not in response.text
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_cancel_fences_queued_run_and_cancelled_replay_is_stable(
    api_harness: APIHarness,
) -> None:
    """Cancellation replay reports the final state without incrementing its fence."""
    submitted = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="cancel-me"),
    )
    run_key = submitted.json()["run_key"]
    url = f"/api/v1/reviewed/runs/{run_key}/cancel"

    first = await api_harness.client.post(url, headers=_admin_headers(api_harness))
    first_record = api_harness.runtime.ledger.get(run_key)
    assert first_record is not None
    replay = await api_harness.client.post(url, headers=_admin_headers(api_harness))
    replay_record = api_harness.runtime.ledger.get(run_key)

    assert first.status_code == 200
    assert first.json()["outcome"] == "cancelled"
    assert first.json()["run"]["status"] == "cancelled"
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "cancelled"
    assert replay.json()["run"]["status"] == "cancelled"
    assert replay_record == first_record
    assert replay_record is not None
    assert replay_record.fencing_token == first_record.fencing_token


async def test_cancel_missing_run_returns_not_found(
    api_harness: APIHarness,
) -> None:
    """A well-shaped but unknown key returns 404 without creating ledger state."""
    response = await api_harness.client.post(
        f"/api/v1/reviewed/runs/{'f' * 64}/cancel",
        headers=_admin_headers(api_harness),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "run_not_found"}
    assert _ledger_count(api_harness.metadata_engine) == 0


async def test_cancel_successful_run_reports_terminal_outcome(api_harness: APIHarness) -> None:
    """A run that succeeded before cancellation remains terminal and unchanged."""
    submitted = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="cancel-after-success"),
    )
    run_key = submitted.json()["run_key"]
    now = datetime.now(UTC)
    claim = api_harness.runtime.ledger.claim("fixture-worker", now, timedelta(seconds=30))
    assert claim is not None
    succeeded = api_harness.runtime.ledger.finish(
        run_key,
        "fixture-worker",
        claim.fencing_token,
        now + timedelta(seconds=1),
    )
    assert succeeded is not None
    assert succeeded.status == "succeeded"

    response = await api_harness.client.post(
        f"/api/v1/reviewed/runs/{run_key}/cancel",
        headers=_admin_headers(api_harness),
    )
    after = api_harness.runtime.ledger.get(run_key)

    assert response.status_code == 200
    assert response.json()["outcome"] == "terminal"
    assert response.json()["run"]["status"] == "succeeded"
    assert after == succeeded


async def test_cancel_failed_run_reports_terminal_outcome_without_error_text(
    api_harness: APIHarness,
) -> None:
    """A previously failed run remains terminal and its stored error stays private."""
    submitted = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="cancel-after-failure"),
    )
    run_key = submitted.json()["run_key"]
    now = datetime.now(UTC)
    claim = api_harness.runtime.ledger.claim("fixture-worker", now, timedelta(seconds=30))
    assert claim is not None
    failed = api_harness.runtime.ledger.fail(
        run_key,
        "fixture-worker",
        claim.fencing_token,
        now + timedelta(seconds=1),
        "private provider credential and error detail",
    )
    assert failed is not None
    assert failed.status == "failed"

    response = await api_harness.client.post(
        f"/api/v1/reviewed/runs/{run_key}/cancel",
        headers=_admin_headers(api_harness),
    )
    after = api_harness.runtime.ledger.get(run_key)

    assert response.status_code == 200
    assert response.json()["outcome"] == "terminal"
    assert response.json()["run"]["status"] == "failed"
    assert response.json()["run"]["failure_code"] == "run_failed"
    assert "provider credential" not in response.text
    assert "error detail" not in response.text
    assert after == failed


async def test_cancel_backend_failure_leaves_record_unchanged(
    api_harness: APIHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cancellation is redacted and leaves the persisted row alone."""
    submitted = await api_harness.client.post(
        "/api/v1/reviewed/runs",
        headers=_admin_headers(api_harness),
        json=_request_body(idempotency_key="cancel-backend-error"),
    )
    run_key = submitted.json()["run_key"]
    before = api_harness.runtime.ledger.get(run_key)
    assert before is not None

    def fail_cancel(
        _ledger: RunLedger,
        _run_key: str,
        _now: datetime,
    ) -> RunRecord | None:
        raise RuntimeError("private fencing token and database secret")

    monkeypatch.setattr(RunLedger, "cancel", fail_cancel)
    response = await api_harness.client.post(
        f"/api/v1/reviewed/runs/{run_key}/cancel",
        headers=_admin_headers(api_harness),
    )
    after = api_harness.runtime.ledger.get(run_key)

    assert response.status_code == 503
    assert response.json() == {"detail": "reviewed_run_service_unavailable"}
    assert "fencing token" not in response.text
    assert "database secret" not in response.text
    assert after == before


async def test_unknown_and_malformed_run_keys_are_not_found(api_harness: APIHarness) -> None:
    """The public path accepts only known lowercase 64-hex run keys."""
    headers = _admin_headers(api_harness)
    unknown = await api_harness.client.get(f"/api/v1/reviewed/runs/{'0' * 64}", headers=headers)
    malformed = await api_harness.client.post(
        "/api/v1/reviewed/runs/not-a-hash/cancel",
        headers=headers,
    )

    assert unknown.status_code == 404
    assert malformed.status_code == 404
    assert unknown.json() == {"detail": "run_not_found"}
    assert malformed.json() == {"detail": "run_not_found"}


def _json_bytes(value: object) -> bytes:
    """Serialize a test JSON body with standards-compliant finite values."""
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
