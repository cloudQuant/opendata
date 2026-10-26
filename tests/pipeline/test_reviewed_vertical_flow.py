"""Exercise the reviewed stock-daily path across CLI, worker, REST, and coverage.

The subprocess returns a deliberately small synthetic JSON response. This test
proves local orchestration and contract wiring only; it does not contact or
verify any provider or upstream data source.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, func, select

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
)
from opendata.api.published_data import (
    router as published_data_router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.models.user import User, UserRole
from opendata.pipeline.coverage_service import (
    CalendarEvidence,
    CoverageQuery,
    InstrumentCoverageService,
    PublicationObservations,
)
from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.provider_model_codec import _copy_record as child_json_echo
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    PublicationStore,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    create_worker_engine,
)
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
from opendata.pipeline.run_service import (
    NormalizedBatch,
    ReviewedJobRegistry,
    RunExecutionContext,
    RunOutcome,
)
from opendata.pipeline.runtime_roles import build_runtime_role_plan
from opendata.pipeline.stock_daily_job import StockDailyJsonResponse, build_stock_daily_job
from opendata.reviewed_cli import ReviewedCLIApplication, run_reviewed_cli

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from pydantic import JsonValue

UTC = timezone.utc
DATASET_ID = "cn-equity-stock-daily"
SOURCE_ID = "akshare.daily"
SCHEMA_VERSION = "1.0.0"
SEMANTIC_VERSION = "stock-daily-raw-v1"
JOB_ID = "cn-stock-daily-vertical-fixture"
WORKER_ID = "vertical-fixture-worker"
LEASE_TTL = timedelta(seconds=5)
START = date(2026, 10, 26)
END_EXCLUSIVE = date(2026, 10, 28)
OBSERVED_AT = datetime(2026, 10, 27, 8, 15, tzinfo=UTC)


def _contract() -> DatasetContract:
    """Declare the exact stock-daily raw-bar semantics used by the adapter."""
    return DatasetContract(
        dataset_id=DATASET_ID,
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=("market", "symbol"),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id=SOURCE_ID, source=SOURCE),
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
            "amount": "CNY",
        },
        field_currencies={
            "open": "CNY",
            "high": "CNY",
            "low": "CNY",
            "close": "CNY",
            "amount": "CNY",
        },
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
            "amount": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _task(*, enabled: bool = False) -> TaskDefinition:
    """Create the fixed task identity; activation is explicit at registration."""
    return TaskDefinition(
        job_id=JOB_ID,
        version="v1",
        dataset_id=DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        timezone="Asia/Shanghai",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        enabled=enabled,
    )


def _decode_wire_response(value: JsonValue) -> object:
    """Validate the child's JSON envelope before exposing it to the parent."""
    if not isinstance(value, dict) or set(value) != {"body", "status_code", "observed_at"}:
        raise ValueError("fixture_wire_response_invalid")
    body = value["body"]
    status_code = value["status_code"]
    observed_at = value["observed_at"]
    if (
        not isinstance(body, str)
        or type(status_code) is not int
        or not isinstance(observed_at, str)
    ):
        raise ValueError("fixture_wire_response_invalid")
    return {"body": body, "status_code": status_code, "observed_at": observed_at}


def _cli_call(
    application: ReviewedCLIApplication,
    request: Mapping[str, object],
) -> tuple[int, dict[str, object]]:
    """Invoke the production CLI parser with an in-memory JSON request."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_reviewed_cli(
        ["--request", "-"],
        application=application,
        stdin=io.BytesIO(json.dumps(request, separators=(",", ":")).encode()),
        stdout=stdout,
        stderr=stderr,
    )
    output = stdout.getvalue() or stderr.getvalue()
    return code, json.loads(output)


def _manual_request(idempotency_key: str) -> dict[str, object]:
    """Build the CLI-level manual dispatch payload for one single-symbol window."""
    return {
        "schema_version": 1,
        "operation": "manual",
        "job_id": JOB_ID,
        "version": "v1",
        "payload": {
            "query": {
                "symbol": "600519.SH",
                "start_date": START.isoformat(),
                "end_date": date(2026, 10, 27).isoformat(),
            },
            "partition": {},
            "shard": "600519",
            "idempotency_key": idempotency_key,
        },
    }


def _worker_request() -> dict[str, object]:
    """Build one CLI worker-once request matching the explicit adapter binding."""
    return {
        "schema_version": 1,
        "operation": "worker_once",
        "worker": WORKER_ID,
        "lease_seconds": int(LEASE_TTL.total_seconds()),
    }


def _published_app(
    store: PublicationStore,
    contracts: DatasetContractRegistry,
) -> FastAPI:
    """Mount only the published-data router with explicit fixture authorization."""
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=DATASET_ID,
                domain="equity",
                source_id=SOURCE_ID,
                permitted_operations=("data.read", "snapshot.create"),
                purposes=("research",),
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                review_evidence="OD204-TEST",
            ),
        )
    )
    user = User(
        id=7,
        username="vertical-fixture-user",
        email="vertical-fixture@example.test",
        hashed_password=hashlib.sha256(b"vertical-fixture-user").hexdigest(),
        role=UserRole.USER,
        is_active=True,
    )
    app = FastAPI()
    app.include_router(published_data_router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=user)
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


@pytest.mark.asyncio
async def test_reviewed_cli_to_stock_publication_rest_coverage_and_failed_run_fence(
    tmp_path: Path,
) -> None:
    """Prove successful end-to-end wiring and that a bad response cannot advance data."""
    database_path = tmp_path / "vertical-flow.sqlite"
    bootstrap_engine = create_engine(f"sqlite:///{database_path}")
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    PUBLICATION_METADATA.create_all(bootstrap_engine)
    bootstrap_engine.dispose()

    contract = _contract()
    contract_registry = DatasetContractRegistry([contract])
    artifact_writer = RawResponseArtifactWriter(tmp_path / "private-raw")
    unreviewed = build_stock_daily_job(
        _task(),
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        contract_registry=contract_registry,
        artifact_writer=artifact_writer,
        response_source=lambda _query: (_ for _ in ()).throw(
            AssertionError("the template response source must not run")
        ),
    )

    # The child runs a registered JSON copier from the application package. The
    # synthetic provider envelope below still crosses the real Popen protocol,
    # without importing the pytest module in the isolated subprocess.
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-stock-daily-wire": child_json_echo}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.05,
    )
    parameter_modes: list[str] = []

    def build_parameters(request: RunRequest) -> dict[str, JsonValue]:
        request_key = request.idempotency_key
        if not isinstance(request_key, str):
            raise ValueError("fixture_request_key_missing")
        mode = (
            "vertical-failure"
            if request_key == f"sha256-{hashlib.sha256(b'vertical-failure').hexdigest()}"
            else "vertical-success"
        )
        parameter_modes.append(mode)
        body = (
            "not a stock daily JSON envelope"
            if mode == "vertical-failure"
            else json.dumps(
                {
                    "rc": 0,
                    "data": {
                        "code": "600519",
                        "market": 1,
                        "klines": [
                            "2026-10-26,10.0,11.0,11.5,9.5,12,134,20,10,1,2",
                            "2026-10-27,11.0,11.2,11.8,10.7,13,145,12,2,0.2,1",
                        ],
                    },
                },
                separators=(",", ":"),
            )
        )
        return {
            "body": body,
            "status_code": 200,
            "observed_at": OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        }

    process_fetch = ProcessFetchHandler(
        executor,
        "fixture-stock-daily-wire",
        build_parameters,
        _decode_wire_response,
        timeout_seconds=8.0,
    )

    def normalize_decoded_response(
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        """Capture and normalize decoded child JSON through the stock factory."""
        if not isinstance(raw, dict):
            raise ValueError("fixture_wire_response_invalid")
        body = raw.get("body")
        status_code = raw.get("status_code")
        observed_text = raw.get("observed_at")
        if (
            not isinstance(body, str)
            or type(status_code) is not int
            or not isinstance(observed_text, str)
        ):
            raise ValueError("fixture_wire_response_invalid")
        observed_at = datetime.fromisoformat(observed_text.replace("Z", "+00:00"))
        decoded_response = StockDailyJsonResponse(
            body=body.encode("utf-8"),
            status_code=status_code,
            observed_at=observed_at,
        )
        parent_stock_job = build_stock_daily_job(
            _task(),
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            contract_registry=contract_registry,
            artifact_writer=artifact_writer,
            response_source=lambda _query: decoded_response,
        )
        captured = parent_stock_job.fetch(request, context)
        return parent_stock_job.normalize(captured, request, context)

    reviewed_job = replace(
        unreviewed,
        task=_task(enabled=True),
        fetch=process_fetch,
        normalize=normalize_decoded_response,
        reviewed=True,
    )
    registry = ReviewedJobRegistry([reviewed_job])
    catalog = ReviewedTaskCatalog([reviewed_job])
    dispatcher_engine = create_engine(f"sqlite:///{database_path}")
    worker_engine = create_worker_engine(
        database_path,
        database_io_timeout_seconds=0.1,
    )
    persistent_worker = PersistentRunWorker(
        worker_engine,
        registry,
        worker_id=WORKER_ID,
        lease_ttl=LEASE_TTL,
        heartbeat_interval_seconds=0.05,
        database_io_timeout_seconds=0.1,
    )
    dispatcher = RunDispatcher.from_engine(dispatcher_engine, catalog)

    def worker_once(worker: str, ttl: timedelta) -> RunOutcome | None:
        """Adapt the CLI SPI to the worker's fixed identity and run_once API."""
        if worker != WORKER_ID or ttl != LEASE_TTL:
            raise ValueError("fixture_worker_binding_mismatch")
        return persistent_worker.run_once()

    application = ReviewedCLIApplication(
        runtime_plan=build_runtime_role_plan(
            profile="reviewed",
            roles=("api", "worker"),
            reviewed_config_id="test.reviewed-vertical-flow",
            reviewed_sink_ids=(DATASET_ID,),
        ),
        dispatcher=dispatcher,
        worker_once=worker_once,
    )

    read_engine = create_engine(f"sqlite:///{database_path}")
    store = PublicationStore(
        read_engine,
        cursor_signing_key=b"vertical-flow-fixture-cursor-key-32bytes",
    )
    app = _published_app(store, contract_registry)
    try:
        submit_code, submitted = _cli_call(application, _manual_request("vertical-success"))
        assert submit_code == 0
        assert submitted["status"] == "queued"
        success_key = submitted["run_key"]

        worker_code, completed = _cli_call(application, _worker_request())
        submitted_record = persistent_worker.ledger.get(str(submitted["run_key"]))
        assert worker_code == 0, (
            completed,
            submitted_record.error_text if submitted_record is not None else None,
        )
        assert completed == {"status": "succeeded", "run_key": success_key}
        success_record = persistent_worker.ledger.get(str(success_key))
        assert success_record is not None and success_record.status == "succeeded"
        assert parameter_modes == ["vertical-success"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v2/datasets/{DATASET_ID}/records",
                params={
                    "start_date": START.isoformat(),
                    "end_date": END_EXCLUSIVE.isoformat(),
                    "page_size": 10,
                },
            )
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["window"] == {
            "start": START.isoformat(),
            "end": END_EXCLUSIVE.isoformat(),
            "end_exclusive": True,
        }
        assert page["count"] == page["record_count"] == 2
        assert [row["payload"]["close"] for row in page["rows"]] == [11.0, 11.2]
        assert {row["observed_at"] for row in page["rows"]} == {"2026-10-27T08:15:00+00:00"}
        assert page["rows"][0]["publication_id"] == page["publication_id"]
        assert page["rows"][0]["payload"]["volume"] == 1_200.0
        assert page["adjustment_basis"] == "raw"
        assert page["source"]["source_id"] == SOURCE_ID
        assert page["meta"]["snapshot"]["snapshot_id"] == page["snapshot_id"]
        publication = store.get_publication(page["publication_id"])
        assert publication is not None
        assert publication.provenance["source_verification"] == "not_performed"
        assert publication.provenance["source_history_limit"] == "unknown"

        reference_registry = InstrumentReferenceRegistry(
            registry_version="fixture-instrument-v1",
            evidence_ref="fixture:instrument-review",
            lifecycles=(
                InstrumentLifecycle(
                    dataset_id=DATASET_ID,
                    source_id=SOURCE_ID,
                    instrument_id="CN:600519",
                    lifecycle_version="fixture-life-v1",
                    status="active",
                    status_as_of=date(2026, 10, 27),
                    status_evidence_ref="fixture:active-status",
                    known_start_date=START,
                    start_boundary_unknown=False,
                    start_evidence_ref="fixture:listing-boundary",
                ),
            ),
            aliases=(
                InstrumentAlias(
                    dataset_id=DATASET_ID,
                    source_id=SOURCE_ID,
                    alias="600519",
                    instrument_id="CN:600519",
                    alias_version="fixture-symbol-v1",
                    evidence_ref="fixture:symbol-map",
                    valid_from=START,
                    valid_through=date(2026, 10, 27),
                ),
            ),
        )
        snapshot_id = page["snapshot_id"]
        assert isinstance(snapshot_id, str)
        publication_observations = PublicationObservations(
            dataset_id=DATASET_ID,
            source_id=SOURCE_ID,
            instrument_id="CN:600519",
            publication_id=page["publication_id"],
            publication_version=f"sequence-{page['sequence']}",
            evidence_ref=f"api-snapshot:{snapshot_id}",
            scope="business_history",
            observed_dates=tuple(date.fromisoformat(row["business_date"]) for row in page["rows"]),
            scope_start=date.fromisoformat(page["window"]["start"]),
            scope_end_exclusive=date.fromisoformat(page["window"]["end"]),
            scope_evidence_ref=f"api-snapshot-window:{snapshot_id}",
        )
        coverage = InstrumentCoverageService(reference_registry).evaluate(
            CoverageQuery(
                dataset_id=DATASET_ID,
                source_id=SOURCE_ID,
                instrument_identifier="600519",
                start=START,
                end_exclusive=END_EXCLUSIVE,
                identifier_as_of=START,
            ),
            calendar=CalendarEvidence(
                dataset_id=DATASET_ID,
                source_id=SOURCE_ID,
                instrument_id="CN:600519",
                calendar_id="SSE",
                calendar_version="fixture-calendar-v1",
                evidence_ref="fixture:two-open-dates",
                covered_from=START,
                covered_through=date(2026, 10, 27),
                open_dates=(START, date(2026, 10, 27)),
                daily_classification_complete=True,
            ),
            publication=publication_observations,
        )
        assert coverage.status.name == "COMPLETE"
        assert coverage.instrument_id == "CN:600519"
        assert coverage.publication_id == page["publication_id"]
        assert coverage.coverage.expected_dates == (START, date(2026, 10, 27))
        assert coverage.coverage.observed_row_count == 2
        assert coverage.coverage.covered_expected_date_count == 2

        with read_engine.connect() as connection:
            before_failure = (
                connection.scalar(
                    select(PUBLICATION_HEADS.c.latest_sequence).where(
                        PUBLICATION_HEADS.c.dataset_id == DATASET_ID
                    )
                ),
                connection.scalar(
                    select(PUBLICATION_HEADS.c.watermark_json).where(
                        PUBLICATION_HEADS.c.dataset_id == DATASET_ID
                    )
                ),
                connection.scalar(select(func.count()).select_from(PUBLICATIONS)),
                connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)),
            )
        assert before_failure[0] == 1
        assert before_failure[2:] == (1, 2)

        failed_submit_code, failed_submit = _cli_call(
            application, _manual_request("vertical-failure")
        )
        assert failed_submit_code == 0
        assert failed_submit["status"] == "queued"
        failure_key = failed_submit["run_key"]
        failed_worker_code, failed = _cli_call(application, _worker_request())
        assert failed_worker_code == 1
        assert failed == {"status": "failed", "run_key": failure_key}
        failure_record = persistent_worker.ledger.get(str(failure_key))
        assert failure_record is not None and failure_record.status == "failed"
        assert parameter_modes == ["vertical-success", "vertical-failure"]

        with read_engine.connect() as connection:
            after_failure = (
                connection.scalar(
                    select(PUBLICATION_HEADS.c.latest_sequence).where(
                        PUBLICATION_HEADS.c.dataset_id == DATASET_ID
                    )
                ),
                connection.scalar(
                    select(PUBLICATION_HEADS.c.watermark_json).where(
                        PUBLICATION_HEADS.c.dataset_id == DATASET_ID
                    )
                ),
                connection.scalar(select(func.count()).select_from(PUBLICATIONS)),
                connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)),
            )
        assert after_failure == before_failure
    finally:
        read_engine.dispose()
        worker_engine.dispose()
        dispatcher_engine.dispose()
