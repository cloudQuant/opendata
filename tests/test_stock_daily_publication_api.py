"""HTTP acceptance examples for the default-off stock-daily v2 publication bridge."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine

from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
from opendata.api.published_data import (
    get_dataset_contracts,
    get_dataset_grants,
    get_publication_store,
)
from opendata.api.stock_daily_capacity import (
    StockDailyCapacityAttestation,
    get_stock_daily_capacity_gate,
)
from opendata.api.stock_daily_publication import (
    _EXPECTED_FIELD_ADJUSTMENTS,
    _EXPECTED_FIELD_CURRENCIES,
    _EXPECTED_FIELD_UNITS,
    DATASET_ID,
    _parse_date,
    _validate_stock_daily_contract,
    _validate_stock_rows,
    _validate_stock_selection,
    router,
)
from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.models.user import User, UserRole
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PublicationStore,
    PublishedRecord,
    PublishedRecordInput,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

UTC = timezone.utc
SOURCE_ID = "akshare.daily"
MARKET = "CN-SSE"
SYMBOL = "600519"
OTHER_SYMBOL = "000001"
START = date(2026, 10, 26)
END_EXCLUSIVE = date(2026, 10, 29)


@pytest.fixture
def stock_daily_api(
    tmp_path: Path,
) -> Iterator[tuple[FastAPI, PublicationStore, DatasetContract, str]]:
    """Mount the v2 route over a disposable SQLite store and one synthetic publication."""
    engine = create_engine(f"sqlite:///{tmp_path / 'stock-daily-publication-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=b"stock-daily-api-cursor-key-32bytes!")
    contract = _contract()
    publication = store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="per-field",
        adjustment_basis="raw",
        quality={
            "status": "warning",
            "report_id": "stock-daily-offline-normalization-v1",
            "issue_count": 0,
        },
        provenance={
            "contract_sha256": canonical_sha256(contract),
            "source_verification": "not_performed",
            "source_history_limit": "unknown",
        },
        idempotency_key="stock-daily-v2-api-fixture",
        records=(
            _record(SYMBOL, START, 100.0),
            _record(SYMBOL, date(2026, 10, 27), 101.0),
            _record(OTHER_SYMBOL, START, 90.0),
        ),
    )
    grant = DatasetGrant(
        dataset_id=DATASET_ID,
        domain="equity",
        source_id=SOURCE_ID,
        permitted_operations=("data.read", "snapshot.create"),
        purposes=("research",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-SYNTHETIC-TEST",
    )
    user = User(
        id=31,
        username="stock-daily-api-user",
        email="stock-daily-api-user@example.test",
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
    app.dependency_overrides[get_stock_daily_capacity_gate] = lambda: _capacity_attestation(
        contract,
        publication.publication_id,
    )
    try:
        yield app, store, contract, publication.publication_id
    finally:
        engine.dispose()


def _capacity_attestation(
    contract: DatasetContract,
    publication_id: str | None,
    *,
    max_window_days: int = 3_660,
    max_page_size: int = 1_000,
) -> StockDailyCapacityAttestation:
    """Build a valid synthetic capacity attestation for the fixture publication."""
    return StockDailyCapacityAttestation(
        schema_version="stock-daily-capacity-v1",
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        semantic_version=contract.semantic_version,
        contract_sha256=canonical_sha256(contract),
        publication_id=publication_id,
        max_window_days=max_window_days,
        max_page_size=max_page_size,
        not_before=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2100, 1, 1, tzinfo=UTC),
        evidence_ref="tests/test_stock_daily_publication_api.py",
        attested_by="synthetic-offline-test",
    )


def _contract() -> DatasetContract:
    """Return the exact raw-bar contract used by the reviewed synthetic flow."""
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
        source=SourceIdentity(source_id=SOURCE_ID, source="akshare"),
        field_units=dict(_EXPECTED_FIELD_UNITS),
        field_currencies=dict(_EXPECTED_FIELD_CURRENCIES),
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments=dict(_EXPECTED_FIELD_ADJUSTMENTS),
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _record(symbol: str, trade_date: date, close: float) -> PublishedRecordInput:
    """Create one normalized synthetic raw daily-bar record."""
    market = "cn"
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
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": 1000.0,
            "amount": 120000.0,
        },
        business_date=trade_date,
        market=market,
        symbol=symbol,
    )


async def _request(
    app: FastAPI,
    *,
    params: Mapping[str, str | int | list[str]],
) -> Response:
    """Send one HTTP request through an in-process ASGI transport."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/v2/stock-daily/records", params=params)


def _first_page_params(publication_id: str, *, page_size: int = 1) -> dict[str, str | int]:
    """Build the explicit publication and half-open interval for page one."""
    return {
        "market": MARKET,
        "symbol": SYMBOL,
        "publication_id": publication_id,
        "start_date": START.isoformat(),
        "end_date": END_EXCLUSIVE.isoformat(),
        "page_size": page_size,
    }


@pytest.mark.asyncio
async def test_first_page_preserves_publication_units_and_unknown_coverage(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """The public page pins the source publication and never promotes coverage."""
    app, _, _, publication_id = stock_daily_api
    response = await _request(app, params=_first_page_params(publication_id))

    assert response.status_code == 200
    body = response.json()
    assert body["dataset_id"] == DATASET_ID
    assert body["publication_id"] == publication_id
    assert body["adjustment_basis"] == "raw"
    assert body["currency"] == "CNY"
    assert body["quality"]["status"] == "warning"
    assert body["field_units"] == _EXPECTED_FIELD_UNITS
    assert body["field_adjustments"] == _EXPECTED_FIELD_ADJUSTMENTS
    assert body["stock_selection"] == {"market": MARKET, "symbol": SYMBOL}
    assert body["meta"]["snapshot"]["stock_selection"] == body["stock_selection"]
    assert body["window"] == {
        "start": START.isoformat(),
        "end": END_EXCLUSIVE.isoformat(),
        "end_exclusive": True,
    }
    assert body["coverage"] == {
        "status": "unknown",
        "scope": "unknown",
        "requested": {"start": START.isoformat(), "end": END_EXCLUSIVE.isoformat()},
        "observed": None,
        "missing_ranges": [],
        "evidence_refs": [],
        "limitation": body["coverage"]["limitation"],
    }
    assert body["coverage"]["status"] == "unknown"
    assert "do not establish complete history" in body["coverage"]["limitation"]
    assert body["rows"][0]["business_key"] == '["cn","600519","2026-10-26"]'
    assert body["rows"][0]["payload"]["market"] == "cn"
    assert body["rows"][0]["payload"]["symbol"] == SYMBOL
    assert body["next_cursor"] is not None
    assert body["has_more"] is True


@pytest.mark.asyncio
async def test_continuation_reuses_identical_snapshot_and_rejects_changed_publication(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Opaque continuation pages stay on one publication, stock and date window."""
    app, _, _, publication_id = stock_daily_api
    first_response = await _request(app, params=_first_page_params(publication_id))
    first = first_response.json()
    next_params = {
        "market": MARKET,
        "symbol": SYMBOL,
        "publication_id": publication_id,
        "snapshot_id": first["snapshot_id"],
        "cursor": first["next_cursor"],
    }

    second_response = await _request(app, params=next_params)
    second = second_response.json()
    assert second_response.status_code == 200
    assert second["meta"]["snapshot"] == first["meta"]["snapshot"]
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["publication_id"] == first["publication_id"]
    assert second["rows"][0]["business_key"] == '["cn","600519","2026-10-27"]'
    assert second["coverage"]["status"] == "unknown"
    assert second["has_more"] is False

    mismatched = {**next_params, "publication_id": "b" * 32}
    mismatch_response = await _request(app, params=mismatched)
    assert mismatch_response.status_code == 422
    assert mismatch_response.json()["error"]["code"] == "invalid_snapshot_request"


@pytest.mark.asyncio
async def test_flag_without_capacity_attestation_keeps_route_hidden(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enable flag alone cannot bypass the digest-pinned capacity gate."""
    app, store, _, publication_id = stock_daily_api
    app.dependency_overrides.pop(get_stock_daily_capacity_gate)
    monkeypatch.setenv("STOCK_DAILY_PUBLICATION_API_ENABLED", "true")
    monkeypatch.delenv("STOCK_DAILY_PUBLICATION_CAPACITY_FILE", raising=False)
    monkeypatch.delenv("STOCK_DAILY_PUBLICATION_CAPACITY_SHA256", raising=False)
    assert "/api/v2/stock-daily/records" not in app.openapi()["paths"]

    def unexpected_store_call(*args: object, **kwargs: object) -> object:
        pytest.fail("gated stock-daily routes must not read publication data")

    monkeypatch.setattr(store, "get_publication", unexpected_store_call)
    response = await _request(app, params=_first_page_params(publication_id))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_missing_snapshot_create_grant_fails_before_publication_lookup(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A data-read grant alone cannot create a public stock snapshot."""
    app, store, _, publication_id = stock_daily_api
    read_only_grant = DatasetGrant(
        dataset_id=DATASET_ID,
        domain="equity",
        source_id=SOURCE_ID,
        permitted_operations=("data.read",),
        purposes=("research",),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-READ-ONLY-TEST",
    )
    app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry((read_only_grant,))

    def unexpected_publication_lookup(*args: object, **kwargs: object) -> object:
        pytest.fail("snapshot-create authorization must precede publication reads")

    monkeypatch.setattr(store, "get_publication", unexpected_publication_lookup)
    response = await _request(app, params=_first_page_params(publication_id))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_rejects_wrong_market_code_unknown_query_duplicate_and_noncanonical_date(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid identity or query shape fails before any publication lookup."""
    app, store, _, publication_id = stock_daily_api

    def unexpected_publication_lookup(*args: object, **kwargs: object) -> object:
        pytest.fail("invalid stock request must be rejected before store access")

    monkeypatch.setattr(store, "get_publication", unexpected_publication_lookup)
    good = _first_page_params(publication_id)
    invalid_responses = [
        await _request(app, params={**good, "market": "CN-SZSE"}),
        await _request(app, params={**good, "market": "CN-BJSE", "symbol": "830001"}),
        await _request(app, params={**good, "symbol": "600519.SH"}),
        await _request(app, params={**good, "adjustment": "qfq"}),
        await _request(app, params={**good, "start_date": "2026-1-26"}),
    ]
    duplicate_response = await _request(
        app,
        params={**good, "market": [MARKET, MARKET]},
    )
    assert all(response.status_code == 422 for response in invalid_responses)
    assert duplicate_response.status_code == 422


@pytest.mark.asyncio
async def test_rejects_invalid_windows_and_cursor_without_snapshot(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Reject invalid business windows and cursor state before storage access."""
    app, _, _, publication_id = stock_daily_api
    good = _first_page_params(publication_id)
    identity = {key: good[key] for key in ("market", "symbol", "publication_id")}
    responses = [
        await _request(app, params={**good, "start_date": "2026-10-26", "end_date": "2026-10-25"}),
        await _request(app, params={**good, "end_date": "2037-01-01"}),
        await _request(app, params={**identity, "cursor": "opaque-cursor"}),
        await _request(app, params=identity),
        await _request(app, params={**good, "start_date": "2026-13-01"}),
    ]

    assert all(response.status_code == 422 for response in responses)
    assert responses[0].json()["error"]["code"] == "invalid_request"
    assert responses[1].json()["error"]["code"] == "invalid_request"
    assert responses[2].json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_rejects_snapshot_window_mutation_and_unknown_snapshot(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Continuation requests cannot change their pinned window or page size."""
    app, _, _, publication_id = stock_daily_api
    first_response = await _request(app, params=_first_page_params(publication_id))
    assert first_response.status_code == 200
    first = first_response.json()
    continuation = {
        "market": MARKET,
        "symbol": SYMBOL,
        "publication_id": publication_id,
        "snapshot_id": first["snapshot_id"],
    }

    changed_window = await _request(
        app,
        params={**continuation, "start_date": START.isoformat()},
    )
    changed_page_size = await _request(app, params={**continuation, "page_size": 2})
    missing_snapshot = await _request(
        app,
        params={**continuation, "snapshot_id": "f" * 32},
    )

    assert changed_window.status_code == 422
    assert changed_page_size.status_code == 422
    assert missing_snapshot.status_code == 404


@pytest.mark.parametrize(
    ("market", "symbol"),
    [
        ("CN-SSE", "6005*"),
        ("CN-BSE", "830001"),
        ("CN-SSE", "abc123"),
        ("CN-SSE", "000001"),
        ("CN-SZSE", "600519"),
        (None, None),
    ],
)
def test_selector_rejects_wildcards_unsupported_markets_and_venue_mismatches(
    market: object,
    symbol: object,
) -> None:
    """Selector boundaries accept only exact six-digit codes for supported venues."""
    with pytest.raises(HTTPException) as error:
        _validate_stock_selection(market=market, symbol=symbol)

    assert error.value.status_code == 422


def test_stock_daily_date_parser_requires_canonical_business_dates() -> None:
    """Date parsing rejects missing, invalid, and alternate ISO representations."""
    assert _parse_date("2026-10-26", "start_date") == START
    for value in (None, "2026-13-01", "2026-W43-1"):
        with pytest.raises(HTTPException) as error:
            _parse_date(value, "start_date")
        assert error.value.status_code == 422


def test_raw_stock_contract_and_rows_are_checked_against_pinned_units_and_identity(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> None:
    """Raw unit drift and row identity drift fail before a DTO can be returned."""
    _, store, contract, publication_id = stock_daily_api
    publication = store.get_publication(publication_id)
    assert publication is not None
    _validate_stock_daily_contract(contract, publication)

    invalid_contract = contract.model_copy(
        update={"field_units": {**_EXPECTED_FIELD_UNITS, "volume": "lot"}}
    )
    with pytest.raises(HTTPException) as contract_error:
        _validate_stock_daily_contract(invalid_contract, publication)
    assert contract_error.value.status_code == 503

    selected_record = PublishedRecord(
        dataset_id=DATASET_ID,
        business_key='["cn","600519","2026-10-26"]',
        revision=1,
        publication_id=publication_id,
        publication_sequence=publication.sequence,
        business_date=START,
        event_at=None,
        observed_at=None,
        source_published_at=None,
        available_at=None,
        payload={
            "market": "cn",
            "symbol": SYMBOL,
            "trade_date": START.isoformat(),
        },
        record_hash="0" * 64,
    )
    snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication_id,
        start_date=START,
        end_date=END_EXCLUSIVE,
        access_scope="test-stock-daily-row-check",
        market="cn",
        symbol=SYMBOL,
    )
    selection = _validate_stock_selection(market=MARKET, symbol=SYMBOL)
    _validate_stock_rows(
        (selected_record,),
        selection=selection,
        publication=publication,
        snapshot=snapshot,
    )
    wrong_record = replace(
        selected_record,
        business_key='["cn","600519","2026-10-27"]',
    )
    invalid_records = (
        wrong_record,
        replace(selected_record, dataset_id="other-stock-dataset"),
        replace(selected_record, publication_id="b" * 32),
        replace(selected_record, publication_sequence=publication.sequence + 1),
        replace(selected_record, event_at=datetime(2026, 10, 26, tzinfo=UTC)),
        replace(selected_record, source_published_at=datetime(2026, 10, 26, tzinfo=UTC)),
        replace(
            selected_record,
            payload={**selected_record.payload, "symbol": OTHER_SYMBOL},
        ),
    )
    for invalid_record in invalid_records:
        with pytest.raises(HTTPException):
            _validate_stock_rows(
                (invalid_record,),
                selection=selection,
                publication=publication,
                snapshot=snapshot,
            )
