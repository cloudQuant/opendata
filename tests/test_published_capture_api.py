"""HTTP tests for the exact instrument/capture snapshot selector profile."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import (
    DatasetGrant,
    DatasetGrantRegistry,
    require_published_access,
)
from opendata.api.published_data import (
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
    PublicationStore,
    PublishedRecordInput,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

UTC = timezone.utc
SOURCE_ID = "sina-fzb"
SELECTED_DATASET_ID = "sina_fzb_report_observations"
LEGACY_DATASET_ID = "equity.legacy-table"
INVALID_PROFILE_DATASET_ID = "equity.invalid-capture-profile"
INSTRUMENT = "600000.SH"
OTHER_INSTRUMENT = "600001.SH"
CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64
CAPTURE_OTHER = "c" * 64


@dataclass(frozen=True, slots=True)
class CaptureApiFixture:
    """Owned SQLite store, mounted API, contracts, grants, and authenticated principal."""

    app: FastAPI
    store: PublicationStore
    grants: DatasetGrantRegistry
    principal: Principal
    selected_contract: DatasetContract
    legacy_contract: DatasetContract
    invalid_profile_contract: DatasetContract


@pytest.fixture
def capture_api(tmp_path: Path) -> Iterator[CaptureApiFixture]:
    """Create only an explicitly owned SQLite store and its API dependencies."""
    engine = create_engine(f"sqlite:///{tmp_path / 'capture-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"capture-api-signing-key-32-bytes!")
    selected_contract = _selected_contract()
    legacy_contract = _legacy_contract()
    invalid_profile_contract = _invalid_reserved_contract()
    grants = DatasetGrantRegistry(
        (
            _grant(SELECTED_DATASET_ID),
            _grant(LEGACY_DATASET_ID),
            _grant(INVALID_PROFILE_DATASET_ID),
        )
    )
    principal = _principal()
    _publish_selected_facts(store, selected_contract)
    _publish_legacy_fact(store, legacy_contract)
    _publish_invalid_profile_fact(store, invalid_profile_contract)
    contracts = DatasetContractRegistry(
        (selected_contract, legacy_contract, invalid_profile_contract)
    )
    app = _make_app(store, grants, contracts, principal)
    try:
        yield CaptureApiFixture(
            app=app,
            store=store,
            grants=grants,
            principal=principal,
            selected_contract=selected_contract,
            legacy_contract=legacy_contract,
            invalid_profile_contract=invalid_profile_contract,
        )
    finally:
        engine.dispose()


def _selected_contract() -> DatasetContract:
    """Build the reserved table/none contract with the exact selection keys."""
    return DatasetContract(
        dataset_id=SELECTED_DATASET_ID,
        schema_version="source-native-report-v1",
        semantic_version="sina-fzb-source-native-v1",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="capture_id", data_type="string"),
            FieldContract(name="statement_code", data_type="string"),
            FieldContract(name="report_period", data_type="date"),
            FieldContract(name="currency", data_type="string"),
            FieldContract(name="entries", data_type="array"),
            FieldContract(name="report_metadata", data_type="object"),
        ),
        business_key=("instrument_id", "capture_id", "statement_code", "report_period"),
        ordering=("instrument_id", "capture_id", "statement_code", "report_period"),
        filter_fields=("instrument_id", "capture_id"),
        temporal_semantics="none",
        source=SourceIdentity(source_id=SOURCE_ID, source="sina"),
        adjustment_basis="not_applicable",
        timezone="UTC",
        pit_policy="unsupported",
    )


def _legacy_contract() -> DatasetContract:
    """Build an older table contract whose one legacy filter is not a selector profile."""
    return DatasetContract(
        dataset_id=LEGACY_DATASET_ID,
        schema_version="1",
        semantic_version="1.0.0",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=(
            FieldContract(name="row_id", data_type="string"),
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="capture_id", data_type="string"),
            FieldContract(name="statement_code", data_type="string"),
            FieldContract(name="report_period", data_type="date"),
            FieldContract(name="currency", data_type="string"),
            FieldContract(name="entries", data_type="array"),
            FieldContract(name="report_metadata", data_type="object"),
        ),
        business_key=("row_id",),
        ordering=("row_id",),
        filter_fields=("instrument_id",),
        temporal_semantics="none",
        source=SourceIdentity(source_id=SOURCE_ID, source="sina"),
        adjustment_basis="not_applicable",
        timezone="UTC",
        pit_policy="unsupported",
    )


def _invalid_reserved_contract() -> DatasetContract:
    """Build a model-valid contract that violates the reserved profile's key rule."""
    return DatasetContract(
        dataset_id=INVALID_PROFILE_DATASET_ID,
        schema_version="1",
        semantic_version="1.0.0",
        asset_class="equity",
        frequency="irregular",
        record_kind="table",
        fields=(
            FieldContract(name="row_id", data_type="string"),
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="capture_id", data_type="string"),
            FieldContract(name="statement_code", data_type="string"),
            FieldContract(name="report_period", data_type="date"),
            FieldContract(name="currency", data_type="string"),
            FieldContract(name="entries", data_type="array"),
            FieldContract(name="report_metadata", data_type="object"),
        ),
        business_key=("row_id",),
        ordering=("row_id",),
        filter_fields=("instrument_id", "capture_id"),
        temporal_semantics="none",
        source=SourceIdentity(source_id=SOURCE_ID, source="sina"),
        adjustment_basis="not_applicable",
        timezone="UTC",
        pit_policy="unsupported",
    )


def _grant(dataset_id: str) -> DatasetGrant:
    """Give the fixture principal explicit read, create, and descriptor operations."""
    return DatasetGrant(
        dataset_id=dataset_id,
        domain="equity",
        source_id=SOURCE_ID,
        permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
        purposes=("research",),
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-TEST",
    )


def _principal() -> Principal:
    """Construct a non-persisted fake principal for the isolated ASGI router."""
    user = User(
        id=7,
        username="capture-api-user",
        email="capture-api@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    return Principal(user=user)


def _make_app(
    store: PublicationStore,
    grants: DatasetGrantRegistry,
    contracts: DatasetContractRegistry,
    principal: Principal,
) -> FastAPI:
    """Mount only the published-data router with explicit in-memory dependencies."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: Any,
) -> Response:
    """Send one ASGI request without application lifespan or a network socket."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def _payload(
    *,
    instrument_id: str,
    capture_id: str,
    report_period: str,
) -> dict[str, object]:
    """Return a compact source-native report row with distinct table and report dates."""
    return {
        "instrument_id": instrument_id,
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": report_period,
        "currency": "CNY",
        "entries": [
            {
                "kind": "item",
                "source_item_id": "total_assets",
                "source_ordinals": [0],
                "raw_value": "100.00",
            }
        ],
        "report_metadata": {"source_marker": "fixture"},
    }


def _record(
    *,
    contract: DatasetContract,
    row_id: str,
    instrument_id: str,
    capture_id: str,
    report_period: str,
) -> PublishedRecordInput:
    """Build one atemporal record with the exact key order declared by its contract."""
    payload = _payload(
        instrument_id=instrument_id,
        capture_id=capture_id,
        report_period=report_period,
    )
    if contract.dataset_id != SELECTED_DATASET_ID:
        payload = {"row_id": row_id, **payload}
        key_values = [row_id]
    else:
        key_values = [instrument_id, capture_id, "fzb", report_period]
    return PublishedRecordInput(
        business_key=json.dumps(key_values, ensure_ascii=False, separators=(",", ":")),
        observed_at=datetime(2026, 10, 27, 1, tzinfo=UTC),
        payload=payload,
        instrument_id=instrument_id if contract.dataset_id == SELECTED_DATASET_ID else None,
        capture_id=capture_id if contract.dataset_id == SELECTED_DATASET_ID else None,
    )


def _publish(
    store: PublicationStore,
    contract: DatasetContract,
    records: tuple[PublishedRecordInput, ...],
    publication_key: str,
) -> None:
    """Create one explicit fixture publication under its canonical contract hash."""
    store.publish(
        dataset_id=contract.dataset_id,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="per-field",
        adjustment_basis="not_applicable",
        quality={"status": "unknown", "report_id": None, "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key=publication_key,
        records=records,
    )


def _publish_selected_facts(store: PublicationStore, contract: DatasetContract) -> None:
    """Publish two reports in one capture plus facts for another instrument/capture."""
    _publish(
        store,
        contract,
        (
            _record(
                contract=contract,
                row_id="unused-a",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_A,
                report_period="2024-06-30",
            ),
            _record(
                contract=contract,
                row_id="unused-b",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_A,
                report_period="2024-09-30",
            ),
            _record(
                contract=contract,
                row_id="unused-other",
                instrument_id=OTHER_INSTRUMENT,
                capture_id=CAPTURE_OTHER,
                report_period="2024-09-30",
            ),
        ),
        "fixture-sina-capture-publication-1",
    )
    _publish(
        store,
        contract,
        (
            _record(
                contract=contract,
                row_id="unused-c",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_B,
                report_period="2024-09-30",
            ),
        ),
        "fixture-sina-capture-publication-2",
    )


def _publish_legacy_fact(store: PublicationStore, contract: DatasetContract) -> None:
    """Publish a legacy one-filter table that also contains pair-shaped payload data."""
    _publish(
        store,
        contract,
        (
            _record(
                contract=contract,
                row_id="legacy-row-1",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_A,
                report_period="2024-09-30",
            ),
        ),
        "fixture-legacy-pair-shaped-payload",
    )


def _publish_invalid_profile_fact(store: PublicationStore, contract: DatasetContract) -> None:
    """Publish a row for a registered contract whose reserved profile is malformed."""
    _publish(
        store,
        contract,
        (
            _record(
                contract=contract,
                row_id="invalid-profile-row",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_A,
                report_period="2024-09-30",
            ),
        ),
        "fixture-invalid-capture-profile",
    )


@pytest.mark.asyncio
async def test_selected_capture_pages_are_pinned_and_expose_selection_everywhere(
    capture_api: CaptureApiFixture,
) -> None:
    """A selected snapshot excludes other captures and returns its exact scope descriptor."""
    first = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
            "page_size": 1,
        },
    )
    assert first.status_code == 200
    first_page = first.json()
    expected_selection = {"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A}
    assert first_page["selection"] == expected_selection
    assert first_page["meta"]["snapshot"]["selection"] == expected_selection
    assert first_page["window"] == {"start": None, "end": None, "end_exclusive": True}
    assert first_page["record_count"] == 2
    assert len(first_page["rows"]) == 1
    assert first_page["rows"][0]["business_date"] is None
    assert first_page["rows"][0]["payload"]["instrument_id"] == INSTRUMENT
    assert first_page["rows"][0]["payload"]["capture_id"] == CAPTURE_A
    assert first_page["has_more"] is True

    _publish(
        capture_api.store,
        capture_api.selected_contract,
        (
            _record(
                contract=capture_api.selected_contract,
                row_id="unused-new-capture",
                instrument_id=INSTRUMENT,
                capture_id=CAPTURE_B,
                report_period="2024-12-31",
            ),
        ),
        "fixture-sina-capture-publication-after-snapshot",
    )
    second = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={
            "snapshot_id": first_page["snapshot_id"],
            "cursor": first_page["next_cursor"],
        },
    )
    assert second.status_code == 200
    second_page = second.json()
    assert second_page["selection"] == expected_selection
    assert second_page["meta"]["snapshot"]["selection"] == expected_selection
    assert second_page["rows"][0]["payload"]["capture_id"] == CAPTURE_A
    assert second_page["has_more"] is False

    repeated_pair = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={
            "snapshot_id": first_page["snapshot_id"],
            "cursor": first_page["next_cursor"],
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
        },
    )
    assert repeated_pair.status_code == 200
    assert repeated_pair.json()["selection"] == expected_selection

    mismatch = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={
            "snapshot_id": first_page["snapshot_id"],
            "cursor": first_page["next_cursor"],
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_B,
        },
    )
    half_pair = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={"snapshot_id": first_page["snapshot_id"], "instrument_id": INSTRUMENT},
    )
    assert mismatch.status_code == half_pair.status_code == 422
    assert mismatch.json()["error"]["message"] == half_pair.json()["error"]["message"]


@pytest.mark.asyncio
async def test_post_snapshot_requires_pair_and_descriptor_first_page_may_omit_it(
    capture_api: CaptureApiFixture,
) -> None:
    """The POST contract pins selection in metadata reused by descriptor and page routes."""
    missing = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={"dataset_id": SELECTED_DATASET_ID},
    )
    half = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={"dataset_id": SELECTED_DATASET_ID, "instrument_id": INSTRUMENT},
    )
    assert missing.status_code == half.status_code == 422

    created = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": SELECTED_DATASET_ID,
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
            "page_size": 1,
        },
    )
    assert created.status_code == 201
    snapshot = created.json()
    expected_selection = {"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A}
    assert snapshot["selection"] == expected_selection

    first_page = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={"snapshot_id": snapshot["snapshot_id"]},
    )
    descriptor = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/snapshots/{snapshot['snapshot_id']}",
        params={"dataset_id": SELECTED_DATASET_ID},
    )
    assert first_page.status_code == descriptor.status_code == 200
    assert first_page.json()["selection"] == expected_selection
    assert first_page.json()["meta"]["snapshot"]["selection"] == expected_selection
    assert descriptor.json()["selection"] == expected_selection


@pytest.mark.asyncio
async def test_reserved_profile_rejects_invalid_selector_shapes_and_extra_body_fields(
    capture_api: CaptureApiFixture,
) -> None:
    """First-page query and JSON body selectors are paired, exact, and sanitized."""
    invalid_queries = (
        {},
        {"instrument_id": INSTRUMENT},
        {"capture_id": CAPTURE_A},
        {"instrument_id": INSTRUMENT, "capture_id": "A" * 64},
        {"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A, "unexpected": "x"},
    )
    for params in invalid_queries:
        response = await _request(
            capture_api.app,
            "GET",
            f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
            params=params,
        )
        assert response.status_code == 422
        assert "capture_id" not in response.json()["error"]["message"]
        assert INSTRUMENT not in response.text

    extra_body = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": SELECTED_DATASET_ID,
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
            "extra": "rejected",
        },
    )
    assert extra_body.status_code == 422
    assert "extra" not in extra_body.text

    repeated = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records"
        f"?instrument_id={INSTRUMENT}&instrument_id={INSTRUMENT}&capture_id={CAPTURE_A}",
    )
    assert repeated.status_code == 422
    assert INSTRUMENT not in repeated.text


@pytest.mark.asyncio
async def test_legacy_profile_keeps_unselected_response_shape_and_rejects_pair(
    capture_api: CaptureApiFixture,
) -> None:
    """An older non-reserved filter tuple keeps unfiltered metadata without null selection."""
    created = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={"dataset_id": LEGACY_DATASET_ID},
    )
    assert created.status_code == 201
    assert "selection" not in created.json()

    first_page = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{LEGACY_DATASET_ID}/records",
        params={"snapshot_id": created.json()["snapshot_id"]},
    )
    descriptor = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/snapshots/{created.json()['snapshot_id']}",
        params={"dataset_id": LEGACY_DATASET_ID},
    )
    assert first_page.status_code == descriptor.status_code == 200
    assert "selection" not in first_page.json()
    assert "selection" not in first_page.json()["meta"]["snapshot"]
    assert "selection" not in descriptor.json()

    selector = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{LEGACY_DATASET_ID}/records",
        params={"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A},
    )
    post_selector = await _request(
        capture_api.app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": LEGACY_DATASET_ID,
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
        },
    )
    assert selector.status_code == post_selector.status_code == 422


@pytest.mark.asyncio
async def test_invalid_reserved_contract_fails_closed_on_read(
    capture_api: CaptureApiFixture,
) -> None:
    """A malformed exact reserved marker cannot downgrade to legacy unfiltered reads."""
    detail = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{INVALID_PROFILE_DATASET_ID}",
    )
    selected = await _request(
        capture_api.app,
        "GET",
        f"/api/v2/datasets/{INVALID_PROFILE_DATASET_ID}/records",
        params={"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A},
    )
    assert detail.status_code == selected.status_code == 503
    assert detail.json()["error"]["code"] == "published_data_unavailable"
    assert selected.json()["error"]["code"] == "published_data_unavailable"


@pytest.mark.asyncio
async def test_persisted_snapshot_scope_must_match_contract_profile(
    capture_api: CaptureApiFixture,
) -> None:
    """Reserved/unselected and legacy/paired stored snapshots fail on descriptor and page."""
    selected_publication = capture_api.store.list_datasets()
    selected_summary = next(
        summary for summary in selected_publication if summary.dataset_id == SELECTED_DATASET_ID
    )
    legacy_summary = next(
        summary for summary in selected_publication if summary.dataset_id == LEGACY_DATASET_ID
    )
    selected_scope = require_published_access(
        capture_api.principal,
        SELECTED_DATASET_ID,
        "snapshot.create",
        capture_api.grants,
    ).binding_sha256
    legacy_scope = require_published_access(
        capture_api.principal,
        LEGACY_DATASET_ID,
        "snapshot.create",
        capture_api.grants,
    ).binding_sha256

    null_reserved = capture_api.store.create_snapshot(
        SELECTED_DATASET_ID,
        publication_id=selected_summary.publication_id,
        access_scope=selected_scope,
    )
    paired_legacy = capture_api.store.create_snapshot(
        LEGACY_DATASET_ID,
        publication_id=legacy_summary.publication_id,
        access_scope=legacy_scope,
        instrument_id=INSTRUMENT,
        capture_id=CAPTURE_A,
    )

    for dataset_id, snapshot in (
        (SELECTED_DATASET_ID, null_reserved),
        (LEGACY_DATASET_ID, paired_legacy),
    ):
        descriptor = await _request(
            capture_api.app,
            "GET",
            f"/api/v2/snapshots/{snapshot.snapshot_id}",
            params={"dataset_id": dataset_id},
        )
        page = await _request(
            capture_api.app,
            "GET",
            f"/api/v2/datasets/{dataset_id}/records",
            params={"snapshot_id": snapshot.snapshot_id},
        )
        assert descriptor.status_code == page.status_code == 503


@pytest.mark.asyncio
async def test_authorization_precedes_publication_store_reads_for_selected_request(
    capture_api: CaptureApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed selector cannot make an unauthorized request reach SQL reads."""
    denied_app = _make_app(
        capture_api.store,
        DatasetGrantRegistry(()),
        DatasetContractRegistry((capture_api.selected_contract,)),
        capture_api.principal,
    )

    def fail_if_store_is_read(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise AssertionError("unauthorized request reached the publication store")

    monkeypatch.setattr(capture_api.store, "list_datasets", fail_if_store_is_read)
    monkeypatch.setattr(capture_api.store, "get_publication", fail_if_store_is_read)
    monkeypatch.setattr(capture_api.store, "create_snapshot", fail_if_store_is_read)

    response = await _request(
        denied_app,
        "GET",
        f"/api/v2/datasets/{SELECTED_DATASET_ID}/records",
        params={"instrument_id": INSTRUMENT, "capture_id": CAPTURE_A},
    )
    post_response = await _request(
        denied_app,
        "POST",
        "/api/v2/snapshots",
        json={
            "dataset_id": SELECTED_DATASET_ID,
            "instrument_id": INSTRUMENT,
            "capture_id": CAPTURE_A,
        },
    )
    assert response.status_code == post_response.status_code == 403
    assert response.json()["error"]["code"] == "dataset_unavailable"
    assert post_response.json()["error"]["code"] == "dataset_unavailable"
