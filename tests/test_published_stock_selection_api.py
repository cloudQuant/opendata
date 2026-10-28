"""HTTP tests for exact market/symbol filters on published business-date series."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, get_args

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine

import opendata.api.published_data as published_data_module
from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import (
    DatasetGrant,
    DatasetGrantRegistry,
    require_published_access,
)
from opendata.api.published_data import (
    CreateSnapshotRequest,
    InstrumentCaptureSelectionResponse,
    SnapshotResponse,
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
from opendata.data.market_symbol_selection import MARKET_SYMBOL_FILTER_FIELDS
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.models.user import User, UserRole
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PublicationStore,
    PublishedRecordInput,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

UTC = timezone.utc
DATASET_ID = "cn-equity-daily-api-fixture"
MARKET = "CN-SH"
SYMBOL = "SSE:600519"
OTHER_SYMBOL = "SSE:000001"
OTHER_MARKET = "CN-SZ"


@pytest.fixture
def stock_api(
    tmp_path: Path,
) -> Iterator[tuple[FastAPI, PublicationStore, DatasetContract]]:
    """Mount the real v2 route over isolated SQLite and a synthetic publication."""
    engine = create_engine(f"sqlite:///{tmp_path / 'published-stock-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"stock-api-cursor-signing-key-32b!")
    contract = _stock_contract()
    store.publish(
        dataset_id=contract.dataset_id,
        source_id=contract.source.source_id,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "passed", "report_id": "OD204-STOCK-TEST", "issue_count": 0},
        provenance={"contract_sha256": canonical_sha256(contract)},
        idempotency_key="published-stock-selection-api-fixture",
        records=(
            _record(MARKET, SYMBOL, date(2025, 1, 2), 102.0),
            _record(OTHER_MARKET, SYMBOL, date(2025, 1, 1), 91.0),
            _record(MARKET, OTHER_SYMBOL, date(2025, 1, 1), 88.0),
            _record(MARKET, SYMBOL, date(2025, 1, 1), 101.0),
        ),
    )
    grant = DatasetGrant(
        dataset_id=contract.dataset_id,
        domain="equity",
        source_id=contract.source.source_id,
        permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
        purposes=("research",),
        valid_from=datetime(2000, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-TEST",
    )
    user = User(
        id=17,
        username="stock-api-user",
        email="stock-api-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=user)
    app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry((grant,))
    app.dependency_overrides[get_dataset_contracts] = lambda: DatasetContractRegistry((contract,))
    app.dependency_overrides[get_publication_store] = lambda: store
    try:
        yield app, store, contract
    finally:
        engine.dispose()


def _stock_contract() -> DatasetContract:
    """Build a daily series whose selector fields are part of its exact business key."""
    return DatasetContract(
        dataset_id=DATASET_ID,
        schema_version="1.0.0",
        semantic_version="stock-daily-raw-v1",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=MARKET_SYMBOL_FILTER_FIELDS,
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(source_id="eastmoney.stock.daily", source="eastmoney"),
        adjustment_basis="raw",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _record(market: str, symbol: str, trade_date: date, close: float) -> PublishedRecordInput:
    """Create a synthetic daily row with exact indexed selector values."""
    business_key = json.dumps(
        (market, symbol, trade_date.isoformat()),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return PublishedRecordInput(
        business_key=business_key,
        payload={
            "market": market,
            "symbol": symbol,
            "trade_date": trade_date.isoformat(),
            "close": close,
        },
        business_date=trade_date,
        market=market,
        symbol=symbol,
    )


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    params: Mapping[str, str | int] | None = None,
    body: Mapping[str, Any] | None = None,
) -> Response:
    """Send a request over ASGI without opening a listening socket."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, params=params, json=body)


def test_v2_snapshot_models_keep_the_frozen_capture_selection_shape() -> None:
    """REST DTOs expose only the pre-existing instrument/capture selector contract."""
    assert {"market", "symbol"}.isdisjoint(CreateSnapshotRequest.model_fields)
    selection_types = set(get_args(SnapshotResponse.model_fields["selection"].annotation))
    assert selection_types == {InstrumentCaptureSelectionResponse, type(None)}


def test_market_symbol_public_api_is_hard_disabled_by_default() -> None:
    """The market-symbol API remains hard-disabled without a runtime override."""
    assert published_data_module._MARKET_SYMBOL_API_ENABLED is False


@pytest.mark.asyncio
async def test_market_profile_hard_off_blocks_snapshot_create_and_reads(
    stock_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserved market profile cannot create or read public snapshots while hard-off."""
    app, store, contract = stock_api
    create_snapshot = store.create_snapshot

    def unexpected_snapshot_create(*args: object, **kwargs: object) -> object:
        pytest.fail("hard-disabled market profiles must not create public snapshots")

    monkeypatch.setattr(store, "create_snapshot", unexpected_snapshot_create)
    records = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/records",
        params={"start_date": "2025-01-01", "end_date": "2025-01-03"},
    )
    post_snapshot = await _request(
        app,
        "POST",
        "/api/v2/snapshots",
        body={
            "dataset_id": contract.dataset_id,
            "start_date": "2025-01-01",
            "end_date": "2025-01-03",
        },
    )
    assert records.status_code == post_snapshot.status_code == 404

    principal = app.dependency_overrides[get_current_principal]()
    grants = app.dependency_overrides[get_dataset_grants]()
    access = require_published_access(
        principal,
        contract.dataset_id,
        "snapshot.create",
        grants,
    )
    snapshot = create_snapshot(
        contract.dataset_id,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 3),
        access_scope=access.binding_sha256,
        market=MARKET,
        symbol=SYMBOL,
    )

    def unexpected_page_read(*args: object, **kwargs: object) -> object:
        pytest.fail("hard-disabled market profiles must not read public snapshot pages")

    monkeypatch.setattr(store, "read_page", unexpected_page_read)
    metadata = await _request(
        app,
        "GET",
        f"/api/v2/snapshots/{snapshot.snapshot_id}",
        params={"dataset_id": contract.dataset_id},
    )
    page = await _request(
        app,
        "GET",
        f"/api/v2/datasets/{contract.dataset_id}/records",
        params={"snapshot_id": snapshot.snapshot_id},
    )
    assert metadata.status_code == page.status_code == 404


@pytest.mark.asyncio
async def test_market_query_fields_remain_unknown_and_fail_before_store_io(
    stock_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frozen v2 route rejects new market selectors as unsupported query fields."""
    app, store, contract = stock_api

    def unexpected_store_io(*args: object, **kwargs: object) -> object:
        pytest.fail("unknown query fields must be rejected before publication-store I/O")

    for method_name in (
        "list_datasets",
        "get_publication",
        "get_snapshot",
        "create_snapshot",
        "read_page",
    ):
        monkeypatch.setattr(store, method_name, unexpected_store_io)

    path = f"/api/v2/datasets/{contract.dataset_id}/records"
    responses = [
        await _request(
            app,
            "GET",
            path,
            params={"market": MARKET, "symbol": SYMBOL},
        ),
        await _request(
            app,
            "GET",
            f"{path}?market={MARKET}&market={MARKET}&symbol={SYMBOL}",
        ),
        await _request(app, "GET", f"{path}?exchange=SSE"),
    ]
    assert all(response.status_code == 422 for response in responses)
    assert all(response.json()["error"]["code"] == "unsupported_query" for response in responses)


@pytest.mark.asyncio
async def test_snapshot_body_keeps_extra_forbid_for_market_fields(
    stock_api: tuple[FastAPI, PublicationStore, DatasetContract],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Market/symbol body fields remain rejected by the strict frozen snapshot DTO."""
    app, store, contract = stock_api

    def unexpected_store_io(*args: object, **kwargs: object) -> object:
        pytest.fail("extra snapshot fields must be rejected before publication-store I/O")

    for method_name in (
        "list_datasets",
        "get_publication",
        "get_snapshot",
        "create_snapshot",
        "read_page",
    ):
        monkeypatch.setattr(store, method_name, unexpected_store_io)
    response = await _request(
        app,
        "POST",
        "/api/v2/snapshots",
        body={
            "dataset_id": contract.dataset_id,
            "start_date": "2025-01-01",
            "end_date": "2025-01-03",
            "market": MARKET,
            "symbol": SYMBOL,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
