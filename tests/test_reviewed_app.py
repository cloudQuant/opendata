"""Tests for the explicit reviewed API composition root."""

from __future__ import annotations

import io
import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, cast

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opendata import reviewed_app as reviewed_app_module
from opendata.reviewed_app import create_reviewed_app

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fastapi import FastAPI
    from opendata.pipeline.reviewed_runtime import ReviewedRuntime
    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

    from opendata.pipeline.reviewed_worker import ResultDecoder
    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
    from opendata.pipeline.run_identity import RunRequest, TaskDefinition
    from opendata.pipeline.run_service import (
        NormalizedBatch,
        NormalizeHandler,
        PublicationReceipt,
        PublishHandler,
        RunExecutionContext,
    )
    from opendata.pipeline.runtime_roles import RuntimeRole

_FIXTURE_JOB_ID = "synthetic-daily"
_FIXTURE_VERSION = "v1"
_FIXTURE_DATASET = "synthetic_fixture_daily"
_FIXTURE_OPERATION = "synthetic-fixture-fetch"
_FIXTURE_METADATA = MetaData()
_FIXTURE_FACTS = Table(
    "reviewed_app_fixture_facts",
    _FIXTURE_METADATA,
    Column("business_key", String(255), primary_key=True),
    Column("business_date", String(10), nullable=False),
    Column("close_value", Integer, nullable=False),
)


class _TrackingAsyncSession(AsyncSession):
    """Count closes so the explicit auth session override has a lifecycle test."""

    close_calls = 0

    async def close(self) -> None:
        """Record closure and delegate to SQLAlchemy."""
        type(self).close_calls += 1
        await super().close()


def _make_runtime(
    metadata_engine: Engine,
    *,
    catalog: ReviewedTaskCatalog | None = None,
    roles: set[RuntimeRole] | None = None,
) -> ReviewedRuntime:
    """Build a runtime from an explicit Engine and role selection."""
    from opendata.pipeline.reviewed_runtime import build_reviewed_runtime

    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
    from opendata.pipeline.runtime_roles import build_runtime_role_plan

    enabled_roles = roles or {"api"}
    sink_ids = {_FIXTURE_DATASET} if enabled_roles & {"worker", "scheduler"} else set()
    plan = build_runtime_role_plan(
        profile="reviewed",
        roles=enabled_roles,
        reviewed_config_id="reviewed-app-test",
        reviewed_sink_ids=sink_ids,
    )
    return build_reviewed_runtime(
        metadata_engine=metadata_engine,
        catalog=catalog or ReviewedTaskCatalog([]),
        runtime_plan=plan,
    )


def _make_synthetic_catalog() -> ReviewedTaskCatalog:
    """Create one statically injected synthetic task and process-backed job."""
    from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
    from opendata.pipeline.provider_model_codec import _copy_record
    from opendata.pipeline.reviewed_worker import ProcessFetchHandler
    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
    from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
    from opendata.pipeline.run_service import ReviewedJob

    task = TaskDefinition(
        job_id=_FIXTURE_JOB_ID,
        version=_FIXTURE_VERSION,
        dataset_id=_FIXTURE_DATASET,
        provider="synthetic",
        model="synthetic-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * mon-fri"),
        enabled=True,
    )
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({_FIXTURE_OPERATION: _synthetic_fetch}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.05,
    )
    fetch = ProcessFetchHandler(
        executor,
        _FIXTURE_OPERATION,
        _build_synthetic_parameters,
        cast("ResultDecoder", _copy_record),
        timeout_seconds=5.0,
    )
    job = ReviewedJob(
        task=task,
        fetch=fetch,
        normalize=cast("NormalizeHandler", _normalize_synthetic),
        publish=cast("PublishHandler", _publish_synthetic),
        reviewed=True,
    )
    return ReviewedTaskCatalog([job])


def _synthetic_fetch(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Return deterministic fixture data from a JSON-only child request."""
    query = parameters.get("query")
    if not isinstance(query, dict):
        raise ValueError("fixture_query_required")
    instrument = query.get("instrument")
    business_date = query.get("business_date")
    if not isinstance(instrument, str) or not isinstance(business_date, str):
        raise ValueError("fixture_query_invalid")
    return {"instrument": instrument, "business_date": business_date, "close": 12}


def _build_synthetic_parameters(request: RunRequest) -> dict[str, JsonValue]:
    """Copy the validated query into a JSON-only process request."""
    return {"query": dict(request.query)}


def _normalize_synthetic(
    raw: object,
    _request: RunRequest,
    _context: RunExecutionContext,
) -> NormalizedBatch:
    """Convert synthetic process output into one typed normalized fact."""
    from opendata.pipeline.run_service import NormalizedBatch, NormalizedRecord

    if not isinstance(raw, dict):
        raise ValueError("fixture_response_invalid")
    instrument = raw.get("instrument")
    business_date = raw.get("business_date")
    close_value = raw.get("close")
    if (
        not isinstance(instrument, str)
        or not isinstance(business_date, str)
        or isinstance(close_value, bool)
        or not isinstance(close_value, int)
    ):
        raise ValueError("fixture_response_invalid")
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=f"{instrument}:{business_date}",
                payload={"close": close_value},
                business_date=date.fromisoformat(business_date),
            ),
        ),
        schema_version="1",
        semantic_version="synthetic-fixture-v1",
        unit="CNY/share",
        adjustment_basis="raw",
        quality={"accepted_rows": 1},
        provenance={"source": "synthetic-process-fixture"},
    )


def _publish_synthetic(
    connection: Connection,
    task: TaskDefinition,
    _request: RunRequest,
    batch: NormalizedBatch,
    _idempotency_key: str,
) -> PublicationReceipt:
    """Write the synthetic result through the fenced ledger connection."""
    from opendata.pipeline.run_service import PublicationReceipt

    for row in batch.records:
        close_value = row.payload.get("close")
        if isinstance(close_value, bool) or not isinstance(close_value, int):
            raise ValueError("fixture_close_invalid")
        connection.execute(
            _FIXTURE_FACTS.insert().values(
                business_key=row.business_key,
                business_date=row.business_date.isoformat() if row.business_date else "",
                close_value=close_value,
            )
        )
    return PublicationReceipt(
        publication_id="synthetic-fixture-publication",
        dataset_id=task.dataset_id,
        record_count=len(batch.records),
    )


def _route_allowlist(app: FastAPI) -> set[tuple[str, frozenset[str]]]:
    """Return method/path pairs for the actual Starlette route table."""
    return {
        (route.path, frozenset(route.methods or ()))
        for route in app.routes
        if isinstance(route, APIRoute)
    }


def _expected_routes() -> set[tuple[str, frozenset[str]]]:
    """Declare the only routes this composition root may mount."""
    return {
        ("/api/v1/auth/register", frozenset({"POST"})),
        ("/api/v1/auth/login", frozenset({"POST"})),
        ("/api/v1/auth/refresh", frozenset({"POST"})),
        ("/api/v1/auth/logout", frozenset({"POST"})),
        ("/api/v1/auth/me", frozenset({"GET"})),
        ("/api/v1/auth/change-password", frozenset({"POST"})),
        ("/api/v1/reviewed/jobs", frozenset({"GET"})),
        ("/api/v1/reviewed/runs", frozenset({"POST"})),
        ("/api/v1/reviewed/runs/{run_key}", frozenset({"GET"})),
        ("/api/v1/reviewed/runs/{run_key}/cancel", frozenset({"POST"})),
    }


def test_invalid_runtime_is_rejected_before_fastapi_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid runtime cannot trigger app or route construction."""

    def unexpected_app_construction(**_kwargs: object) -> FastAPI:
        raise AssertionError("FastAPI must not be constructed for an invalid runtime")

    monkeypatch.setattr(reviewed_app_module, "FastAPI", unexpected_app_construction)
    with pytest.raises(TypeError, match="exact ReviewedRuntime"):
        create_reviewed_app(
            cast("ReviewedRuntime", object()),
            auth_session_factory=cast(
                "async_sessionmaker[AsyncSession]",
                object(),
            ),
        )


@pytest.mark.asyncio
async def test_api_disabled_or_closed_runtime_is_rejected() -> None:
    """The factory requires an open reviewed runtime with its API role enabled."""
    from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime

    from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
    from opendata.pipeline.runtime_roles import build_runtime_role_plan

    engine = create_engine("sqlite://")
    auth_engine = create_async_engine("sqlite+aiosqlite://")
    try:
        scheduler_plan = build_runtime_role_plan(
            profile="reviewed",
            roles={"scheduler"},
            reviewed_config_id="scheduler-only",
            reviewed_sink_ids={_FIXTURE_DATASET},
        )
        runtime = build_reviewed_runtime(
            metadata_engine=engine,
            catalog=ReviewedTaskCatalog([]),
            runtime_plan=scheduler_plan,
        )
        factory = async_sessionmaker(auth_engine, expire_on_commit=False)
        with pytest.raises(ValueError, match="API role"):
            create_reviewed_app(runtime, auth_session_factory=factory)

        api_plan = build_runtime_role_plan(
            profile="reviewed",
            roles={"api"},
            reviewed_config_id="api-only",
        )
        closed_runtime = build_reviewed_runtime(
            metadata_engine=engine,
            catalog=ReviewedTaskCatalog([]),
            runtime_plan=api_plan,
        )
        assert type(closed_runtime) is ReviewedRuntime
        closed_runtime.close()
        with pytest.raises(ValueError, match="closed"):
            create_reviewed_app(closed_runtime, auth_session_factory=factory)
    finally:
        engine.dispose()
        await auth_engine.dispose()


@pytest.mark.asyncio
async def test_app_route_allowlist_and_borrowed_runtime_lifecycle(tmp_path: Path) -> None:
    """Only reviewed/auth routes mount, and app shutdown clears state without closing inputs."""
    engine = create_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    auth_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    disposed: list[bool] = []
    event.listen(engine, "engine_disposed", lambda _engine: disposed.append(True))
    try:
        runtime = _make_runtime(engine)
        factory = async_sessionmaker(auth_engine, expire_on_commit=False)
        app = create_reviewed_app(runtime, auth_session_factory=factory)

        assert _route_allowlist(app) == _expected_routes()
        assert not any(
            path == "/api" or (path.startswith("/api/") and not path.startswith("/api/v1/"))
            for path, _ in _route_allowlist(app)
        )
        assert app.state.reviewed_runtime is runtime
        async with app.router.lifespan_context(app):
            assert app.state.reviewed_runtime is runtime
            assert runtime.closed is False
        assert app.state.reviewed_runtime is None
        assert runtime.closed is False
        assert disposed == []
        async with auth_engine.connect() as connection:
            result = await connection.exec_driver_sql("SELECT 1")
            assert result.scalar_one() == 1
    finally:
        engine.dispose()
        await auth_engine.dispose()


@pytest.mark.asyncio
async def test_reviewed_api_auth_and_synthetic_operator_vertical(
    tmp_path: Path,
) -> None:
    """A JWT admin queues and executes one synthetic run through the real worker path."""
    from sqlalchemy.ext.asyncio import async_sessionmaker as make_async_sessionmaker

    from opendata.core.security import create_access_token
    from opendata.models.user import User, UserRole
    from opendata.pipeline.reviewed_worker import create_worker_engine
    from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
    from opendata.reviewed_cli import run_reviewed_cli

    metadata_path = tmp_path / "reviewed.sqlite"
    bootstrap_engine = create_engine(f"sqlite:///{metadata_path}")
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    _FIXTURE_METADATA.create_all(bootstrap_engine)
    bootstrap_engine.dispose()

    worker_engine = create_worker_engine(
        metadata_path,
        database_io_timeout_seconds=0.1,
    )
    auth_path = tmp_path / "auth.sqlite"
    auth_engine = create_async_engine(f"sqlite+aiosqlite:///{auth_path}")
    try:
        async with auth_engine.begin() as connection:
            user_table = cast("Table", User.__table__)
            await connection.run_sync(
                lambda sync_connection: user_table.create(sync_connection, checkfirst=True)
            )
        factory = cast(
            "async_sessionmaker[AsyncSession]",
            make_async_sessionmaker(
                auth_engine,
                class_=_TrackingAsyncSession,
                expire_on_commit=False,
            ),
        )
        async with factory() as session:
            admin = User(
                username="reviewed-admin",
                email="reviewed-admin@example.invalid",
                hashed_password="test-only-hash",
                role=UserRole.ADMIN,
                is_active=True,
            )
            regular_user = User(
                username="reviewed-user",
                email="reviewed-user@example.invalid",
                hashed_password="test-only-hash",
                role=UserRole.USER,
                is_active=True,
            )
            session.add_all([admin, regular_user])
            await session.commit()
            admin_id = admin.id
            regular_user_id = regular_user.id
        _TrackingAsyncSession.close_calls = 0

        catalog = _make_synthetic_catalog()
        runtime = _make_runtime(
            worker_engine,
            catalog=catalog,
            roles={"api", "worker"},
        )
        app = create_reviewed_app(runtime, auth_session_factory=factory)
        token = create_access_token(
            {"sub": str(admin_id), "email": "reviewed-admin@example.invalid"}
        )
        regular_token = create_access_token(
            {"sub": str(regular_user_id), "email": "reviewed-user@example.invalid"}
        )
        cli_application = runtime.to_cli_application(
            worker_engine=worker_engine,
            worker_id="reviewed-app-test-worker",
            lease_ttl=timedelta(seconds=3),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.1,
        )
        body = {
            "job_id": _FIXTURE_JOB_ID,
            "version": _FIXTURE_VERSION,
            "idempotency_key": "manual-fixture-001",
            "query": {"instrument": "FIXTURE:ONE", "business_date": "2026-10-27"},
            "partition": {},
        }
        headers = {"Authorization": f"Bearer {token}"}

        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://reviewed.test",
            ) as client,
        ):
            jobs = await client.get("/api/v1/reviewed/jobs", headers=headers)
            assert jobs.status_code == 200
            assert jobs.json()["jobs"][0]["job_id"] == _FIXTURE_JOB_ID

            user_denied = await client.get(
                "/api/v1/reviewed/jobs",
                headers={"Authorization": f"Bearer {regular_token}"},
            )
            assert user_denied.status_code == 403
            api_key_denied = await client.get(
                "/api/v1/reviewed/jobs",
                headers={"X-API-Key": "od_fixture_rejected"},
            )
            assert api_key_denied.status_code == 401

            queued = await client.post(
                "/api/v1/reviewed/runs",
                headers=headers,
                json=body,
            )
            assert queued.status_code == 202
            queued_again = await client.post(
                "/api/v1/reviewed/runs",
                headers=headers,
                json=body,
            )
            assert queued_again.status_code == 202
            run_key = queued.json()["run_key"]
            assert queued_again.json()["run_key"] == run_key
            assert queued.json()["status"] == "queued"

            cli_stdout = io.StringIO()
            cli_stderr = io.StringIO()
            exit_code = run_reviewed_cli(
                ["--request", "-"],
                application=cli_application,
                stdin=io.BytesIO(
                    b'{"schema_version":1,"operation":"worker_once",'
                    b'"worker":"reviewed-app-test-worker","lease_seconds":3}'
                ),
                stdout=cli_stdout,
                stderr=cli_stderr,
            )
            assert exit_code == 0
            assert cli_stderr.getvalue() == ""
            assert json.loads(cli_stdout.getvalue()) == {
                "run_key": run_key,
                "status": "succeeded",
            }

            status_response = await client.get(
                f"/api/v1/reviewed/runs/{run_key}",
                headers=headers,
            )
            assert status_response.status_code == 200
            assert status_response.json()["status"] == "succeeded"

            cancel = await client.post(
                f"/api/v1/reviewed/runs/{run_key}/cancel",
                headers=headers,
            )
            assert cancel.status_code == 200
            assert cancel.json()["outcome"] == "terminal"
            assert cancel.json()["run"]["status"] == "succeeded"
            cancel_again = await client.post(
                f"/api/v1/reviewed/runs/{run_key}/cancel",
                headers=headers,
            )
            assert cancel_again.status_code == 200
            assert cancel_again.json()["outcome"] == "terminal"
            assert cancel_again.json()["run"]["status"] == "succeeded"

            with worker_engine.connect() as connection:
                fact = connection.execute(
                    select(_FIXTURE_FACTS.c.business_key, _FIXTURE_FACTS.c.close_value)
                ).one()
            assert fact == ("FIXTURE:ONE:2026-10-27", 12)
            assert _TrackingAsyncSession.close_calls >= 6

            runtime.close()
            closed_runtime_response = await client.get(
                "/api/v1/reviewed/jobs",
                headers=headers,
            )
            assert closed_runtime_response.status_code == 503
            assert closed_runtime_response.json()["detail"] == "reviewed_runtime_unavailable"

        assert app.state.reviewed_runtime is None
    finally:
        worker_engine.dispose()
        await auth_engine.dispose()
