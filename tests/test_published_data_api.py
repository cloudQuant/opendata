"""HTTP behavior tests for the published-data REST API."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from opendata.api.dependencies import Principal, get_current_principal, get_db
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    MAX_RESPONSE_BYTES,
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import (
    DatasetContractRegistry,
    canonical_sha256,
)
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.models.user import User, UserRole
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PageSizeLimitError,
    PublicationStore,
    PublishedRecordInput,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

DATASET_ID = "equity.daily"
SOURCE_ID = "provider.alpha"
START_DATE = date(2026, 10, 21)
END_DATE = date(2026, 10, 23)
UTC = timezone.utc


@pytest.fixture
def published_api(tmp_path: Path) -> tuple[FastAPI, PublicationStore, DatasetContract]:
    """Create an isolated SQLite publication store and an explicitly reviewed contract."""
    engine = create_engine(f"sqlite:///{tmp_path / 'published-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"test-cursor-signing-key-32-bytes!")
    contract = DatasetContract(
        dataset_id=DATASET_ID,
        schema_version="1",
        semantic_version="1.0.0",
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
        temporal_semantics="business_date",
        time_field="trade_date",
        source=SourceIdentity(source_id=SOURCE_ID, source="fixture-provider"),
        field_units={"close": "CNY/share"},
        field_currencies={"close": "CNY"},
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={"close": "raw"},
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )
    digest = canonical_sha256(contract)
    store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-204-1", "issue_count": 0},
        provenance={"contract_sha256": digest},
        idempotency_key="fixture-publication-1",
        records=(
            _record("AAA", START_DATE, 100.0),
            _record("AAA", date(2026, 10, 22), 200.0),
        ),
    )
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=DATASET_ID,
                domain="equity",
                source_id=SOURCE_ID,
                permitted_operations=(
                    "data.read",
                    "coverage.read",
                    "snapshot.create",
                    "snapshot.read",
                ),
                purposes=("research",),
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                review_evidence="OD204-TEST",
            ),
        )
    )
    contracts = DatasetContractRegistry((contract,))
    app = _make_app(store, grants, contracts)
    return app, store, contract


def _record(instrument_id: str, trade_date: date, close: float) -> PublishedRecordInput:
    """Build one fixture row with independent business, event, and observation times."""
    business_key = _canonical_business_key((instrument_id, trade_date.isoformat()))
    return PublishedRecordInput(
        business_key=business_key,
        business_date=trade_date,
        event_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC),
        observed_at=datetime(2026, 10, 24, 1, tzinfo=UTC),
        source_published_at=datetime(2026, 10, 24, 2, tzinfo=UTC),
        available_at=datetime(2026, 10, 24, 3, tzinfo=UTC),
        payload={
            "instrument_id": instrument_id,
            "trade_date": trade_date.isoformat(),
            "close": close,
        },
    )


def _canonical_business_key(values: tuple[str, ...]) -> str:
    """Encode a key as compact UTF-8 JSON in declared field order."""
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _make_app(
    store: PublicationStore,
    grants: DatasetGrantRegistry,
    contracts: DatasetContractRegistry,
    *,
    principal: Principal | None = None,
) -> FastAPI:
    """Mount only the v2 API and override its server-side dependencies."""
    user = User(
        id=7,
        username="api-user",
        email="api-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    selected_principal = principal or Principal(user=user)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: selected_principal
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Any:
    """Send one isolated ASGI request without relying on the repository app lifespan."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_first_page_pins_snapshot_and_later_pages_ignore_new_publications(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A cursor reads the old publication even after a correction is published."""
    app, store, contract = published_api
    first = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "page_size": 1,
        },
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["count"] == 1
    assert first_body["record_count"] == 2
    assert first_body["rows"][0]["payload"]["close"] == 100.0
    assert first_body["window"] == {
        "start": START_DATE.isoformat(),
        "end": END_DATE.isoformat(),
        "end_exclusive": True,
    }
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    assert first_body["meta"]["snapshot"]["snapshot_id"] == first_body["snapshot_id"]
    assert first_body["source"] == {"source_id": SOURCE_ID, "source": "fixture-provider"}
    assert first_body["contract_sha256"] == canonical_sha256(contract)
    assert first_body["field_units"] == {"close": "CNY/share"}
    assert first_body["quality"] == {
        "status": "passed",
        "report_id": "DQ-204-1",
        "issue_count": 0,
    }
    assert first_body["rows"][0]["event_at"] == "2026-10-21T00:00:00+00:00"
    assert first.headers["cache-control"] == "private, no-store"

    store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "warning", "report_id": "DQ-204-2", "issue_count": 1},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="fixture-publication-correction",
        records=(_record("AAA", date(2026, 10, 22), 201.0),),
    )
    continuation = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "snapshot_id": first_body["snapshot_id"],
            "cursor": first_body["next_cursor"],
        },
    )
    assert continuation.status_code == 200
    continuation_body = continuation.json()
    assert continuation_body["snapshot_id"] == first_body["snapshot_id"]
    assert continuation_body["publication_id"] == first_body["publication_id"]
    assert continuation_body["rows"][0]["payload"]["close"] == 200.0
    assert continuation_body["rows"][0]["revision"] == 1
    assert continuation_body["record_count"] == 2
    assert continuation_body["has_more"] is False


@pytest.mark.asyncio
async def test_snapshot_post_accepts_json_dates_then_records_reads_its_first_page(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """The snapshot endpoint accepts real JSON dates and pins a reusable first page."""
    app, _, contract = published_api
    created = await _request(
        app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": DATASET_ID,
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "page_size": 1,
        },
    )
    assert created.status_code == 201
    snapshot = created.json()
    assert snapshot["dataset_id"] == DATASET_ID
    assert snapshot["schema_version"] == contract.schema_version
    assert snapshot["semantic_version"] == contract.semantic_version
    assert snapshot["snapshot_id"]
    assert snapshot["expires_at"] > snapshot["created_at"]
    assert snapshot["record_count"] == 2
    assert snapshot["schema"][0] == {
        "name": "instrument_id",
        "data_type": "string",
        "nullable": False,
    }
    assert snapshot["storage_order"] == [
        {
            "field": "business_date",
            "direction": "asc",
            "nulls": "last",
            "comparison": "native",
        },
        {
            "field": "business_key",
            "direction": "asc",
            "comparison": "binary_lexicographic",
        },
    ]
    assert snapshot["identity_key_encoding"] == "canonical-json-array-v1"

    first_page = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"snapshot_id": snapshot["snapshot_id"]},
    )
    assert first_page.status_code == 200
    assert first_page.json()["rows"][0]["business_date"] == START_DATE.isoformat()
    assert first_page.json()["meta"]["snapshot"] == snapshot
    assert first_page.json()["meta"]["ordering_fields"] == ["trade_date", "instrument_id"]
    assert first_page.json()["identity_semantics"]["temporal_semantics"] == "business_date"

    metadata = await _request(
        app,
        "GET",
        f"/api/v2/snapshots/{snapshot['snapshot_id']}",
        params={"dataset_id": DATASET_ID},
    )
    assert metadata.status_code == 200
    assert metadata.json() == snapshot


@pytest.mark.asyncio
async def test_snapshot_pages_reject_window_page_size_and_unsupported_query_changes(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Unimplemented filters and attempts to mutate a pinned view fail visibly."""
    app, _, _ = published_api
    first = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "page_size": 1,
        },
    )
    snapshot_id = first.json()["snapshot_id"]
    for params in (
        {"snapshot_id": snapshot_id, "page_size": 1},
        {"snapshot_id": snapshot_id, "start_date": START_DATE.isoformat()},
        {"snapshot_id": snapshot_id, "as_of": "2026-10-24T00:00:00Z"},
        {"snapshot_id": snapshot_id, "semantic_version": "1.0.0"},
        {
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "filter": "instrument_id=AAA",
        },
    ):
        response = await _request(
            app,
            "GET",
            f"/api/v2/datasets/{DATASET_ID}/records",
            params=params,
        )
        assert response.status_code == 422
        assert response.json()["error"]["request_id"]


@pytest.mark.asyncio
async def test_coverage_remains_unknown_and_does_not_infer_completeness_from_rows(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Coverage reports evidence limits even when the publication contains rows."""
    app, _, _ = published_api
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/coverage",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["scope"] == "unknown"
    assert coverage["requested"] == {
        "start": START_DATE.isoformat(),
        "end": END_DATE.isoformat(),
    }
    assert coverage["observed"] is None
    assert coverage["missing_ranges"] == []
    assert "independently verified" in coverage["limitation"]


@pytest.mark.asyncio
async def test_api_key_without_exact_operation_and_domain_is_denied_before_store_reads(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A domain wildcard does not substitute for the exact data.read operation."""
    _, store, _ = published_api
    user = User(
        id=8,
        username="key-user",
        email="key-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    principal = Principal(user=user, api_key_id=10, scopes=("*", "snapshot.create"))
    valid_grants = DatasetGrantRegistry(
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
    contracts = DatasetContractRegistry((published_api[2],))
    app = _make_app(store, valid_grants, contracts, principal=principal)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Dataset is unavailable"


@pytest.mark.asyncio
async def test_missing_warehouse_schema_returns_sanitized_503(
    tmp_path: Path,
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A missing migrated table never leaks a SQL error or filesystem path."""
    _, _, contract = published_api
    blank_engine = create_engine(f"sqlite:///{tmp_path / 'missing-schema.db'}")
    blank_store = PublicationStore(
        blank_engine, cursor_signing_key=b"test-cursor-signing-key-32-bytes!"
    )
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=DATASET_ID,
                domain="equity",
                source_id=SOURCE_ID,
                permitted_operations=("data.read",),
                purposes=("research",),
                valid_from=datetime(2026, 1, 1, tzinfo=UTC),
                valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                review_evidence="OD204-TEST",
            ),
        )
    )
    app = _make_app(blank_store, grants, DatasetContractRegistry((contract,)))
    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert str(tmp_path) not in response.text
    blank_engine.dispose()


@pytest.mark.asyncio
async def test_authentication_failure_is_401_before_publication_store_dependency(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Missing authentication stays a real 401 and never touches the warehouse."""
    app, _, _ = published_api
    app.dependency_overrides.pop(get_current_principal)

    async def no_database_session() -> AsyncIterator[None]:
        yield None

    def fail_if_store_is_requested() -> PublicationStore:
        raise AssertionError("warehouse dependency ran before authentication")

    app.dependency_overrides[get_db] = no_database_session
    app.dependency_overrides[get_publication_store] = fail_if_store_is_requested
    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_openapi_declares_success_and_error_payloads(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """OpenAPI includes machine-readable page, snapshot, coverage, and error schemas."""
    app, _, _ = published_api
    schema = app.openapi()
    records = schema["paths"]["/api/v2/datasets/{dataset_id}/records"]["get"]["responses"]
    assert records["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/DataPageResponse"
    )
    assert records["422"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorEnvelope"
    )
    page = schema["components"]["schemas"]["DataPageResponse"]["properties"]
    assert {
        "snapshot_id",
        "window",
        "rows",
        "count",
        "next_cursor",
        "identity_key_encoding",
    } <= set(page)


@pytest.mark.asyncio
async def test_store_page_size_overflow_maps_to_413(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store-level bounded-page failure is exposed as a stable 413 response."""
    app, store, _ = published_api

    def raise_page_size_error(*args: Any, **kwargs: Any) -> None:
        raise PageSizeLimitError

    monkeypatch.setattr(store, "read_page", raise_page_size_error)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "response_too_large"
    assert len(response.content) < MAX_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_rejected_quality_publication_cannot_create_or_read_a_snapshot(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A historical low-level rejected publication is unavailable to API readers."""
    app, store, _ = published_api
    get_publication = store.get_publication
    snapshot_creation_called = False

    def rejected_publication(publication_id: str) -> Any:
        publication = get_publication(publication_id)
        if publication is None:
            return None
        return replace(
            publication,
            quality={"status": "rejected", "report_id": "DQ-REJECTED", "issue_count": 1},
        )

    def fail_if_snapshot_created(*args: Any, **kwargs: Any) -> None:
        nonlocal snapshot_creation_called
        snapshot_creation_called = True
        raise AssertionError("rejected publication reached snapshot creation")

    monkeypatch.setattr(store, "get_publication", rejected_publication)
    monkeypatch.setattr(store, "create_snapshot", fail_if_snapshot_created)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert snapshot_creation_called is False


@pytest.mark.asyncio
async def test_snapshot_creation_rejects_unsupported_idempotency_header(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """The API does not imply retries are idempotent without store semantics."""
    app, _, _ = published_api
    response = await _request(
        app,
        "POST",
        "/api/v2/snapshots",
        headers={"Idempotency-Key": "client-generated-key"},
        json={
            "dataset_id": DATASET_ID,
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "page_size": 1,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_header"
