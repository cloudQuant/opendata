"""Offline LRB bridge from a synthetic source response to scoped REST pages.

The test uses the real validated publication writer and ASGI routes with a
temporary SQLite file. Authentication is replaced with a fake principal so
this verifies dataset-grant enforcement after principal injection; it does not
verify production bearer-token or API-key authentication.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
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
from opendata.data.platform_contracts import SourceIdentity
from opendata.data.providers.sina.lrb_statement_parser import parse_sina_lrb_response
from opendata.data.providers.sina.lrb_statement_process import (
    SinaLrbQuery,
    sina_lrb_request_summary_sha256,
)
from opendata.data.providers.sina.lrb_statement_publication import (
    SINA_LRB_DATASET_ID,
    build_sina_lrb_observation_contract,
    reconstruct_sina_lrb_source_document,
)
from opendata.models.user import User, UserRole
from opendata.pipeline.contract_publication import ValidatedPublicationWriter
from opendata.pipeline.publication_store import PUBLICATION_METADATA, PublicationStore
from opendata.pipeline.reviewed_worker import ProcessFetchHandler
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
from opendata.pipeline.sina_lrb_process_job import (
    SinaLrbProcessCapture,
    normalize_sina_lrb_capture,
)
from opendata.pipeline.sina_lrb_raw_capture import (
    SinaLrbArchivedCapture,
    SinaLrbRawAuthorization,
    SinaLrbRawCaptureHandler,
)

UTC = timezone.utc
INSTRUMENT_ID = "600519.SH"
OTHER_INSTRUMENT_ID = "600001.SH"
RETRIEVED_AT = datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
SIGNING_KEY = b"od204-sina-lrb-bridge-test-signing-key"


class _FixedProcessHandler(ProcessFetchHandler):
    """Return one synthetic source capture without launching a child process."""

    def __init__(self, capture: SinaLrbProcessCapture) -> None:
        self._capture = capture

    @property
    def heartbeat_interval_seconds(self) -> float:
        return 0.05

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        del request, context
        return self._capture


def _synthetic_response(*, revenue: str, profit: str) -> bytes:
    """Build a small deterministic Sina-shaped response without network I/O."""
    response = {
        "result": {
            "status": {"code": 0, "message": "ok"},
            "data": {
                "report_count": "2",
                "report_date": [
                    {"date_value": "20231231", "label": "FY"},
                    {"date_value": "20240630", "label": "H1"},
                ],
                # Deliberately use an order distinct from report_date to check
                # source ordinals survive contract ordering and API paging.
                "report_list": {
                    "20240630": {"data": [{"item_title": "营业收入", "item_value": revenue}]},
                    "20231231": {"data": [{"item_title": "归母净利润", "item_value": profit}]},
                },
            },
        }
    }
    return json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _grant(source_id: str = "sina-lrb") -> DatasetGrant:
    """Return an in-memory test grant for the exact LRB dataset."""
    return DatasetGrant(
        dataset_id=SINA_LRB_DATASET_ID,
        domain="equity",
        source_id=source_id,
        permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
        purposes=("research",),
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-LRB-BRIDGE",
    )


async def test_sina_lrb_publication_snapshot_and_records_bridge(tmp_path: Path) -> None:
    """Publish two synthetic captures and read one through fixed, scoped API pages."""
    first_body = _synthetic_response(revenue="123.45", profit="67.89")
    second_body = _synthetic_response(revenue="999.99", profit="888.88")
    contract = build_sina_lrb_observation_contract()
    contracts = DatasetContractRegistry([contract])
    task = TaskDefinition(
        job_id="sina-lrb-observation",
        version="v1",
        dataset_id=SINA_LRB_DATASET_ID,
        provider="sina",
        model="financial_statement",
        schedule=ScheduleMetadata(kind="cron", expression="0 0 * * *"),
        enabled=True,
    )
    request = RunRequest(
        run_type="manual",
        idempotency_key="sina-lrb-offline-bridge",
        query={},
        partition={"instrument_id": INSTRUMENT_ID},
    )
    capture_id = build_run_identity(task, request).idempotency_key
    second_request = RunRequest(
        run_type="manual",
        idempotency_key="sina-lrb-offline-bridge-second-capture",
        query={},
        partition={"instrument_id": INSTRUMENT_ID},
    )
    second_capture_id = build_run_identity(task, second_request).idempotency_key
    assert second_capture_id != capture_id
    batches: dict[str, NormalizedBatch] = {}

    engine = create_engine(f"sqlite:///{(tmp_path / 'sina-lrb-bridge.sqlite').as_posix()}")
    try:
        RUN_LEDGER_METADATA.create_all(engine)
        PUBLICATION_METADATA.create_all(engine)
        ledger = RunLedger(engine)
        store = PublicationStore(engine, cursor_signing_key=SIGNING_KEY)
        writer = ValidatedPublicationWriter(contracts)
        raw_root = tmp_path / "raw-evidence"
        authorization = SinaLrbRawAuthorization(
            source_access_ref="source-auth:synthetic-bridge-grant",
            raw_retention_ref="raw-retention:synthetic-bridge-grant",
            source_access_authorized=True,
            raw_retention_authorized=True,
        )

        def fetch(
            source_request: RunRequest,
            context: RunExecutionContext,
        ) -> tuple[SinaLrbArchivedCapture, SinaLrbArchivedCapture]:
            assert source_request.partition == {"instrument_id": INSTRUMENT_ID}

            def archive(
                response_body: bytes,
                capture_request: RunRequest,
                capture_context: RunExecutionContext,
                *,
                retrieved_at_utc: datetime,
            ) -> SinaLrbArchivedCapture:
                process_capture = SinaLrbProcessCapture(
                    query=SinaLrbQuery(INSTRUMENT_ID),
                    status_code=200,
                    retrieved_at_utc=retrieved_at_utc,
                    body=response_body,
                )
                capture_handler = SinaLrbRawCaptureHandler(
                    _FixedProcessHandler(process_capture),
                    artifact_root=raw_root,
                    authorization=authorization,
                    retention_class="restricted",
                    source_history_limit="unknown",
                )
                archived = capture_handler(capture_request, capture_context)
                assert type(archived) is SinaLrbArchivedCapture
                return archived

            first_archived = archive(
                first_body,
                source_request,
                context,
                retrieved_at_utc=RETRIEVED_AT,
            )
            second_archived = archive(
                second_body,
                second_request,
                replace(context, run_key=second_capture_id),
                retrieved_at_utc=RETRIEVED_AT + timedelta(minutes=5),
            )
            return first_archived, second_archived

        def normalize(
            raw: object,
            source_request: RunRequest,
            context: RunExecutionContext,
        ) -> NormalizedBatch:
            assert type(raw) is tuple and len(raw) == 2
            assert source_request.partition == {"instrument_id": INSTRUMENT_ID}
            first_batch = normalize_sina_lrb_capture(
                raw[0],
                source_request,
                context,
                artifact_root=raw_root,
            )
            second_batch = normalize_sina_lrb_capture(
                raw[1],
                second_request,
                replace(context, run_key=second_capture_id),
                artifact_root=raw_root,
            )
            evidence_fields = (
                "capture_id",
                "run_key",
                "response_sha256",
                "retrieved_at_utc",
                "raw_manifest_sha256",
                "request_summary_sha256",
                "source_access_ref",
                "raw_retention_ref",
                "raw_captured_at_utc",
            )
            capture_observations = [
                {field: capture_batch.provenance[field] for field in evidence_fields}
                for capture_batch in (first_batch, second_batch)
            ]
            provenance = dict(first_batch.provenance)
            for field in (*evidence_fields, "raw_response_persisted"):
                provenance.pop(field)
            provenance["capture_observations"] = capture_observations
            provenance["raw_response_persisted"] = all(
                capture_batch.provenance["raw_response_persisted"] is True
                for capture_batch in (first_batch, second_batch)
            )
            combined_batch = replace(
                first_batch,
                records=tuple(
                    sorted(
                        (*first_batch.records, *second_batch.records),
                        key=lambda record: record.business_key,
                    )
                ),
                provenance=provenance,
            )
            batches[context.run_key] = combined_batch
            return combined_batch

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
        assert service.submit(task.job_id, task.version, request).run_key == capture_id
        outcome = service.run_once("sina-lrb-bridge-worker", timedelta(minutes=1))
        assert (
            outcome is not None and outcome.record is not None and outcome.publication is not None
        )
        assert (outcome.record.run_key, outcome.record.status) == (capture_id, "succeeded")
        assert (outcome.publication.dataset_id, outcome.publication.record_count) == (
            SINA_LRB_DATASET_ID,
            6,
        )
        batch = batches[capture_id]
        assert len(batch.records) == 6
        assert {record.payload["capture_id"] for record in batch.records} == {
            capture_id,
            second_capture_id,
        }
        assert batch.provenance["raw_response_persisted"] is True
        capture_observations = batch.provenance["capture_observations"]
        assert isinstance(capture_observations, list) and len(capture_observations) == 2
        assert {item["run_key"] for item in capture_observations} == {
            capture_id,
            second_capture_id,
        }
        assert all(
            item["capture_id"] == item["run_key"]
            and item["request_summary_sha256"] == sina_lrb_request_summary_sha256(INSTRUMENT_ID)
            and item["source_access_ref"] == "source-auth:synthetic-bridge-grant"
            and item["raw_retention_ref"] == "raw-retention:synthetic-bridge-grant"
            and len(item["raw_manifest_sha256"]) == 64
            for item in capture_observations
        )
        assert {item["response_sha256"] for item in capture_observations} == {
            hashlib.sha256(first_body).hexdigest(),
            hashlib.sha256(second_body).hexdigest(),
        }

        app = FastAPI()
        app.include_router(router)
        fake_principal = Principal(
            user=User(
                id=17,
                username="od204-lrb-bridge-test",
                email="od204-lrb-bridge@example.test",
                hashed_password="not-a-real-password-hash",
                role=UserRole.USER,
                is_active=True,
            )
        )
        grants = DatasetGrantRegistry([_grant()])
        app.dependency_overrides[get_current_principal] = lambda: fake_principal
        app.dependency_overrides[get_dataset_grants] = lambda: grants
        app.dependency_overrides[get_dataset_contracts] = lambda: contracts
        app.dependency_overrides[get_publication_store] = lambda: store

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            snapshot_response = await client.post(
                "/api/v2/snapshots",
                json={
                    "dataset_id": SINA_LRB_DATASET_ID,
                    "page_size": 1,
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": capture_id,
                },
            )
            assert snapshot_response.status_code == 201
            snapshot = snapshot_response.json()
            snapshot_id = snapshot["snapshot_id"]
            assert snapshot["record_count"] == 3
            assert snapshot["selection"] == {
                "instrument_id": INSTRUMENT_ID,
                "capture_id": capture_id,
            }

            pages: list[dict[str, Any]] = []
            cursor: str | None = None
            for _page_number in range(3):
                params: dict[str, str] = {"snapshot_id": snapshot_id}
                if cursor is not None:
                    params["cursor"] = cursor
                response = await client.get(
                    f"/api/v2/datasets/{SINA_LRB_DATASET_ID}/records",
                    params=params,
                )
                assert response.status_code == 200
                page = response.json()
                assert page["snapshot_id"] == snapshot_id
                assert page["selection"] == {
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": capture_id,
                }
                assert page["count"] == 1
                assert len(page["rows"]) == 1
                pages.append(page)
                cursor = page["next_cursor"]
                assert page["has_more"] is (_page_number < 2)
                assert (cursor is not None) is (_page_number < 2)

            rows = [page["rows"][0] for page in pages]
            payloads = [row["payload"] for row in rows]
            assert [(payload["record_kind"], payload["record_key"]) for payload in payloads] == [
                ("context", "context"),
                ("report", "20231231"),
                ("report", "20240630"),
            ]
            assert all(
                payload["instrument_id"] == INSTRUMENT_ID
                and payload["capture_id"] == capture_id
                and payload["statement_code"] == "lrb"
                for payload in payloads
            )
            assert all(row["business_date"] is None and row["event_at"] is None for row in rows)
            reports = {payload["record_key"]: payload for payload in payloads[1:]}
            assert (
                reports["20231231"]["report_list_ordinal"],
                reports["20231231"]["report_date_ordinal"],
            ) == (1, 0)
            assert (
                reports["20240630"]["report_list_ordinal"],
                reports["20240630"]["report_date_ordinal"],
            ) == (0, 1)
            selected_capture_records = [
                record for record in batch.records if record.payload["capture_id"] == capture_id
            ]
            assert len(selected_capture_records) == 3
            assert payloads == [dict(record.payload) for record in selected_capture_records]
            assert (
                reconstruct_sina_lrb_source_document(payloads)
                == parse_sina_lrb_response(first_body).source_document
            )

            changed_selector = await client.get(
                f"/api/v2/datasets/{SINA_LRB_DATASET_ID}/records",
                params={
                    "snapshot_id": snapshot_id,
                    "instrument_id": OTHER_INSTRUMENT_ID,
                    "capture_id": capture_id,
                },
            )
            assert changed_selector.status_code == 422
            assert changed_selector.json()["error"]["code"] == "invalid_selection"

            malformed_selector = await client.post(
                "/api/v2/snapshots",
                json={
                    "dataset_id": SINA_LRB_DATASET_ID,
                    "instrument_id": "600519.BJ",
                    "capture_id": capture_id,
                },
            )
            assert malformed_selector.status_code == 422
            assert malformed_selector.json()["error"]["code"] == "invalid_selection"

            app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry([])
            unauthorized = await client.post(
                "/api/v2/snapshots",
                json={
                    "dataset_id": SINA_LRB_DATASET_ID,
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": capture_id,
                },
            )
            assert unauthorized.status_code == 403
            assert capture_id not in unauthorized.text

            app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry(
                [_grant(source_id="sina-fzb")]
            )
            mismatched_source = await client.get(
                f"/api/v2/datasets/{SINA_LRB_DATASET_ID}/records",
                params={
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": capture_id,
                    "page_size": 1,
                },
            )
            assert mismatched_source.status_code == 503
            assert capture_id not in mismatched_source.text

            app.dependency_overrides[get_dataset_grants] = lambda: grants
            wrong_source_contract = contract.model_copy(
                update={"source": SourceIdentity(source="sina", source_id="sina-fzb")}
            )
            app.dependency_overrides[get_dataset_contracts] = lambda: DatasetContractRegistry(
                [wrong_source_contract]
            )
            mismatched_contract_source = await client.get(
                f"/api/v2/datasets/{SINA_LRB_DATASET_ID}/records",
                params={
                    "instrument_id": INSTRUMENT_ID,
                    "capture_id": capture_id,
                    "page_size": 1,
                },
            )
            assert mismatched_contract_source.status_code == 503
            assert capture_id not in mismatched_contract_source.text
    finally:
        engine.dispose()
