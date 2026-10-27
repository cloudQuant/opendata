"""Isolated end-to-end replay through Sina transport, worker, publication, and REST pages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
)
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
)
from opendata.models.user import User, UserRole
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.process_executor import (
    MAX_RESULT_BYTES,
    MAX_RESULT_TEXT_BYTES,
    ReviewedProcessExecutor,
    ReviewedProcessRegistry,
)
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    PublicationStore,
)
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    create_worker_engine,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
from opendata.pipeline.run_service import ReviewedJob, ReviewedJobRegistry, RunOutcome
from opendata.pipeline.sina_fzb_process_job import (
    _build_sina_fzb_process_parameters,
    _decode_sina_fzb_process_result,
    build_sina_fzb_manual_payload,
    normalize_sina_fzb_capture,
)
from tests.sina_fzb_recorded_transport_handler import (
    ARCHIVE_DIRECTORY,
    FIXTURE_SHA256,
    WITNESS_FILENAME,
    recorded_fixture,
    sina_fzb_recorded_transport_handler,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from sqlalchemy import Table
    from sqlalchemy.engine import Engine

UTC = timezone.utc
DATASET_ID = "sina_fzb_report_observations"
SOURCE_ID = "sina-fzb"
SYMBOL = "600519.SH"
OTHER_SYMBOL = "000001.SZ"
SIGNING_KEY = b"w12-recorded-transport-test-cursor-signing-key"
PROCESS_OPERATION_ID = "test.sina.fzb.recorded-transport"


def _contract_registry() -> DatasetContractRegistry:
    """Return only the exact source-native Sina FZB publication contract."""
    return DatasetContractRegistry([build_sina_fzb_observation_contract()])


def _task() -> TaskDefinition:
    """Build the test-only enabled schedule metadata required by the worker."""
    return TaskDefinition(
        job_id="sina-fzb-capture",
        version="process-v1",
        dataset_id=DATASET_ID,
        provider="sina",
        model="financial_statement",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        enabled=True,
    )


def _request(symbol: str, idempotency_key: str) -> RunRequest:
    """Build a one-symbol manual request with the production payload builder."""
    return RunRequest(
        run_type="manual",
        **build_sina_fzb_manual_payload(symbol, idempotency_key=idempotency_key),
    )


def _worker(database_path: Path) -> tuple[PersistentRunWorker, Engine]:
    """Create the real process worker and publication store on disposable SQLite."""
    bootstrap_engine = create_engine(f"sqlite:///{database_path}")
    try:
        RUN_LEDGER_METADATA.create_all(bootstrap_engine)
        PUBLICATION_METADATA.create_all(bootstrap_engine)
    finally:
        bootstrap_engine.dispose()

    worker_engine = create_worker_engine(
        database_path,
        database_io_timeout_seconds=0.1,
    )
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({PROCESS_OPERATION_ID: sina_fzb_recorded_transport_handler}),
        max_result_bytes=MAX_RESULT_BYTES,
        max_result_text_bytes=MAX_RESULT_TEXT_BYTES,
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.1,
        shutdown_grace_seconds=0.1,
    )
    fetch = ProcessFetchHandler(
        executor,
        PROCESS_OPERATION_ID,
        _build_sina_fzb_process_parameters,
        _decode_sina_fzb_process_result,
        timeout_seconds=25.0,
    )
    job = ReviewedJob(
        task=_task(),
        fetch=fetch,
        normalize=normalize_sina_fzb_capture,
        publish=ValidatedPublicationWriter(_contract_registry()),
        reviewed=True,
    )
    worker = PersistentRunWorker(
        worker_engine,
        ReviewedJobRegistry([job]),
        worker_id="sina-recorded-transport-e2e",
        lease_ttl=timedelta(seconds=3),
        heartbeat_interval_seconds=0.1,
        database_io_timeout_seconds=0.1,
    )
    return worker, worker_engine


def _run_once_with_child_environment_allowlist(
    worker: PersistentRunWorker,
) -> RunOutcome | None:
    """Give the real child only the five explicit test settings it needs."""
    inherited_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(
        {
            "APP_ENV": "testing",
            "TESTING": "true",
            "REDIS_URL": "",
            "SENTRY_DSN": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    try:
        return worker.run_once()
    finally:
        os.environ.clear()
        os.environ.update(inherited_environment)


def _publication_count(engine: Engine, table: Table) -> int:
    """Count rows in one explicitly supplied table on the test-owned engine."""
    with engine.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(table)) or 0)


def test_recorded_sina_response_traverses_transport_worker_publication_and_rest_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the exact governed child capture against the immutable local recording."""
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "sina-recorded-transport.sqlite"
    database_path.touch()
    worker, worker_engine = _worker(database_path)
    store = PublicationStore(worker_engine, cursor_signing_key=SIGNING_KEY)
    request = _request(SYMBOL, "recorded-transport-positive")

    try:
        submitted = worker.submit("sina-fzb-capture", "process-v1", request)
        outcome = _run_once_with_child_environment_allowlist(worker)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.run_key == submitted.run_key
        assert outcome.record.status == "succeeded"
        assert outcome.publication is not None
        assert outcome.publication.dataset_id == DATASET_ID
        assert outcome.publication.record_count == 103

        fixture_call, fixture_body = recorded_fixture()
        witness_path = tmp_path / WITNESS_FILENAME
        assert witness_path.is_file()
        assert stat.S_IMODE(witness_path.stat().st_mode) == 0o600
        witness = json.loads(witness_path.read_text(encoding="utf-8"))
        assert witness["fixture_sha256"] == FIXTURE_SHA256
        assert witness["child_environment_keys"] == [
            "APP_ENV",
            "PYTHONDONTWRITEBYTECODE",
            "REDIS_URL",
            "SENTRY_DSN",
            "TESTING",
        ]
        assert witness["response_sha256"] == hashlib.sha256(fixture_body).hexdigest()
        assert witness["network_fallbacks_blocked"] == []
        assert len(witness["calls"]) == 1
        transport_call = witness["calls"][0]
        assert transport_call["accepted"] is True
        assert transport_call["method"] == fixture_call["method"] == "GET"
        assert transport_call["url"] == (
            "https://quotes.sina.cn/cn/api/openapi.php/"
            "CompanyFinanceService.getFinanceReport2022?"
            "paperCode=sh600519&source=fzb&type=0&page=1&num=1000"
        )
        assert transport_call["params"] == [
            ["paperCode", "sh600519"],
            ["source", "fzb"],
            ["type", "0"],
            ["page", "1"],
            ["num", "1000"],
        ]
        assert transport_call["accept_encoding"] == "identity"
        assert transport_call["authorization_present"] is False

        archive = witness["archive"]
        assert isinstance(archive, dict)
        assert set(archive) == {
            "artifact_path",
            "manifest_path",
            "request_summary_sha256",
            "response_sha256",
        }
        assert archive["response_sha256"] == hashlib.sha256(fixture_body).hexdigest()
        archive_root = tmp_path / ARCHIVE_DIRECTORY
        artifact_path = (tmp_path / archive["artifact_path"]).resolve()
        manifest_path = (tmp_path / archive["manifest_path"]).resolve()
        assert artifact_path.is_relative_to(archive_root.resolve())
        assert manifest_path.is_relative_to(archive_root.resolve())
        assert artifact_path.read_bytes() == fixture_body
        assert len(fixture_body) <= MAX_RAW_RESPONSE_BYTES
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["response_sha256"] == hashlib.sha256(fixture_body).hexdigest()
        assert manifest["response_size_bytes"] == len(fixture_body)
        assert manifest["retention_class"] == "unknown"
        assert manifest["source_history_limit"] == "unknown"
        assert manifest["request_summary_sha256"] == archive["request_summary_sha256"]

        publication = store.get_publication(outcome.publication.publication_id)
        assert publication is not None and publication.record_count == 103
        assert publication.sequence == 1
        assert _publication_count(worker_engine, PUBLICATIONS) == 1
        assert _publication_count(worker_engine, PUBLICATION_HEADS) == 1
        assert _publication_count(worker_engine, PUBLISHED_FACT_VERSIONS) == 103

        grants = DatasetGrantRegistry(
            [
                DatasetGrant(
                    dataset_id=DATASET_ID,
                    domain="equity",
                    source_id=SOURCE_ID,
                    permitted_operations=(
                        "data.read",
                        "snapshot.create",
                        "snapshot.read",
                    ),
                    purposes=("research",),
                    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                    review_evidence="OD204-W12-TEST",
                )
            ]
        )
        principal = Principal(
            user=User(
                id=204,
                username="sina-recorded-test",
                email="sina-recorded-test@example.test",
                hashed_password="not-a-real-password-hash",
                role=UserRole.USER,
                is_active=True,
            )
        )
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_dataset_grants] = lambda: grants
        app.dependency_overrides[get_dataset_contracts] = _contract_registry
        app.dependency_overrides[get_publication_store] = lambda: store

        with TestClient(app) as http_client:
            first_response = http_client.get(
                f"/api/v2/datasets/{DATASET_ID}/records",
                params={
                    "publication_id": outcome.publication.publication_id,
                    "page_size": 64,
                    "instrument_id": SYMBOL,
                    "capture_id": submitted.run_key,
                },
            )
            assert first_response.status_code == 200
            first = first_response.json()
            assert first["publication_id"] == outcome.publication.publication_id
            assert first["record_count"] == 103
            assert first["sequence"] == 1
            assert first["page_size"] == 64
            assert first["selection"] == {
                "instrument_id": SYMBOL,
                "capture_id": submitted.run_key,
            }
            assert first["pit_policy"] == "unsupported"
            assert len(first["rows"]) == 64
            assert first["count"] == 64
            assert first["has_more"] is True
            assert isinstance(first["next_cursor"], str)

            second_response = http_client.get(
                f"/api/v2/datasets/{DATASET_ID}/records",
                params={
                    "snapshot_id": first["snapshot_id"],
                    "cursor": first["next_cursor"],
                },
            )
            assert second_response.status_code == 200
            second = second_response.json()
            assert second["snapshot_id"] == first["snapshot_id"]
            assert second["publication_id"] == first["publication_id"]
            assert second["sequence"] == first["sequence"]
            assert second["meta"]["snapshot"] == first["meta"]["snapshot"]
            assert len(second["rows"]) == 39
            assert second["count"] == 39
            assert second["has_more"] is False
            assert second["next_cursor"] is None

        rows = first["rows"] + second["rows"]
        assert len(rows) == 103
        assert len({row["business_key"] for row in rows}) == 103
        assert all(row["publication_id"] == outcome.publication.publication_id for row in rows)
        assert all(row["publication_sequence"] == 1 for row in rows)
        assert all(row["business_date"] is None for row in rows)
        assert all(row["payload"]["instrument_id"] == SYMBOL for row in rows)
        assert all(row["payload"]["capture_id"] == submitted.run_key for row in rows)
    finally:
        worker_engine.dispose()


def test_recorded_replay_rejects_another_valid_symbol_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never reuse a recording for a different canonical instrument query."""
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "sina-recorded-transport-mismatch.sqlite"
    database_path.touch()
    worker, worker_engine = _worker(database_path)

    try:
        request = _request(OTHER_SYMBOL, "recorded-transport-negative")
        submitted = worker.submit("sina-fzb-capture", "process-v1", request)
        outcome = _run_once_with_child_environment_allowlist(worker)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.run_key == submitted.run_key
        assert outcome.record.status == "failed"
        assert outcome.publication is None
        assert outcome.record.error_text == "execution_failed:ProcessExecutionHandlerError"

        witness_path = tmp_path / WITNESS_FILENAME
        assert witness_path.is_file()
        witness = json.loads(witness_path.read_text(encoding="utf-8"))
        assert witness["fixture_sha256"] == FIXTURE_SHA256
        assert witness["child_environment_keys"] == [
            "APP_ENV",
            "PYTHONDONTWRITEBYTECODE",
            "REDIS_URL",
            "SENTRY_DSN",
            "TESTING",
        ]
        assert len(witness["calls"]) == 1
        rejected = witness["calls"][0]
        assert rejected["accepted"] is False
        assert rejected["url"].endswith("paperCode=sz000001&source=fzb&type=0&page=1&num=1000")
        assert rejected["authorization_present"] is False
        assert witness["network_fallbacks_blocked"] == []
        assert witness["archive"] is None
        assert not (tmp_path / ARCHIVE_DIRECTORY).exists()

        with worker_engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(PUBLICATIONS)) == 0
            assert connection.scalar(select(func.count()).select_from(PUBLICATION_HEADS)) == 0
            assert connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)) == 0
    finally:
        worker_engine.dispose()
