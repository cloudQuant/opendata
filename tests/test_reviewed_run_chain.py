"""Exercise reviewed process execution through fenced publication and evidence.

Only the small explicitly named fixture function runs in the fresh interpreter.
All source rows are synthetic. The test owns its SQLite database and artifact
root; no network, deployment credentials, or production warehouse is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from sqlalchemy import create_engine, func, select

from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    PublicationStore,
)
from opendata.pipeline.raw_evidence import (
    CapturedRawResponse,
    RawResponseArtifactWriter,
    RawResponseSource,
    request_summary_sha256,
)
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    ReviewedJob,
    RunExecutionContext,
    RunService,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

UTC = timezone.utc
TRADE_DATE = date(2026, 10, 26)
OCCURRENCE = datetime(2026, 10, 26, 16, tzinfo=UTC)
DATASET = "fixture_daily_chain"


def fixture_source_response(parameters: dict[str, JsonValue]) -> JsonValue:
    """Return known synthetic source rows, optionally wait for a failure test."""
    marker = parameters.get("started_path")
    if isinstance(marker, str):
        Path(marker).write_text(str(os.getpid()))
    delay = parameters.get("delay_seconds", 0)
    if isinstance(delay, (int, float)):
        time.sleep(delay)
    return {
        "instrument_id": "FIXTURE:ONE",
        "trade_date": "2026-10-26",
        "close": 12.5,
    }


def _contract() -> DatasetContract:
    """Define a test-only dataset with explicit field and time meanings."""
    return DatasetContract(
        dataset_id=DATASET,
        schema_version="1.0.0",
        semantic_version="fixture-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        filter_fields=("instrument_id",),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id="fixture-source", source="fixture"),
        field_units={"close": "CNY/share"},
        currency="CNY",
        adjustment_basis="raw",
        calendar_id="fixture-calendar",
        timezone="UTC",
        pit_policy="unsupported",
    )


@pytest.fixture
def chain(tmp_path: Path):
    """Own every storage resource and inject the reviewed handlers explicitly."""
    engine = create_engine(f"sqlite:///{tmp_path / 'chain.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    PUBLICATION_METADATA.create_all(engine)
    ledger = RunLedger(engine, clock_policy="database")
    writer = RawResponseArtifactWriter(tmp_path / "raw")
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-daily": fixture_source_response}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=0.05,
    )
    captures: list[CapturedRawResponse] = []
    fetch_calls: list[str] = []
    options: dict[str, object] = {"timeout": 15.0, "corrupt": False}

    def fetch(request: RunRequest, context: RunExecutionContext) -> CapturedRawResponse:
        fetch_calls.append(context.run_key)
        result = executor.run(
            "fixture-daily",
            request.query,
            timeout_seconds=float(options["timeout"]),
            checkpoint=context.checkpoint,
        )
        body = json.dumps(result.value, sort_keys=True, separators=(",", ":")).encode()
        capture = writer.capture(
            [body],
            source=RawResponseSource(
                provider_id="fixture", dataset_id=DATASET, operation_id="fixture-daily"
            ),
            request_summary_sha256=request_summary_sha256(
                {"instrument_id": "FIXTURE:ONE"}, allowed_fields=("instrument_id",)
            ),
            http_status=200,
            retention_class="ephemeral",
            source_history_limit="unknown",
        )
        captures.append(capture)
        if options["corrupt"]:
            capture.artifact_path.write_bytes(b"corrupted")
        return capture

    def normalize(
        raw: object, _request: RunRequest, _context: RunExecutionContext
    ) -> NormalizedBatch:
        assert isinstance(raw, CapturedRawResponse)
        body = raw.artifact_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != raw.manifest.response_sha256:
            raise ValueError("raw evidence integrity check failed")
        row = json.loads(body)
        return NormalizedBatch(
            records=(
                NormalizedRecord(
                    business_key='["FIXTURE:ONE","2026-10-26"]',
                    payload=row,
                    business_date=TRADE_DATE,
                    observed_at=raw.manifest.captured_at_utc,
                ),
            ),
            schema_version="1.0.0",
            semantic_version="fixture-v1",
            unit="per-field",
            adjustment_basis="raw",
            quality={
                "status": "passed",
                "report_id": "synthetic-fixture-schema-v1",
                "issue_count": 0,
            },
            provenance={
                "synthetic": True,
                "raw_response_sha256": raw.manifest.response_sha256,
                "raw_manifest_sha256": hashlib.sha256(raw.manifest_path.read_bytes()).hexdigest(),
            },
        )

    task = TaskDefinition(
        job_id="fixture-job",
        version="v1",
        dataset_id=DATASET,
        provider="fixture",
        model="fixture-daily",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        enabled=True,
    )
    registry = ReviewedTaskCatalog(
        [
            ReviewedJob(
                task=task,
                fetch=fetch,
                normalize=normalize,
                publish=ValidatedPublicationWriter(DatasetContractRegistry([_contract()])),
                reviewed=True,
            )
        ]
    )
    try:
        yield {
            "engine": engine,
            "ledger": ledger,
            "dispatcher": RunDispatcher(ledger, registry),
            "service": RunService(ledger, registry.registry),
            "captures": captures,
            "fetch_calls": fetch_calls,
            "options": options,
        }
    finally:
        engine.dispose()


def _assert_no_publication(chain) -> None:
    """Check both publication metadata and fact versions stayed unchanged."""
    with chain["engine"].connect() as connection:
        assert connection.scalar(select(func.count()).select_from(PUBLICATIONS)) == 0
        assert connection.scalar(select(func.count()).select_from(PUBLICATION_HEADS)) == 0
        assert connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)) == 0


def test_dispatch_process_capture_and_fenced_publication_are_one_idempotent_run(chain) -> None:
    """The real process result remains linked to immutable local evidence."""
    dispatcher = chain["dispatcher"]
    dry = dispatcher.dispatch_scheduled("fixture-job", "v1", occurrence=OCCURRENCE, dry_run=True)
    first = dispatcher.dispatch_scheduled("fixture-job", "v1", occurrence=OCCURRENCE)
    replay = dispatcher.dispatch_scheduled("fixture-job", "v1", occurrence=OCCURRENCE)
    assert dry.identity.run_key == first.identity.run_key == replay.identity.run_key
    assert chain["ledger"].get(first.identity.run_key).status == "queued"

    outcome = chain["service"].run_once("fixture-worker", timedelta(seconds=3))
    assert outcome.record.status == "succeeded", outcome.record.error_text
    assert len(chain["fetch_calls"]) == 1
    assert chain["service"].run_once("fixture-worker", timedelta(seconds=3)) is None
    store = PublicationStore(
        chain["engine"], cursor_signing_key=b"fixture-cursor-signing-key-32bytes"
    )
    publication = store.get_publication(outcome.publication.publication_id)
    capture = chain["captures"][0]
    assert publication.provenance["raw_response_sha256"] == capture.manifest.response_sha256
    assert (
        publication.provenance["raw_manifest_sha256"]
        == hashlib.sha256(capture.manifest_path.read_bytes()).hexdigest()
    )
    snapshot = store.create_snapshot(DATASET)
    page = store.read_page(snapshot.snapshot_id)
    assert len(page.records) == 1
    assert page.records[0].payload["close"] == 12.5
    assert page.records[0].observed_at == capture.manifest.captured_at_utc
    assert page.records[0].business_date == TRADE_DATE


def test_unresponsive_source_times_out_without_publishing(chain) -> None:
    """A killed fetch cannot advance facts or a publication watermark."""
    chain["options"]["timeout"] = 0.5
    queued = chain["dispatcher"].dispatch_manual(
        "fixture-job", "v1", {"idempotency_key": "timeout", "query": {"delay_seconds": 30}}
    )
    started = time.monotonic()
    outcome = chain["service"].run_once("fixture-worker", timedelta(seconds=3))
    assert time.monotonic() - started < 5
    assert outcome.record.status == "failed"
    assert outcome.record.run_key == queued.identity.run_key
    assert outcome.publication is None
    assert chain["captures"] == []
    _assert_no_publication(chain)


def test_raw_evidence_corruption_blocks_publication(chain) -> None:
    """A corrupt local source artifact cannot be normalized and published."""
    chain["options"]["corrupt"] = True
    chain["dispatcher"].dispatch_manual("fixture-job", "v1", {"idempotency_key": "corrupt"})
    outcome = chain["service"].run_once("fixture-worker", timedelta(seconds=3))
    assert outcome.record.status == "failed"
    assert outcome.publication is None
    _assert_no_publication(chain)


def test_cancelled_run_kills_active_source_and_preserves_cancelled_state(chain, tmp_path) -> None:
    """A real cancellation observed by lease checkpoints never publishes facts."""
    marker = tmp_path / "source-started"
    queued = chain["dispatcher"].dispatch_manual(
        "fixture-job",
        "v1",
        {
            "idempotency_key": "cancelled",
            "query": {"started_path": str(marker), "delay_seconds": 30},
        },
    )
    cancelled = threading.Event()

    def cancel_after_source_started() -> None:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if marker.exists():
            chain["ledger"].cancel(queued.identity.run_key, datetime.now(UTC))
            cancelled.set()

    thread = threading.Thread(target=cancel_after_source_started, daemon=True)
    thread.start()
    try:
        outcome = chain["service"].run_once("fixture-worker", timedelta(seconds=3))
    finally:
        thread.join(timeout=11)
    assert not thread.is_alive()
    assert cancelled.is_set()
    assert outcome.record.status == "cancelled"
    assert outcome.publication is None
    assert chain["captures"] == []
    _assert_no_publication(chain)
