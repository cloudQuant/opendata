"""HTTP behavior tests for the published-data REST API."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, update

from opendata.api import published_data as published_data_module
from opendata.api.dependencies import Principal, get_current_principal, get_db
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    MAX_RESPONSE_BYTES,
    close_publication_store,
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
    SNAPSHOTS,
    PageSizeLimitError,
    PublicationContractConflictError,
    PublicationStore,
    PublishedRecordInput,
    SnapshotSizeLimitError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

DATASET_ID = "equity.daily"
SOURCE_ID = "provider.alpha"
START_DATE = date(2026, 10, 21)
END_DATE = date(2026, 10, 23)
UTC = timezone.utc


@pytest.fixture
def published_api(
    tmp_path: Path,
) -> Iterator[tuple[FastAPI, PublicationStore, DatasetContract]]:
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
            FieldContract(name="volume", data_type="integer"),
            FieldContract(name="is_active", data_type="boolean"),
            FieldContract(name="metadata", data_type="object"),
            FieldContract(name="labels", data_type="array"),
            FieldContract(name="source_timestamp", data_type="datetime"),
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
    try:
        yield app, store, contract
    finally:
        engine.dispose()


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
        payload=_payload(instrument_id, trade_date, close),
    )


def _payload(instrument_id: str, trade_date: date, close: float) -> dict[str, object]:
    """Build a valid sample row spanning every JSON scalar and container type."""
    return {
        "instrument_id": instrument_id,
        "trade_date": trade_date.isoformat(),
        "close": close,
        "volume": 100,
        "is_active": True,
        "metadata": {"source": "fixture"},
        "labels": ["daily", "verified"],
        "source_timestamp": "2026-10-24T02:00:00+00:00",
    }


def _canonical_business_key(values: tuple[str, ...]) -> str:
    """Encode a key as compact UTF-8 JSON in declared field order."""
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _grant(dataset_id: str = DATASET_ID, source_id: str = SOURCE_ID) -> DatasetGrant:
    """Create an explicit fixture grant for one published dataset identity."""
    return DatasetGrant(
        dataset_id=dataset_id,
        domain="equity",
        source_id=source_id,
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
    )


def _publish_contract_variant(
    store: PublicationStore,
    base_contract: DatasetContract,
    dataset_id: str,
    updates: dict[str, object],
) -> tuple[FastAPI, DatasetContract]:
    """Publish another dataset with its own exact contract and reviewed grant."""
    payload = base_contract.model_dump(mode="python")
    payload.update(dataset_id=dataset_id, **updates)
    contract = DatasetContract.model_validate(payload)
    store.publish(
        dataset_id=dataset_id,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-204-VARIANT", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key=f"fixture-{dataset_id}",
        records=(_record("AAA", START_DATE, 100.0),),
    )
    grants = DatasetGrantRegistry((_grant(DATASET_ID), _grant(dataset_id)))
    contracts = DatasetContractRegistry((base_contract, contract))
    return _make_app(store, grants, contracts), contract


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
async def test_catalog_and_dataset_detail_expose_only_verified_published_metadata(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog and detail routes use the same exact contract-backed publication view."""
    app, store, contract = published_api
    catalog = await _request(app, "GET", "/api/v2/datasets")
    detail = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")

    assert catalog.status_code == detail.status_code == 200
    assert catalog.json()["count"] == 1
    assert catalog.json()["items"][0]["dataset_id"] == DATASET_ID
    assert catalog.json()["items"][0]["contract_sha256"] == canonical_sha256(contract)
    assert detail.json()["dataset_id"] == DATASET_ID
    assert detail.json()["schema"][1]["name"] == "trade_date"
    assert detail.json()["record_count"] == 2

    original_get_publication = store.get_publication

    def naive_published_at(publication_id: str) -> Any:
        publication = original_get_publication(publication_id)
        if publication is None:
            return None
        return replace(publication, published_at=datetime(2026, 10, 24, 3, 0, 0))

    monkeypatch.setattr(store, "get_publication", naive_published_at)
    normalized_timestamp = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert normalized_timestamp.status_code == 200
    assert normalized_timestamp.json()["as_of"] == "2026-10-24T03:00:00+00:00"


@pytest.mark.asyncio
async def test_delta_page_preserves_each_rows_origin_publication_lineage(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A v2 snapshot keeps old row origins while a changed row points to v2."""
    app, store, contract = published_api
    original_publication_id = store.list_datasets()[0].publication_id
    updated_publication = store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-204-DELTA", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="fixture-publication-delta-b",
        records=(_record("AAA", date(2026, 10, 22), 201.0),),
    )

    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 200
    page = response.json()
    assert page["publication_id"] == updated_publication.publication_id
    assert page["sequence"] == updated_publication.sequence == 2
    assert page["rows"][0]["publication_id"] == original_publication_id
    assert page["rows"][0]["publication_sequence"] == 1
    assert page["rows"][0]["payload"]["close"] == 100.0
    assert page["rows"][1]["publication_id"] == updated_publication.publication_id
    assert page["rows"][1]["publication_sequence"] == 2
    assert page["rows"][1]["payload"]["close"] == 201.0
    assert all(row["publication_sequence"] <= page["sequence"] for row in page["rows"])


@pytest.mark.asyncio
async def test_rows_preserve_null_source_times_without_inventing_timestamps(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Absent source times remain null in an otherwise valid stored observation."""
    app, store, contract = published_api
    store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-204-NULL-TIMES", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="fixture-publication-null-times",
        records=(
            PublishedRecordInput(
                business_key=_canonical_business_key(("BBB", "2026-10-22")),
                business_date=date(2026, 10, 22),
                payload=_payload("BBB", date(2026, 10, 22), 201.0),
            ),
        ),
    )
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 200
    row = response.json()["rows"][-1]
    assert row["event_at"] is None
    assert row["observed_at"] is None
    assert row["source_published_at"] is None
    assert row["available_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "business_key"),
    (
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "close": "bad"},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "instrument_id": 7},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "trade_date": "2026-02-30"},
            ("BBB", "2026-02-30"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "volume": 1.5},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "is_active": 1},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "metadata": []},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "labels": {}},
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "source_timestamp": "bad"},
            ("BBB", "2026-10-22"),
        ),
        (
            {
                key: value
                for key, value in _payload("BBB", date(2026, 10, 22), 201.0).items()
                if key != "close"
            },
            ("BBB", "2026-10-22"),
        ),
        (
            {**_payload("BBB", date(2026, 10, 22), 201.0), "extra": True},
            ("BBB", "2026-10-22"),
        ),
        (
            _payload("BBB", date(2026, 10, 22), 201.0),
            ("OTHER", "2026-10-22"),
        ),
        (
            _payload("BBB", date(2026, 10, 21), 201.0),
            ("BBB", "2026-10-21"),
        ),
    ),
)
async def test_records_fail_closed_when_a_persisted_payload_violates_its_contract(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    payload: dict[str, object],
    business_key: tuple[str, str],
) -> None:
    """Wire reads reject persisted payloads with unknown, missing, mistyped, or stale keys."""
    app, store, contract = published_api
    store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis="raw",
        quality={"status": "passed", "report_id": "DQ-204-BAD-ROW", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key=f"fixture-publication-bad-{len(json.dumps(payload))}",
        records=(
            PublishedRecordInput(
                business_key=_canonical_business_key(business_key),
                business_date=date(2026, 10, 22),
                payload=payload,
            ),
        ),
    )
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"


@pytest.mark.asyncio
async def test_empty_catalog_does_not_read_publications_without_permitted_grants(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty authorization catalog short-circuits before any warehouse query."""
    app, store, contract = published_api
    app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry(())

    def fail_if_catalog_is_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unauthorized catalog attempted a warehouse read")

    monkeypatch.setattr(store, "list_datasets", fail_if_catalog_is_read)
    response = await _request(app, "GET", "/api/v2/datasets")

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ("contract_hash", "source", "quality", "registry"))
async def test_dataset_detail_fails_closed_on_unverifiable_publication_metadata(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    """Contract, source, quality, and registry mismatches never expose dataset metadata."""
    app, store, contract = published_api
    original_get_publication = store.get_publication
    if mismatch == "registry":
        app.dependency_overrides[get_dataset_contracts] = lambda: DatasetContractRegistry(())
    else:

        def mismatched_publication(publication_id: str) -> Any:
            publication = original_get_publication(publication_id)
            if publication is None:
                return None
            if mismatch == "contract_hash":
                return replace(publication, provenance={"contract_sha256": "0" * 64})
            if mismatch == "source":
                return replace(publication, source_id="provider.other")
            return replace(publication, quality={"status": "passed", "issue_count": "invalid"})

        monkeypatch.setattr(store, "get_publication", mismatched_publication)

    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert contract.dataset_id == DATASET_ID


@pytest.mark.asyncio
async def test_unknown_dataset_detail_is_unavailable_without_a_grant(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Unreviewed dataset identifiers remain indistinguishable from inaccessible data."""
    app, _, _ = published_api
    response = await _request(app, "GET", "/api/v2/datasets/equity.missing")
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Dataset is unavailable"


@pytest.mark.asyncio
async def test_granted_but_unpublished_dataset_is_not_found(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """An explicit grant reveals no dataset until an immutable publication exists."""
    _, store, contract = published_api
    unpublished_id = "equity.approved-but-empty"
    app = _make_app(
        store,
        DatasetGrantRegistry((_grant(unpublished_id),)),
        DatasetContractRegistry((contract,)),
    )
    response = await _request(app, "GET", f"/api/v2/datasets/{unpublished_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


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
            "frequency": "1m",
        },
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
async def test_routes_reject_repeated_query_keys_bad_ids_and_missing_snapshot_dataset(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Ambiguous or malformed identifiers are rejected before store access."""
    app, _, _ = published_api
    repeated = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records?start_date=2026-10-21&start_date=2026-10-22&end_date=2026-10-23",
    )
    assert repeated.status_code == 422
    assert repeated.json()["error"]["code"] == "unsupported_query"

    bad_snapshot_id = await _request(
        app,
        "GET",
        f"/api/v2/snapshots/{'A' * 32}",
        params={"dataset_id": DATASET_ID},
    )
    assert bad_snapshot_id.status_code == 422
    assert bad_snapshot_id.json()["error"]["code"] == "invalid_request"

    missing_dataset_id = await _request(app, "GET", f"/api/v2/snapshots/{'a' * 32}")
    assert missing_dataset_id.status_code == 422
    assert missing_dataset_id.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_records_require_a_window_and_can_select_a_known_publication(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """First-page reads require bounds and accept only exact published version IDs."""
    app, store, _ = published_api
    missing_window = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}/records")
    assert missing_window.status_code == 422
    assert missing_window.json()["error"]["code"] == "date_window_required"

    publication_id = store.list_datasets()[0].publication_id
    selected = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "publication_id": publication_id,
        },
    )
    assert selected.status_code == 200
    assert selected.json()["publication_id"] == publication_id

    unknown_publication = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "publication_id": "f" * 32,
        },
    )
    assert unknown_publication.status_code == 404
    assert unknown_publication.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_snapshot_body_rejects_noncanonical_and_incomplete_date_windows(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """JSON body validation rejects invalid dates, one-sided ranges, and reversed ranges."""
    app, _, _ = published_api
    invalid_bodies = (
        {
            "dataset_id": DATASET_ID,
            "start_date": "2026-02-30",
            "end_date": "2026-03-01",
        },
        {
            "dataset_id": DATASET_ID,
            "start_date": "2026-10-21T00:00:00Z",
            "end_date": "2026-10-23",
        },
        {"dataset_id": DATASET_ID, "start_date": "2026-10-21"},
        {
            "dataset_id": DATASET_ID,
            "start_date": "2026-10-23",
            "end_date": "2026-10-21",
        },
        {
            "dataset_id": DATASET_ID,
            "publication_id": "not-an-identifier",
            "start_date": "2026-10-21",
            "end_date": "2026-10-23",
        },
    )
    for body in invalid_bodies:
        response = await _request(app, "POST", "/api/v2/snapshots", json=body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_snapshot_cursor_rejects_tampering_and_another_authenticated_subject(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Cursor bytes and the caller identity are both bound to the pinned snapshot."""
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
    snapshot = first.json()
    cursor = snapshot["next_cursor"]
    tampered_cursor = ("A" if cursor[0] != "A" else "B") + cursor[1:]

    tampered = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"snapshot_id": snapshot["snapshot_id"], "cursor": tampered_cursor},
    )
    assert tampered.status_code == 422
    assert tampered.json()["error"]["code"] == "invalid_snapshot_request"

    other_user = User(
        id=99,
        username="other-user",
        email="other-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=other_user)
    cross_subject = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"snapshot_id": snapshot["snapshot_id"], "cursor": cursor},
    )
    assert cross_subject.status_code == 404
    assert cross_subject.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_expired_snapshot_maps_to_gone_after_its_persisted_expiry(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A persisted expired snapshot cannot be resumed or read as current data."""
    app, store, _ = published_api
    created = await _request(
        app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": DATASET_ID,
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
        },
    )
    assert created.status_code == 201
    snapshot_id = created.json()["snapshot_id"]
    with store._engine.begin() as connection:
        connection.execute(
            update(SNAPSHOTS).where(SNAPSHOTS.c.snapshot_id == snapshot_id).values(expires_at_us=0)
        )

    response = await _request(
        app,
        "GET",
        f"/api/v2/snapshots/{snapshot_id}",
        params={"dataset_id": DATASET_ID},
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_empty_snapshot_page_returns_a_valid_empty_result(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """A valid date window without rows returns a pinned, explicitly empty page."""
    app, _, _ = published_api
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={
            "start_date": "2026-10-23",
            "end_date": "2026-10-24",
        },
    )
    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["count"] == 0
    assert response.json()["record_count"] == 0
    assert response.json()["has_more"] is False
    assert response.json()["next_cursor"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "expected_code"),
    (
        ("pit", "point_in_time_required"),
        ("event_time", "unsupported_time_semantics"),
        ("numeric_business_key", "unsupported_business_key"),
    ),
)
async def test_record_route_rejects_contract_semantics_it_cannot_guarantee(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    variant: str,
    expected_code: str,
) -> None:
    """Only a contract with a supported time model and PIT policy can be queried."""
    _, store, base_contract = published_api
    dataset_id = f"equity.variant.{variant}"
    if variant == "pit":
        updates: dict[str, object] = {"pit_policy": "required"}
    elif variant == "event_time":
        updates = {
            "fields": (*base_contract.fields, FieldContract(name="event_at", data_type="datetime")),
            "time_field": "event_at",
            "temporal_semantics": "event_time",
        }
    else:
        updates = {"business_key": ("close",), "ordering": ("trade_date", "close")}
    app, _ = _publish_contract_variant(store, base_contract, dataset_id, updates)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{dataset_id}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code


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
async def test_grant_and_contract_configuration_loaders_fail_closed_over_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """Operator grant and contract files are required, parsed, and schema-checked."""
    app, _, contract = published_api
    app.dependency_overrides.pop(get_dataset_grants)
    app.dependency_overrides.pop(get_dataset_contracts)
    grants_path = tmp_path / "grants.json"
    contracts_path = tmp_path / "contracts.json"
    grants_path.write_text(
        json.dumps({"datasets": [_grant().model_dump(mode="json")]}), encoding="utf-8"
    )
    contracts_path.write_text(
        json.dumps({"registry_version": 1, "contracts": [contract.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUBLISHED_DATASET_GRANTS_FILE", str(grants_path))
    monkeypatch.setenv("PUBLISHED_DATASET_CONTRACTS_FILE", str(contracts_path))
    valid = await _request(app, "GET", "/api/v2/datasets")
    assert valid.status_code == 200
    assert valid.json()["count"] == 1

    grants_path.write_text("{invalid", encoding="utf-8")
    invalid_grants = await _request(app, "GET", "/api/v2/datasets")
    assert invalid_grants.status_code == 503
    assert invalid_grants.json()["error"]["code"] == "published_data_unavailable"

    grants_path.write_text(
        json.dumps({"datasets": [_grant().model_dump(mode="json")]}), encoding="utf-8"
    )
    contracts_path.write_text("{invalid", encoding="utf-8")
    invalid_contracts = await _request(app, "GET", "/api/v2/datasets")
    assert invalid_contracts.status_code == 503
    assert invalid_contracts.json()["error"]["code"] == "published_data_unavailable"

    monkeypatch.setenv("PUBLISHED_DATASET_GRANTS_FILE", "")
    missing_grants = await _request(app, "GET", "/api/v2/datasets")
    assert missing_grants.status_code == 503
    monkeypatch.setenv("PUBLISHED_DATASET_GRANTS_FILE", str(grants_path))
    monkeypatch.setenv("PUBLISHED_DATASET_CONTRACTS_FILE", "")
    missing_contracts = await _request(app, "GET", "/api/v2/datasets")
    assert missing_contracts.status_code == 503


@pytest.mark.asyncio
async def test_configuration_loaders_read_explicit_env_file_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
) -> None:
    """The selected operator env file supplies paths when process variables are absent."""
    app, _, contract = published_api
    app.dependency_overrides.pop(get_dataset_grants)
    app.dependency_overrides.pop(get_dataset_contracts)
    grants_path = tmp_path / "grants.json"
    contracts_path = tmp_path / "contracts.json"
    env_path = tmp_path / ".published-data.env"
    grants_path.write_text(
        json.dumps({"datasets": [_grant().model_dump(mode="json")]}), encoding="utf-8"
    )
    contracts_path.write_text(
        json.dumps({"registry_version": 1, "contracts": [contract.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            (
                f"PUBLISHED_DATASET_GRANTS_FILE={grants_path}",
                f"PUBLISHED_DATASET_CONTRACTS_FILE={contracts_path}",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PUBLISHED_DATASET_GRANTS_FILE", raising=False)
    monkeypatch.delenv("PUBLISHED_DATASET_CONTRACTS_FILE", raising=False)
    monkeypatch.setattr(
        published_data_module,
        "settings",
        SimpleNamespace(model_config={"env_file": (env_path,)}),
    )

    response = await _request(app, "GET", "/api/v2/datasets")
    assert response.status_code == 200
    assert response.json()["count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("missing_signing_key", "missing_warehouse_fields"))
async def test_configured_store_requires_signing_key_and_explicit_warehouse_settings(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """The default store factory never falls back to an implicit local warehouse."""
    app, _, _ = published_api
    app.dependency_overrides.pop(get_publication_store)
    monkeypatch.setattr(published_data_module, "_CONFIGURED_STORE", None)
    monkeypatch.setattr(published_data_module, "_CONFIGURED_ENGINE", None)
    if failure == "missing_signing_key":
        monkeypatch.delenv("PUBLISHED_DATA_CURSOR_SIGNING_KEY", raising=False)
        monkeypatch.setattr(
            published_data_module,
            "settings",
            SimpleNamespace(
                model_fields_set={"data_mysql_host", "data_mysql_user", "data_mysql_database"},
                model_config={"env_file": None},
            ),
        )
    else:
        monkeypatch.setenv("PUBLISHED_DATA_CURSOR_SIGNING_KEY", "x" * 32)
        monkeypatch.setattr(
            published_data_module,
            "settings",
            SimpleNamespace(model_fields_set=set()),
        )

    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"
    assert "signing" not in response.text


@pytest.mark.asyncio
async def test_configured_store_is_disposed_after_a_missing_schema_response(
    tmp_path: Path,
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lazily constructed store uses explicit settings and can release its engine."""
    app, _, _ = published_api
    app.dependency_overrides.pop(get_publication_store)
    monkeypatch.setattr(published_data_module, "_CONFIGURED_STORE", None)
    monkeypatch.setattr(published_data_module, "_CONFIGURED_ENGINE", None)
    monkeypatch.setattr(
        published_data_module,
        "settings",
        SimpleNamespace(
            model_fields_set={"data_mysql_host", "data_mysql_user", "data_mysql_database"},
            data_database_url=f"sqlite:///{tmp_path / 'configured-store.db'}",
        ),
    )
    monkeypatch.setenv("PUBLISHED_DATA_CURSOR_SIGNING_KEY", "x" * 32)

    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 503
    close_publication_store()
    assert published_data_module._CONFIGURED_STORE is None
    assert published_data_module._CONFIGURED_ENGINE is None


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
    row = schema["components"]["schemas"]["PublishedRowResponse"]["properties"]
    assert "publication_sequence" in row


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
async def test_snapshot_size_limit_maps_to_413(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection that exceeds the persisted snapshot limit is not partially pinned."""
    app, store, _ = published_api

    def raise_snapshot_size_error(*args: Any, **kwargs: Any) -> None:
        raise SnapshotSizeLimitError

    monkeypatch.setattr(store, "create_snapshot", raise_snapshot_size_error)
    response = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{DATASET_ID}/records",
        params={"start_date": START_DATE.isoformat(), "end_date": END_DATE.isoformat()},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "snapshot_too_large"


@pytest.mark.asyncio
async def test_publication_contract_conflict_is_a_stable_conflict_response(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store-level immutable-contract conflict stays a machine-readable 409."""
    app, store, _ = published_api

    def raise_contract_conflict(publication_id: str) -> Any:
        raise PublicationContractConflictError("private store diagnostic")

    monkeypatch.setattr(store, "get_publication", raise_contract_conflict)
    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "publication_contract_conflict"
    assert "private store diagnostic" not in response.text


@pytest.mark.asyncio
async def test_unexpected_store_failure_is_sanitized_as_internal_error(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected store exceptions do not leak internals through the HTTP envelope."""
    app, store, _ = published_api

    def fail_catalog(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private connection detail")

    monkeypatch.setattr(store, "list_datasets", fail_catalog)
    response = await _request(app, "GET", "/api/v2/datasets")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private connection detail" not in response.text


@pytest.mark.asyncio
async def test_success_response_size_guard_returns_413(
    published_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final serialized body is checked against the public response budget."""
    app, _, _ = published_api
    monkeypatch.setattr(published_data_module, "MAX_RESPONSE_BYTES", 1)
    response = await _request(app, "GET", f"/api/v2/datasets/{DATASET_ID}")
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "response_too_large"


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
