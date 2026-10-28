"""HTTP contract tests for pinned Sina FZB capture-candidate discovery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

import opendata.api.published_data as published_data_module
from opendata.api.dependencies import Principal, get_current_principal
from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
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
from opendata.data.providers.sina.financial_statement_publication import (
    build_sina_fzb_observation_contract,
)
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
DATASET_ID = "sina_fzb_report_observations"
SOURCE_ID = "sina-fzb"
SYMBOL = "600000.SH"
OTHER_SYMBOL = "000001.SZ"
CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64
CAPTURE_C = "c" * 64
CAPTURE_D = "d" * 64


@pytest.fixture
def sina_capture_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FastAPI, PublicationStore, str, str]]:
    """Build two isolated SQLite publication batches with real v2 route wiring."""
    monkeypatch.setattr(published_data_module, "_SINA_CAPTURE_DISCOVERY_ENABLED", True)
    engine = create_engine(f"sqlite:///{tmp_path / 'sina-capture-api.db'}")
    PUBLICATION_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE INDEX "ix_od_fact_capture_discovery" '
            'ON "od_published_fact_versions" '
            '("dataset_id", "publication_id", "instrument_id", '
            '"publication_sequence", "is_deleted", "observed_at", "capture_id")'
        )
    store = PublicationStore(engine, cursor_signing_key=b"test-capture-signing-key-32-bytes!")
    contract = build_sina_fzb_observation_contract()
    contract_hash = canonical_sha256(contract)

    first_publication = store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="source_native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "unknown", "report_id": None, "issue_count": 0},
        provenance={"contract_sha256": contract_hash},
        idempotency_key="sina-capture-discovery-first",
        records=(
            _record(SYMBOL, CAPTURE_A, "2024-12-31", "2026-10-25T10:00:00+00:00"),
            _record(SYMBOL, CAPTURE_A, "2023-12-31", "2026-10-25T10:00:00+00:00"),
            _record(SYMBOL, CAPTURE_B, "2024-12-31", "2026-10-26T10:00:00+00:00"),
            _record(SYMBOL, CAPTURE_C, "2024-12-31", "2026-10-26T10:00:00+00:00"),
            _record(OTHER_SYMBOL, CAPTURE_D, "2024-12-31", "2026-10-27T10:00:00+00:00"),
        ),
    )
    second_publication = store.publish(
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        unit="source_native",
        adjustment_basis=contract.adjustment_basis,
        quality={"status": "unknown", "report_id": None, "issue_count": 0},
        provenance={"contract_sha256": contract_hash},
        idempotency_key="sina-capture-discovery-second",
        records=(_record(SYMBOL, CAPTURE_D, "2025-12-31", "2026-10-27T10:00:00+00:00"),),
    )

    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=DATASET_ID,
                domain="equity",
                source_id=SOURCE_ID,
                permitted_operations=("data.read",),
                purposes=("research",),
                valid_from=datetime(2000, 1, 1, tzinfo=UTC),
                valid_until=datetime(2100, 1, 1, tzinfo=UTC),
                review_evidence="OD204-TEST",
            ),
        )
    )
    contracts = DatasetContractRegistry((contract,))
    app = _make_app(store, grants, contracts)
    try:
        yield (
            app,
            store,
            first_publication.publication_id,
            second_publication.publication_id,
        )
    finally:
        engine.dispose()


def _record(
    instrument_id: str,
    capture_id: str,
    report_period: str,
    observed_at: str,
) -> PublishedRecordInput:
    """Build a source-native observation row with independent observation time."""
    payload = {
        "instrument_id": instrument_id,
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": report_period,
        "source_paper_code": "20260430",
        "published_on": "2026-04-30",
        "publication_precision": "day",
        "source_update_epoch_seconds": 1_777_488_000,
        "source_updated_at": "2026-04-30T00:00:00+00:00",
        "source_updated_at_timezone": "UTC",
        "retrieved_at_utc": observed_at,
        "currency": "CNY",
        "period_metadata": {},
        "report_metadata": {},
        "entries": [],
    }
    return PublishedRecordInput(
        business_key=json.dumps(
            (instrument_id, capture_id, "fzb", report_period),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        payload=payload,
        observed_at=datetime.fromisoformat(observed_at),
        instrument_id=instrument_id,
        capture_id=capture_id,
    )


def _make_app(
    store: PublicationStore,
    grants: DatasetGrantRegistry,
    contracts: DatasetContractRegistry,
) -> FastAPI:
    """Mount the published-data router with synthetic caller and local dependencies."""
    user = User(
        id=7,
        username="capture-api-user",
        email="capture-api-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=user)
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_publication_store] = lambda: store
    return app


async def _request(app: FastAPI, *, dataset_id: str = DATASET_ID, **kwargs: Any) -> Any:
    """Send an isolated ASGI request without starting application lifespan tasks."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(
            f"/api/v2/datasets/{dataset_id}/capture-candidates",
            **kwargs,
        )


@pytest.mark.asyncio
async def test_candidates_are_pinned_deduplicated_and_explicitly_non_pit(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """Pages contain only one publication batch and never describe observations as PIT."""
    app, _, publication_id, _ = sina_capture_api
    first = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": SYMBOL,
            "page_size": 1,
        },
    )

    assert first.status_code == 200
    first_body = first.json()
    assert first_body["publication_id"] == publication_id
    assert first_body["instrument_id"] == SYMBOL
    assert first_body["scope"] == "publication_batch_only"
    assert first_body["empty_result_semantics"] == "no_matching_rows_in_pinned_publication"
    assert first_body["observed_at_semantics"] == "capture_observation_time_not_point_in_time"
    assert first_body["pit_policy"] == "unsupported"
    assert first_body["items"] == [
        {"capture_id": CAPTURE_B, "observed_at": "2026-10-26T10:00:00+00:00"}
    ]
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]
    assert first.headers["cache-control"] == "private, no-store"

    continuation = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": SYMBOL,
            "page_size": 1,
            "cursor": first_body["next_cursor"],
        },
    )
    continuation_body = continuation.json()
    assert continuation.status_code == 200
    assert continuation_body["publication_id"] == publication_id
    assert continuation_body["items"] == [
        {"capture_id": CAPTURE_C, "observed_at": "2026-10-26T10:00:00+00:00"}
    ]
    assert continuation_body["has_more"] is True

    final_page = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": SYMBOL,
            "page_size": 1,
            "cursor": continuation_body["next_cursor"],
        },
    )
    assert final_page.status_code == 200
    assert final_page.json()["items"] == [
        {"capture_id": CAPTURE_A, "observed_at": "2026-10-25T10:00:00+00:00"}
    ]
    assert final_page.json()["has_more"] is False


@pytest.mark.asyncio
async def test_cursor_binds_publication_instrument_and_page_size(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """A continuation cannot drift to another publication or selection contract."""
    app, _, publication_id, later_publication_id = sina_capture_api
    first = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": SYMBOL,
            "page_size": 1,
        },
    )
    cursor = first.json()["next_cursor"]

    changed_instrument = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": OTHER_SYMBOL,
            "page_size": 1,
            "cursor": cursor,
        },
    )
    changed_page_size = await _request(
        app,
        params={
            "publication_id": publication_id,
            "instrument_id": SYMBOL,
            "page_size": 2,
            "cursor": cursor,
        },
    )
    changed_publication = await _request(
        app,
        params={
            "publication_id": later_publication_id,
            "instrument_id": SYMBOL,
            "page_size": 1,
            "cursor": cursor,
        },
    )

    for response in (changed_instrument, changed_page_size, changed_publication):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_capture_discovery_cursor"


@pytest.mark.asyncio
async def test_cursor_is_authenticated_and_bound_to_principal(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """Tampered tokens and tokens created by another authorized subject are rejected."""
    app, _, publication_id, _ = sina_capture_api
    params = {"publication_id": publication_id, "instrument_id": SYMBOL, "page_size": 1}
    first = await _request(app, params=params)
    cursor = first.json()["next_cursor"]
    tampered = ("A" if cursor[0] != "A" else "B") + cursor[1:]

    rejected = await _request(app, params={**params, "cursor": tampered})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "invalid_capture_discovery_cursor"

    other_user = User(
        id=99,
        username="other-capture-user",
        email="other-capture-user@example.test",
        hashed_password="not-a-real-password-hash",
        role=UserRole.USER,
        is_active=True,
    )
    app.dependency_overrides[get_current_principal] = lambda: Principal(user=other_user)
    cross_subject = await _request(app, params={**params, "cursor": cursor})

    assert cross_subject.status_code == 422
    assert cross_subject.json()["error"]["code"] == "invalid_capture_discovery_cursor"


@pytest.mark.asyncio
async def test_candidates_require_explicit_publication_and_instrument(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """Discovery refuses implicit publication-head and instrument selection."""
    app, _, publication_id, _ = sina_capture_api
    missing_publication = await _request(app, params={"instrument_id": SYMBOL})
    missing_instrument = await _request(app, params={"publication_id": publication_id})

    for response in (missing_publication, missing_instrument):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_empty_batch_result_does_not_claim_historical_absence(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """An instrument absent from the selected batch yields scoped empty metadata only."""
    app, _, _, later_publication_id = sina_capture_api
    response = await _request(
        app,
        params={
            "publication_id": later_publication_id,
            "instrument_id": OTHER_SYMBOL,
            "page_size": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["has_more"] is False
    assert body["scope"] == "publication_batch_only"
    assert body["empty_result_semantics"] == "no_matching_rows_in_pinned_publication"
    assert body["pit_policy"] == "unsupported"


@pytest.mark.asyncio
async def test_route_is_not_available_for_other_granted_datasets(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
) -> None:
    """A different valid data grant cannot widen the Sina-only endpoint."""
    app, _, publication_id, _ = sina_capture_api
    unrelated_grant = DatasetGrant(
        dataset_id="equity.daily",
        domain="equity",
        source_id="provider.alpha",
        permitted_operations=("data.read",),
        purposes=("research",),
        valid_from=datetime(2000, 1, 1, tzinfo=UTC),
        valid_until=datetime(2100, 1, 1, tzinfo=UTC),
        review_evidence="OD204-TEST",
    )
    app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry((unrelated_grant,))
    response = await _request(
        app,
        dataset_id="equity.daily",
        params={"publication_id": publication_id, "instrument_id": SYMBOL},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_route_fails_closed_before_auth_or_store_dependencies(
    sina_capture_api: tuple[FastAPI, PublicationStore, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default-off route reveals no grant state and never resolves its store."""
    app, _, publication_id, _ = sina_capture_api
    monkeypatch.setattr(published_data_module, "_SINA_CAPTURE_DISCOVERY_ENABLED", False)
    dependency_calls: list[str] = []

    def forbidden_dependency(name: str) -> Any:
        def call() -> None:
            dependency_calls.append(name)
            raise AssertionError(f"disabled route resolved {name}")

        return call

    app.dependency_overrides[get_current_principal] = forbidden_dependency("principal")
    app.dependency_overrides[get_dataset_grants] = forbidden_dependency("grants")
    app.dependency_overrides[get_dataset_contracts] = forbidden_dependency("contracts")
    app.dependency_overrides[get_publication_store] = forbidden_dependency("store")

    response = await _request(
        app,
        params={"publication_id": publication_id, "instrument_id": SYMBOL},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert dependency_calls == []
