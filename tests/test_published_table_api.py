"""HTTP behavior tests for atemporal published-table reads."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import Engine, create_engine

from opendata.api import published_data as published_data_module
from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import (
    AccessContext,
    DatasetGrant,
    DatasetGrantRegistry,
    PublishedAccessError,
    require_published_access,
)
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
from opendata.data.platform_contracts import (
    DatasetContract,
    FieldContract,
    SourceIdentity,
)
from opendata.models.user import User, UserRole
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PublicationStore,
    PublishedPage,
    PublishedRecordInput,
    SnapshotRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

UTC = timezone.utc
TABLE_TEMPORAL_DESCRIPTION = "null for atemporal table records; not a historical business date"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "afi204" / "instrument_reference_v1.json"


@pytest.fixture
def table_api(tmp_path: Path) -> Iterator[tuple[FastAPI, PublicationStore, DatasetContract]]:
    """Mount the real route over owned SQLite state and the frozen synthetic fixture."""
    app, store, contract, engine = _create_table_api(tmp_path)
    try:
        yield app, store, contract
    finally:
        engine.dispose()


def _create_table_api(
    tmp_path: Path,
    *,
    contract_updates: Mapping[str, object] | None = None,
    grant_operations: Sequence[str] = ("data.read", "snapshot.create"),
) -> tuple[FastAPI, PublicationStore, DatasetContract, Engine]:
    """Build a contract, publication, registry, and explicit route dependencies."""
    fixture = _load_fixture()
    contract_payload = _mapping(fixture["contract"])
    if contract_updates:
        contract_payload.update(contract_updates)
    contract = DatasetContract.model_validate_json(json.dumps(contract_payload))
    fixture_rows = _list_of_mappings(fixture["rows"])
    records = tuple(_fixture_record(row) for row in fixture_rows)

    engine = create_engine(f"sqlite:///{tmp_path / 'published-table-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"table-api-test-cursor-signing-key!")
    store.publish(
        dataset_id=contract.dataset_id,
        source_id=contract.source.source_id,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "passed", "report_id": "OD204-W8-TEST", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="wave8-table-fixture-publication",
        records=records,
    )
    grant = DatasetGrant(
        dataset_id=contract.dataset_id,
        domain="equity",
        source_id=contract.source.source_id,
        permitted_operations=tuple(grant_operations),
        purposes=("research",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
        review_evidence="OD204-TEST",
    )
    app = _make_app(
        store,
        DatasetGrantRegistry((grant,)),
        DatasetContractRegistry((contract,)),
    )
    return app, store, contract, engine


def _load_fixture() -> dict[str, object]:
    """Load the shared protocol fixture as a checked JSON object."""
    parsed: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return _mapping(parsed)


def _mapping(value: object) -> dict[str, object]:
    """Return a string-keyed JSON object without trusting decoded input types."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise AssertionError("fixture entry must be a JSON object with string keys")
    return dict(value)


def _list_of_mappings(value: object) -> tuple[dict[str, object], ...]:
    """Return a tuple of checked JSON objects from a fixture array."""
    if not isinstance(value, list):
        raise AssertionError("fixture rows must be a JSON array")
    return tuple(_mapping(item) for item in value)


def _fixture_record(item: Mapping[str, object]) -> PublishedRecordInput:
    """Convert one fixture row into the actual immutable publication input."""
    business_key = item.get("business_key")
    payload_value = item.get("payload")
    if not isinstance(business_key, str):
        raise AssertionError("fixture business_key must be a string")
    return PublishedRecordInput(
        business_key=business_key,
        payload=_mapping(payload_value),
        business_date=None,
        event_at=None,
        observed_at=None,
        source_published_at=None,
        available_at=None,
    )


def _make_app(
    store: PublicationStore,
    grants: DatasetGrantRegistry,
    contracts: DatasetContractRegistry,
    *,
    principal: Principal | None = None,
) -> FastAPI:
    """Mount the v2 router with explicit authenticated and data dependencies."""
    user = User(
        id=7,
        username="table-api-user",
        email="table-api-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal or Principal(user=user)
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


async def _request(
    app: FastAPI,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
) -> Response:
    """Send a request through the ASGI application without process networking."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, params=params)


@pytest.mark.asyncio
async def test_atemporal_table_reads_published_rows_by_business_key_and_paginates(
    table_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared table fixture is published, snapshotted, and read over the six-path API."""
    app, _, contract = table_api
    fixture = _load_fixture()
    expected_rows = tuple(
        sorted(
            _list_of_mappings(fixture["rows"]),
            key=lambda row: str(row["business_key"]),
        )
    )
    first = await _request(
        app,
        f"/api/v2/datasets/{contract.dataset_id}/records",
        params={"page_size": 1},
    )

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["record_kind"] == "table"
    assert first_body["temporal_semantics"] == "none"
    assert first_body["time_field"] is None
    assert first_body["window"] == {"start": None, "end": None, "end_exclusive": True}
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    assert first_body["identity_semantics"]["temporal_fields"]["business_date"] == (
        TABLE_TEMPORAL_DESCRIPTION
    )
    assert (
        first_body["meta"]["snapshot"]["identity_semantics"]["temporal_fields"]["business_date"]
        == TABLE_TEMPORAL_DESCRIPTION
    )
    assert first_body["identity_semantics"]["ordering_fields"] == ["instrument_id"]
    assert first_body["identity_semantics"]["storage_order"] == [
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
    assert first_body["contract_sha256"] == canonical_sha256(contract)
    assert isinstance(first_body["content_sha256"], str)
    assert len(first_body["content_sha256"]) == 64
    assert first_body["rows"][0]["business_key"] == min(
        str(row["business_key"]) for row in expected_rows
    )
    assert first_body["rows"][0]["business_date"] is None
    assert first_body["rows"][0]["payload"] == expected_rows[0]["payload"]
    assert first_body["rows"][0]["event_at"] is None
    assert first_body["rows"][0]["observed_at"] is None
    assert first_body["rows"][0]["source_published_at"] is None
    assert first_body["rows"][0]["available_at"] is None

    def allow_only_data_read(
        principal: Principal,
        dataset_id: str,
        operation: str,
        registry: DatasetGrantRegistry,
        now: datetime | None = None,
        purpose: str = "research",
    ) -> AccessContext:
        if operation != "data.read":
            raise PublishedAccessError
        return require_published_access(
            principal,
            dataset_id,
            operation,
            registry,
            now=now,
            purpose=purpose,
        )

    monkeypatch.setattr(
        published_data_module,
        "require_published_access",
        allow_only_data_read,
    )
    second = await _request(
        app,
        f"/api/v2/datasets/{contract.dataset_id}/records",
        params={
            "snapshot_id": first_body["snapshot_id"],
            "cursor": first_body["next_cursor"],
        },
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["snapshot_id"] == first_body["snapshot_id"]
    assert second_body["rows"][0]["business_key"] > first_body["rows"][0]["business_key"]
    assert second_body["rows"][0]["payload"] == expected_rows[1]["payload"]
    assert second_body["rows"][0]["business_date"] is None
    assert second_body["next_cursor"] is None
    assert second_body["has_more"] is False


@pytest.mark.asyncio
async def test_initial_table_read_requires_both_read_and_snapshot_grants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data.read-only grant cannot create a snapshot or reach the storage write path."""
    app, store, contract, engine = _create_table_api(
        tmp_path,
        grant_operations=("data.read",),
    )
    create_calls = 0
    original_create_snapshot = store.create_snapshot

    def observe_create_snapshot(
        dataset_id: str,
        *,
        publication_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        page_size: int = 100,
        access_scope: str = "default",
        ttl: timedelta = timedelta(hours=24),
    ) -> SnapshotRecord:
        nonlocal create_calls
        create_calls += 1
        return original_create_snapshot(
            dataset_id,
            publication_id=publication_id,
            start_date=start_date,
            end_date=end_date,
            page_size=page_size,
            access_scope=access_scope,
            ttl=ttl,
        )

    monkeypatch.setattr(store, "create_snapshot", observe_create_snapshot)
    try:
        response = await _request(
            app,
            f"/api/v2/datasets/{contract.dataset_id}/records",
        )
        assert response.status_code == 403
        assert response.json()["error"]["message"] == "Dataset is unavailable"
        assert create_calls == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_table_record_with_non_null_business_date_fails_closed(
    table_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API rejects stored table facts that attach a historical date bucket."""
    app, store, contract = table_api
    original_read_page = store.read_page

    def non_null_business_date(
        snapshot_id: str,
        *,
        cursor: str | None = None,
        access_scope: str = "default",
    ) -> PublishedPage:
        page = original_read_page(snapshot_id, cursor=cursor, access_scope=access_scope)
        changed = replace(page.records[0], business_date=date(2026, 10, 22))
        return replace(page, records=(changed, *page.records[1:]))

    monkeypatch.setattr(store, "read_page", non_null_business_date)
    response = await _request(app, f"/api/v2/datasets/{contract.dataset_id}/records")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"


@pytest.mark.asyncio
async def test_table_business_key_is_revalidated_against_payload(
    table_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic canonical business-key validation still protects table records."""
    app, store, contract = table_api
    original_read_page = store.read_page

    def mismatched_business_key(
        snapshot_id: str,
        *,
        cursor: str | None = None,
        access_scope: str = "default",
    ) -> PublishedPage:
        page = original_read_page(snapshot_id, cursor=cursor, access_scope=access_scope)
        changed = replace(page.records[0], business_key='["DIFFERENT"]')
        return replace(page, records=(changed, *page.records[1:]))

    monkeypatch.setattr(store, "read_page", mismatched_business_key)
    response = await _request(app, f"/api/v2/datasets/{contract.dataset_id}/records")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "published_data_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "params", "expected_code"),
    (
        ({"pit_policy": "required"}, {}, "unsupported_point_in_time_policy"),
        (
            {
                "temporal_semantics": "business_date",
                "time_field": "status_as_of",
                "ordering": ("status_as_of", "instrument_id"),
            },
            {},
            "unsupported_time_semantics",
        ),
        ({}, {"start_date": "2026-10-21", "end_date": "2026-10-23"}, "date_bounds_unsupported"),
        (
            {"ordering": ("market", "instrument_id")},
            {},
            "unsupported_ordering",
        ),
        (
            {"business_key": ("start_boundary_unknown",), "ordering": ("start_boundary_unknown",)},
            {},
            "unsupported_business_key",
        ),
    ),
)
async def test_table_rejects_unsupported_contracts_and_date_bounds(
    tmp_path: Path,
    updates: Mapping[str, object],
    params: Mapping[str, str],
    expected_code: str,
) -> None:
    """Only the frozen atemporal, non-PIT table contract is eligible for this route."""
    app, _, contract, engine = _create_table_api(tmp_path, contract_updates=updates)
    try:
        response = await _request(
            app,
            f"/api/v2/datasets/{contract.dataset_id}/records",
            params=dict(params),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == expected_code
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_time_series_identity_semantics_and_date_window_output_are_unchanged(
    tmp_path: Path,
) -> None:
    """The table support leaves prior business-date time-series responses intact."""
    engine = create_engine(f"sqlite:///{tmp_path / 'published-timeseries-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"table-api-test-cursor-signing-key!")
    contract = DatasetContract(
        dataset_id="equity.daily.fixture",
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
        source=SourceIdentity(source_id="provider.fixture", source="fixture-provider"),
        adjustment_basis="raw",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )
    trade_day = date(2026, 10, 22)
    store.publish(
        dataset_id=contract.dataset_id,
        source_id=contract.source.source_id,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "passed", "report_id": "OD204-W8-TS", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="wave8-timeseries-fixture-publication",
        records=(
            PublishedRecordInput(
                business_key='["AAA","2026-10-22"]',
                payload={
                    "instrument_id": "AAA",
                    "trade_date": trade_day.isoformat(),
                    "close": 100.0,
                },
                business_date=trade_day,
            ),
        ),
    )
    grant = DatasetGrant(
        dataset_id=contract.dataset_id,
        domain="equity",
        source_id=contract.source.source_id,
        permitted_operations=("data.read", "snapshot.create"),
        purposes=("research",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2027, 1, 1, tzinfo=UTC),
        review_evidence="OD204-TEST",
    )
    app = _make_app(
        store,
        DatasetGrantRegistry((grant,)),
        DatasetContractRegistry((contract,)),
    )
    try:
        response = await _request(
            app,
            f"/api/v2/datasets/{contract.dataset_id}/records",
            params={"start_date": "2026-10-21", "end_date": "2026-10-23"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["identity_semantics"]["temporal_fields"] == {
            "business_date": (
                "business-date bucket; null ordering last (route rejects null time values)"
            ),
            "observed_at": "source observation time",
            "source_published_at": "source publication time",
            "available_at": "platform availability time",
            "event_at": "source event time",
        }
        assert body["identity_semantics"]["temporal_semantics"] == "business_date"
        assert body["identity_semantics"]["time_field"] == "trade_date"
        assert body["window"] == {
            "start": "2026-10-21",
            "end": "2026-10-23",
            "end_exclusive": True,
        }
        assert body["rows"][0]["business_date"] == "2026-10-22"
    finally:
        engine.dispose()
