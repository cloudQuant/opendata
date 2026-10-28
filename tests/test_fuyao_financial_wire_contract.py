"""Offline wire contract from a synthetic reviewed capture to AFI's reader.

This test uses the reviewed Fuyao job with synthetic response bytes and a
temporary SQLite archive. The REST route is real, but its MySQL-only page
loader is replaced by a read-only SELECT from that same test-owned archive.
AFI's reader is imported from an isolated checkout snapshot without executing
``app/__init__.py`` (which imports settings and database wiring), then talks to
the route through HTTPX MockTransport and Starlette TestClient. Nothing contacts
the network or reads project credentials when run with ``--noconftest`` and an
environment allowlist. Coverage is intentionally one synthetic income/annual
capture; the other five statement/cadence scopes are empty.
This test is opt-in: ordinary single-repository runs skip it; the dedicated
cross-repository gate must set ``AFI_WIRE_REQUIRED=1`` and provide
``AFI_CHECKOUT_SNAPSHOT``.
This contract test does not establish real-source
authenticity, source rights, unit correctness, PIT history, full-A-share
coverage, MySQL behavior, or production activation.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from dataclasses import replace
from datetime import date, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import and_, event, select
from sqlalchemy.pool import NullPool


@pytest.fixture
def offline_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep settings resolution and all generated files inside pytest's temp root."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TESTING", "true")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _load_existing_job_test_support() -> ModuleType:
    """Reuse the established synthetic Fuyao bundle, gate and response helpers."""
    path = Path(__file__).with_name("test_fuyao_financial_run_job.py")
    spec = importlib.util.spec_from_file_location(
        "_opendata_fuyao_financial_job_test_support",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("synthetic Fuyao job test support is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _load_afi_reader_types(backend_root: Path) -> tuple[type[Any], type[Any]]:
    """Load only AFI credentials and archive-reader modules, bypassing app setup."""
    app_root = backend_root / "app"
    reader_path = app_root / "services/stock_analysis/opendata_financial_archive.py"
    credentials_path = app_root / "services/market_data/opendata_credentials.py"
    if not reader_path.is_file() or not credentials_path.is_file():
        pytest.fail(
            "AFI checkout snapshot is incomplete: expected the financial archive reader and "
            "OpenData credentials modules under src/backend/app"
        )

    previous = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "app" or name.startswith("app.")
    }
    for name in previous:
        sys.modules.pop(name, None)

    app_package = ModuleType("app")
    app_package.__package__ = "app"
    app_package.__path__ = [str(app_root)]
    sys.modules["app"] = app_package
    try:
        credentials_module = importlib.import_module(
            "app.services.market_data.opendata_credentials"
        )
        reader_module = importlib.import_module(
            "app.services.stock_analysis.opendata_financial_archive"
        )
        return (
            credentials_module.OpendataCredentials,
            reader_module.OpenDataFinancialArchiveReader,
        )
    finally:
        for name in tuple(sys.modules):
            if name == "app" or name.startswith("app."):
                sys.modules.pop(name, None)
        sys.modules.update(previous)


def test_reviewed_fuyao_capture_reaches_afi_archive_reader_contract(
    offline_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Persist one synthetic job capture and consume it through both REST contracts."""
    if os.environ.get("AFI_WIRE_REQUIRED") != "1":
        pytest.skip(
            "cross-repository AFI wire contract is opt-in; only the dedicated gate can "
            "accept it (set AFI_WIRE_REQUIRED=1 with AFI_CHECKOUT_SNAPSHOT)"
        )
    afi_snapshot_value = os.environ.get("AFI_CHECKOUT_SNAPSHOT", "").strip()
    if not afi_snapshot_value:
        pytest.fail(
            "AFI_CHECKOUT_SNAPSHOT must point to an isolated ai-for-investor checkout snapshot"
        )
    afi_snapshot = Path(afi_snapshot_value)
    if not afi_snapshot.is_absolute():
        pytest.fail("AFI_CHECKOUT_SNAPSHOT must be an absolute path")
    backend_root = afi_snapshot / "src/backend"
    if not backend_root.is_dir():
        pytest.fail("AFI_CHECKOUT_SNAPSHOT must contain src/backend")

    support = _load_existing_job_test_support()

    from opendata.data.financial_observation_archive_reader import (
        FinancialObservationArchivePage,
        FinancialObservationArchiveRow,
    )
    from opendata.pipeline import fuyao_financial_process as process_module
    from opendata.pipeline.financial_observation_store import OBSERVATIONS
    from opendata.pipeline.fuyao_financial_run_job import (
        build_fuyao_financial_job,
        build_fuyao_financial_manual_payload,
    )
    from opendata.pipeline.process_executor import (
        FUYAO_PROCESS_API_KEY_ENV,
        FUYAO_PROCESS_BINDING_ENV,
        ProcessExecutionResult,
        ReviewedProcessExecutor,
    )
    from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
    from opendata.pipeline.run_service import RunExecutionContext

    instrument_id = "999999.BJ"
    statement_type = "income"
    periodicity = "annual"
    run_key = "a" * 64
    gate_engine, binding = support._synthetic_binding(offline_runtime)
    request.addfinalizer(gate_engine.dispose)
    warehouse_path = offline_runtime / "test_financial_archive_warehouse.sqlite"
    warehouse = sa.create_engine(
        f"sqlite:///{warehouse_path}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    request.addfinalizer(warehouse.dispose)

    @event.listens_for(warehouse, "connect")
    def enable_foreign_keys(dbapi_connection: Any, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    support._migrate(
        warehouse,
        support._ARCHIVE_MIGRATION,
        "fuyao_financial_wire_archive_migration_test",
    )
    support._install_mock_process_fetcher(
        monkeypatch,
        support._response_body(statement_type, periodicity),
    )
    monkeypatch.setenv(FUYAO_PROCESS_API_KEY_ENV, support._SYNTHETIC_API_KEY)
    monkeypatch.setenv(FUYAO_PROCESS_BINDING_ENV, support._binding_document(binding))

    disabled_task = TaskDefinition(
        job_id="fuyao-financial-observation",
        version="process-v1",
        dataset_id="financial_observation_archive",
        provider="ths",
        model="financial_statement",
        enabled=False,
    )
    preparation_job = build_fuyao_financial_job(disabled_task, deployment_binding=binding)
    active_task = TaskDefinition(
        job_id=disabled_task.job_id,
        version=disabled_task.version,
        dataset_id=disabled_task.dataset_id,
        provider=disabled_task.provider,
        model=disabled_task.model,
        enabled=True,
        schedule=ScheduleMetadata(
            kind="interval",
            expression="PT1H",
            description="Synthetic test only; no scheduler is registered.",
        ),
    )
    job = replace(preparation_job, task=active_task, reviewed=True)
    run_request = RunRequest(
        run_type="manual",
        **build_fuyao_financial_manual_payload(
            instrument_id,
            statement_type=statement_type,
            periodicity=periodicity,
            limit=1,
            idempotency_key="synthetic-wire-contract-run",
        ),
    )
    context = RunExecutionContext(
        run_key=run_key,
        worker="synthetic-wire-contract-worker",
        fencing_token=1,
        _checkpoint=lambda: None,
    )

    def execute_synthetic_child(
        _executor: ReviewedProcessExecutor,
        _binding: object,
        parameters: Any,
        *,
        timeout_seconds: float,
        checkpoint: Any = None,
        cancelled: Any = None,
    ) -> ProcessExecutionResult:
        assert timeout_seconds > 0
        assert cancelled is None
        if checkpoint is not None:
            checkpoint()
        value = process_module.execute_fuyao_financial_request(parameters)
        return ProcessExecutionResult(
            value=value,
            stdout_discarded_bytes=0,
            stderr_discarded_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(ReviewedProcessExecutor, "run_fuyao", execute_synthetic_child)
    raw_capture = job.fetch(run_request, context)
    batch = job.normalize(raw_capture, run_request, context)
    assert batch.commit_payload is not None
    commit_decoder = job.decode_commit_payload
    assert commit_decoder is not None
    prepared = commit_decoder(batch.commit_payload)
    publish_batch = replace(batch, commit_payload=None, commit_context=prepared)
    with warehouse.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        publication = job.publish(connection, active_task, run_request, publish_batch, run_key)
        connection.commit()
    expected_record_count = len(raw_capture.observation_ids)
    assert expected_record_count > 0
    assert publication.record_count == expected_record_count

    from opendata.api import financial_observation_archive as archive_api
    from opendata.api.dependencies import get_current_principal

    def read_test_archive_page(
        *,
        source_id: str,
        instrument_id: str,
        statement_type: str,
        periodicity: str,
        start_report_period: date,
        end_report_period: date,
        page_size: int,
    ) -> dict[str, object]:
        """Use only SELECTs against the synthetic archive in place of MySQL RR."""
        filters = and_(
            OBSERVATIONS.c.source_id == source_id,
            OBSERVATIONS.c.instrument_id == instrument_id,
            OBSERVATIONS.c.statement_type == statement_type,
            OBSERVATIONS.c.periodicity == periodicity,
            OBSERVATIONS.c.report_period >= start_report_period,
            OBSERVATIONS.c.report_period <= end_report_period,
        )
        with warehouse.connect() as connection:
            mappings = (
                connection.execute(
                    select(OBSERVATIONS)
                    .where(filters)
                    .order_by(OBSERVATIONS.c.report_period, OBSERVATIONS.c.item_code)
                    .limit(page_size + 1)
                )
                .mappings()
                .all()
            )
        assert len(mappings) <= page_size
        selected = mappings[:page_size]
        rows = tuple(
            FinancialObservationArchiveRow(
                observation_id=row["observation_id"],
                natural_key_sha256=row["natural_key_sha256"],
                content_sha256=row["content_sha256"],
                source_id=row["source_id"],
                instrument_id=row["instrument_id"],
                statement_type=row["statement_type"],
                periodicity=row["periodicity"],
                report_period=row["report_period"],
                source_disclosure_date=row["source_disclosure_date"],
                observed_at_utc=(
                    row["observed_at_utc"].replace(tzinfo=timezone.utc)
                    if row["observed_at_utc"].tzinfo is None
                    else row["observed_at_utc"]
                ),
                item_code=row["item_code"],
                value_decimal_text=row["value_decimal_text"],
                native_unit=row["native_unit"],
                source_metadata_json=row["source_metadata_json"],
                observation_policy=row["observation_policy"],
            )
            for row in selected
        )
        page = FinancialObservationArchivePage(
            rows=rows,
            next_cursor=None,
        )
        return archive_api._response_payload(
            page,
            source_id=source_id,
            instrument_id=instrument_id,
            statement_type=statement_type,
            periodicity=periodicity,
            start_report_period=start_report_period,
            end_report_period=end_report_period,
        )

    monkeypatch.setattr(archive_api, "_read_archive_page", read_test_archive_page)
    app = FastAPI()
    app.include_router(archive_api.router, prefix="/api/v2/data")
    app.dependency_overrides[get_current_principal] = lambda: SimpleNamespace(
        allows_domain=lambda domain: domain == "financial_statement"
    )

    credentials_type, reader_type = _load_afi_reader_types(backend_root)
    synthetic_api_key = "synthetic-opendata-contract-key"
    api_requests: list[httpx.Request] = []
    with TestClient(app, base_url="https://opendata.example.test") as api_client:

        async def call_local_opendata(request: httpx.Request) -> httpx.Response:
            api_requests.append(request)
            assert request.url.path == "/api/v2/data/financial-statements/archive"
            assert request.headers["X-API-Key"] == synthetic_api_key
            response = await asyncio.to_thread(
                api_client.get,
                request.url.path,
                params=list(request.url.params.multi_items()),
                headers={"X-API-Key": synthetic_api_key},
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

        async def consume_with_afi_reader() -> dict[str, Any]:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(call_local_opendata),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                reader = reader_type(
                    credentials_type(
                        synthetic_api_key,
                        base_url="https://opendata.example.test",
                    ),
                    client=client,
                    allowed_hosts={"opendata.example.test"},
                )
                return await reader.fetch_analysis_evidence(
                    instrument_id=instrument_id,
                    analysis_date=date(2026, 10, 29),
                )

        evidence = asyncio.run(consume_with_afi_reader())

    assert len(api_requests) == 6
    expected_scopes = {
        (scope_statement, scope_periodicity)
        for scope_statement in ("income", "balance", "cashflow")
        for scope_periodicity in ("annual", "quarterly")
    }
    assert {
        (request.url.params["statement_type"], request.url.params["periodicity"])
        for request in api_requests
    } == expected_scopes
    assert {request.url.params["start_report_period"] for request in api_requests} == {"2024-01-01"}
    assert {request.url.params["end_report_period"] for request in api_requests} == {"2026-10-29"}
    assert {request.url.params["source_id"] for request in api_requests} == {"ths_fuyao"}
    assert {request.url.params["instrument_id"] for request in api_requests} == {instrument_id}
    assert {request.url.params["statement_type"] for request in api_requests} == {
        "income",
        "balance",
        "cashflow",
    }
    assert {request.url.params["periodicity"] for request in api_requests} == {
        "annual",
        "quarterly",
    }
    assert all(request.url.params["page_size"] == "199" for request in api_requests)
    assert all(synthetic_api_key not in str(request.url) for request in api_requests)
    assert evidence["status"] == "observed"
    assert evidence["completeness"] == "unknown"
    assert evidence["point_in_time_reconstruction"] == "unavailable"
    assert evidence["period_amount_semantics"] == "unknown"
    assert evidence["eligible_for_decision_or_backtest"] is False
    assert evidence["requested_window"] == {
        "start_report_period": "2024-01-01",
        "end_report_period": "2026-10-29",
    }
    assert len(evidence["queries"]) == len(expected_scopes)
    assert all(query["status"] == "ok" for query in evidence["queries"])
    assert {
        (query["statement_type"], query["periodicity"]): query["record_count"]
        for query in evidence["queries"]
    } == {
        scope: expected_record_count if scope == (statement_type, periodicity) else 0
        for scope in expected_scopes
    }
    assert len(evidence["records"]) == expected_record_count
    record = next(row for row in evidence["records"] if row["value_decimal_text"] == "123.45")
    assert record["source_id"] == "ths_fuyao"
    assert record["instrument_id"] == instrument_id
    assert record["statement_type"] == statement_type
    assert record["periodicity"] == periodicity
    assert record["report_period"] == "2024-12-31"
    assert record["source_disclosure_date"] == "2025-03-31"
    assert record["value_decimal_text"] == "123.45"
    assert record["native_unit"] == "CNY"
    assert record["observed_at_utc"].endswith("Z")
    assert support._SYNTHETIC_API_KEY not in repr(evidence)
