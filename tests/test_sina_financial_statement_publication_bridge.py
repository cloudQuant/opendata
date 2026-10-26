"""Offline bridge from the recorded Sina FZB response to scoped v2 pages."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
    serialize_sina_fzb_response,
)
from opendata_client.snapshots import SnapshotClient
from sqlalchemy import create_engine

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.providers.sina.financial_statement_contract import SinaFzbQuery
from opendata.models.user import User, UserRole
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.publication_store import PUBLICATION_METADATA, PublicationStore
from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
    RunService,
)

UTC = timezone.utc
DATASET_ID = "sina_fzb_report_observations"
SYMBOL = "600519.SH"
OTHER_SYMBOL = "600001.SH"
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
FIXTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
RETRIEVED_A = datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
RETRIEVED_B = RETRIEVED_A + timedelta(minutes=5)
SIGNING_KEY = b"w11-sina-capture-snapshot-signing-key"

IDENTITY_SEMANTICS = json.loads(
    '{"version":1,"identity_key_encoding":"canonical-json-array-v1","revision":'
    '"latest non-deleted revision per business_key at the pinned publication sequence",'
    '"business_key_fields":["instrument_id","capture_id","statement_code","report_period"],'
    '"ordering_fields":["instrument_id","capture_id","statement_code","report_period"],'
    '"storage_order":[{"field":"business_date","direction":"asc","nulls":"last",'
    '"comparison":"native"},{"field":"business_key","direction":"asc",'
    '"comparison":"binary_lexicographic"}],"temporal_semantics":"none","time_field":null,'
    '"temporal_fields":{"business_date":"null for atemporal table records; not a historical '
    'business date","observed_at":"source observation time","source_published_at":'
    '"source publication time","available_at":"platform availability time",'
    '"event_at":"source event time"}}'
)


def _recorded_body() -> bytes:
    archive = FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == FIXTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    return base64.b64decode(call["content_b64"], validate=True)


def _assert_batch(batch: NormalizedBatch, capture_id: str, retrieved_at: datetime) -> None:
    assert len(batch.records) == 103
    total_ordinals = 0
    marker_count = 0
    for record in batch.records:
        assert record.business_date is record.event_at is None
        assert record.observed_at == retrieved_at
        payload = record.payload
        assert (
            payload["instrument_id"] == SYMBOL
            and payload["capture_id"] == capture_id
            and payload["statement_code"] == "fzb"
        )
        entries = payload["entries"]
        assert isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries)
        source_ordinals = [ordinal for entry in entries for ordinal in entry["source_ordinals"]]
        assert all(type(ordinal) is int for ordinal in source_ordinals)
        assert sorted(source_ordinals) == list(range(len(source_ordinals)))
        total_ordinals += len(source_ordinals)
        marker_count += sum(entry["kind"] == "group_marker" for entry in entries)
    assert len({record.payload["report_period"] for record in batch.records}) == 103
    assert total_ordinals == 14_523
    assert marker_count == 515


def _assert_pages(rows: list[dict[str, object]], capture_id: str, retrieved_at: datetime) -> None:
    assert len(rows) == 103
    assert all(
        row["business_date"] is row["event_at"] is None
        and isinstance(row["observed_at"], str)
        and datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00")) == retrieved_at
        and isinstance(row["payload"], dict)
        and (row["payload"]["instrument_id"], row["payload"]["capture_id"]) == (SYMBOL, capture_id)
        and row["payload"]["report_period"] != row["business_date"]
        for row in rows
    )


def test_w9_fzb_capture_publication_and_snapshot_scope(tmp_path: Path) -> None:
    contract = build_sina_fzb_observation_contract()
    assert contract.dataset_id == DATASET_ID
    contract_registry = DatasetContractRegistry([contract])
    task = TaskDefinition(
        job_id="sina-fzb-observation",
        version="v1",
        dataset_id=DATASET_ID,
        provider="sina",
        model="sina-fzb-source-native-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        enabled=True,
    )
    requests = {
        token: RunRequest(run_type="manual", idempotency_key=token, query={"symbol": SYMBOL})
        for token in ("capture-a", "capture-b")
    }
    run_keys = {
        token: build_run_identity(task, request).idempotency_key
        for token, request in requests.items()
    }
    retrieval_times = {"capture-a": RETRIEVED_A, "capture-b": RETRIEVED_B}
    batches: dict[str, NormalizedBatch] = {}
    body = _recorded_body()

    engine = create_engine(f"sqlite:///{tmp_path / 'sina-bridge.sqlite'}")
    try:
        RUN_LEDGER_METADATA.create_all(engine)
        PUBLICATION_METADATA.create_all(engine)
        ledger = RunLedger(engine)
        store = PublicationStore(engine, cursor_signing_key=SIGNING_KEY)
        writer = ValidatedPublicationWriter(contract_registry)

        def fetch(request: RunRequest, context: RunExecutionContext) -> bytes:
            assert request.idempotency_key in run_keys
            assert context.run_key == run_keys[request.idempotency_key]
            return body

        def normalize(
            raw: object, request: RunRequest, context: RunExecutionContext
        ) -> NormalizedBatch:
            assert isinstance(raw, bytes)
            symbol = request.query["symbol"]
            assert isinstance(symbol, str)
            assert context.run_key == run_keys[request.idempotency_key]
            batch = serialize_sina_fzb_response(
                raw,
                query=SinaFzbQuery(symbol=symbol),
                retrieved_at_utc=retrieval_times[request.idempotency_key],
                capture_id=context.run_key,
            )
            batches[context.run_key] = batch
            return batch

        job = ReviewedJob(
            task=task,
            reviewed=True,
            fetch=fetch,
            normalize=normalize,
            publish=writer,
        )
        service = RunService(
            ledger,
            ReviewedJobRegistry([job]),
        )

        def execute(token: str) -> tuple[str, NormalizedBatch]:
            request = requests[token]
            expected_run_key = run_keys[token]
            assert service.submit(task.job_id, task.version, request).run_key == expected_run_key
            outcome = service.run_once("w11-offline-worker", timedelta(minutes=1))
            assert (
                outcome is not None
                and outcome.record is not None
                and outcome.publication is not None
            )
            assert (outcome.record.run_key, outcome.record.status) == (
                expected_run_key,
                "succeeded",
            )
            assert (outcome.publication.dataset_id, outcome.publication.record_count) == (
                DATASET_ID,
                103,
            )
            assert ledger.get(expected_run_key).status == "succeeded"
            return expected_run_key, batches[expected_run_key]

        app = FastAPI()
        app.include_router(router)
        grants = DatasetGrantRegistry(
            [
                DatasetGrant(
                    dataset_id=DATASET_ID,
                    domain="equity",
                    source_id="sina-fzb",
                    permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
                    purposes=("research",),
                    valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                    valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                    review_evidence="OD204-W11-TEST",
                )
            ]
        )
        principal = Principal(
            user=User(
                id=17,
                username="w11-sina-test",
                email="w11-sina-test@example.test",
                hashed_password="not-a-real-password-hash",
                role=UserRole.USER,
                is_active=True,
            )
        )
        app.dependency_overrides[get_current_principal] = lambda: principal
        app.dependency_overrides[get_dataset_grants] = lambda: grants
        app.dependency_overrides[get_dataset_contracts] = lambda: contract_registry
        app.dependency_overrides[get_publication_store] = lambda: store

        capture_a, batch_a = execute("capture-a")
        _assert_batch(batch_a, capture_a, RETRIEVED_A)
        with (
            TestClient(app) as http_client,
            SnapshotClient(
                "http://testserver", api_key="offline-test-key", http_client=http_client
            ) as client,
        ):

            def select_capture(instrument_id: str, capture_id: str):
                return client.create_snapshot(
                    DATASET_ID,
                    expected_identity_semantics=IDENTITY_SEMANTICS,
                    page_size=64,
                    instrument_id=instrument_id,
                    capture_id=capture_id,
                )

            def read_pages(snapshot):
                return list(
                    client.iter_records(
                        snapshot,
                        expected_identity_semantics=IDENTITY_SEMANTICS,
                    )
                )

            first_snapshot = select_capture(SYMBOL, capture_a)
            assert first_snapshot.record_count == 103
            assert first_snapshot.sequence == 1
            assert first_snapshot.selection is not None
            assert first_snapshot.selection.instrument_id == SYMBOL
            assert first_snapshot.selection.capture_id == capture_a
            first_rows = read_pages(first_snapshot)
            _assert_pages(first_rows, capture_a, RETRIEVED_A)
            expected_a = {record.business_key: dict(record.payload) for record in batch_a.records}
            actual_a = {row["business_key"]: row["payload"] for row in first_rows}
            assert actual_a == expected_a

            wrong_instrument = select_capture(OTHER_SYMBOL, capture_a)
            assert wrong_instrument.record_count == 0
            assert read_pages(wrong_instrument) == []

            capture_b, batch_b = execute("capture-b")
            assert capture_b != capture_a
            _assert_batch(batch_b, capture_b, RETRIEVED_B)
            second_snapshot = select_capture(SYMBOL, capture_b)
            assert second_snapshot.record_count == 103
            assert second_snapshot.sequence == 2
            assert second_snapshot.selection is not None
            assert second_snapshot.selection.capture_id == capture_b
            second_rows = read_pages(second_snapshot)
            _assert_pages(second_rows, capture_b, RETRIEVED_B)
            expected_b = {record.business_key: dict(record.payload) for record in batch_b.records}
            actual_b = {row["business_key"]: row["payload"] for row in second_rows}
            assert actual_b == expected_b
            assert {row["business_key"] for row in first_rows}.isdisjoint(
                row["business_key"] for row in second_rows
            )

            pinned_again = client.get_snapshot(
                first_snapshot.snapshot_id,
                dataset_id=DATASET_ID,
                expected_identity_semantics=IDENTITY_SEMANTICS,
            )
            assert pinned_again.content_sha256 == first_snapshot.content_sha256
            assert pinned_again.sequence == first_snapshot.sequence
            assert read_pages(pinned_again) == first_rows
    finally:
        engine.dispose()
