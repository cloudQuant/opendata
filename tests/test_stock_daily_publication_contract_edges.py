"""Negative contract checks for the pinned stock-daily publication API."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, NoReturn

import pytest
from fastapi import HTTPException

pytest_plugins = ("tests.test_stock_daily_publication_api",)

from opendata.api.published_data import _quality_receipt  # noqa: E402
from opendata.api.stock_daily_publication import (  # noqa: E402
    DATASET_ID,
    _validate_stock_daily_contract,
    _validate_stock_rows,
    _validate_stock_selection,
)
from opendata.pipeline.publication_store import (  # noqa: E402
    PublicationRecord,
    PublishedRecord,
)
from tests import test_stock_daily_publication_api as stock_daily_fixtures  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from fastapi import FastAPI

    from opendata.data.platform_contracts import DatasetContract
    from opendata.pipeline.publication_store import PublicationStore


@pytest.fixture
def stock_daily_publication(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
) -> Iterator[tuple[PublicationStore, DatasetContract, PublicationRecord, str]]:
    """Reuse the API fixture's disposable store and its synthetic publication."""
    _, store, contract, publication_id = stock_daily_api
    publication = store.get_publication(publication_id)
    assert publication is not None
    yield store, contract, publication, publication_id


def _published_row(
    publication: PublicationRecord,
    *,
    business_date: date = stock_daily_fixtures.START,
) -> PublishedRecord:
    """Build one row with the API fixture's canonical raw-bar identity."""
    source_row = stock_daily_fixtures._record(
        stock_daily_fixtures.SYMBOL,
        business_date,
        100.0,
    )
    return PublishedRecord(
        dataset_id=DATASET_ID,
        business_key=source_row.business_key,
        revision=1,
        publication_id=publication.publication_id,
        publication_sequence=publication.sequence,
        business_date=business_date,
        event_at=None,
        observed_at=None,
        source_published_at=None,
        available_at=None,
        payload=source_row.payload,
        record_hash="a" * 64,
    )


def test_contract_rejects_native_unit_currency_and_adjustment_drift(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
) -> None:
    """A stock publication cannot relabel native units or raw adjustments."""
    _, contract, publication, _ = stock_daily_publication
    invalid_contracts = (
        contract.model_copy(
            update={
                "field_units": {
                    **stock_daily_fixtures._EXPECTED_FIELD_UNITS,
                    "volume": "lot",
                }
            }
        ),
        contract.model_copy(
            update={
                "field_currencies": {
                    **stock_daily_fixtures._EXPECTED_FIELD_CURRENCIES,
                    "amount": "USD",
                }
            }
        ),
        contract.model_copy(
            update={
                "field_adjustments": {
                    **stock_daily_fixtures._EXPECTED_FIELD_ADJUSTMENTS,
                    "close": "qfq",
                }
            }
        ),
        contract.model_copy(update={"adjustment_basis": "qfq"}),
        contract.model_copy(update={"currency": "USD"}),
    )

    for invalid_contract in invalid_contracts:
        with pytest.raises(HTTPException) as error:
            _validate_stock_daily_contract(invalid_contract, publication)
        assert error.value.status_code == 503


@pytest.mark.parametrize(
    ("unit", "adjustment_basis"),
    (
        pytest.param("share", "raw", id="publication-unit-is-per-field"),
        pytest.param("per-field", "qfq", id="publication-adjustment-is-raw"),
    ),
)
def test_contract_rejects_publication_level_unit_or_adjustment_drift(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
    *,
    unit: str,
    adjustment_basis: str,
) -> None:
    """Generic publication metadata must agree with the field-level stock contract."""
    _, contract, publication, _ = stock_daily_publication
    invalid_publication = replace(
        publication,
        unit=unit,
        adjustment_basis=adjustment_basis,
    )

    with pytest.raises(HTTPException) as error:
        _validate_stock_daily_contract(contract, invalid_publication)

    assert error.value.status_code == 503


def test_quality_receipt_rejects_unreviewable_or_malformed_quality_state(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
) -> None:
    """Only known quality states and exact nonnegative issue counts are publishable."""
    _, _, publication, _ = stock_daily_publication
    invalid_quality: tuple[Mapping[str, object], ...] = (
        {"status": "rejected", "report_id": None, "issue_count": 0},
        {"status": "warning", "report_id": 17, "issue_count": 0},
        {"status": "warning", "report_id": "report", "issue_count": True},
        {"status": "warning", "report_id": "report", "issue_count": -1},
        {"status": "warning", "report_id": "report"},
        {"report_id": "report", "issue_count": 0},
    )

    for quality in invalid_quality:
        with pytest.raises(HTTPException) as error:
            _quality_receipt(replace(publication, quality=quality))
        assert error.value.status_code == 503


def test_stock_row_validator_checks_snapshot_sequence_before_rows(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
) -> None:
    """A stale snapshot sequence rejects the entire page even with valid rows."""
    store, _, publication, publication_id = stock_daily_publication
    selection = _validate_stock_selection(
        market="CN-SSE",
        symbol=stock_daily_fixtures.SYMBOL,
    )
    snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication_id,
        start_date=stock_daily_fixtures.START,
        end_date=stock_daily_fixtures.END_EXCLUSIVE,
        access_scope="test-stock-daily-contract-edges",
        market="cn",
        symbol=stock_daily_fixtures.SYMBOL,
    )

    with pytest.raises(HTTPException) as error:
        _validate_stock_rows(
            (_published_row(publication),),
            selection=selection,
            publication=publication,
            snapshot=replace(snapshot, publication_sequence=publication.sequence + 1),
        )

    assert error.value.status_code == 503


def test_stock_row_validator_rejects_invalid_dates_payload_and_key_encoding(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
) -> None:
    """Rows must use a date, stay in the half-open window, and keep the exact key shape."""
    store, _, publication, publication_id = stock_daily_publication
    selection = _validate_stock_selection(
        market="CN-SSE",
        symbol=stock_daily_fixtures.SYMBOL,
    )
    snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication_id,
        start_date=stock_daily_fixtures.START,
        end_date=stock_daily_fixtures.END_EXCLUSIVE,
        access_scope="test-stock-daily-contract-edges",
        market="cn",
        symbol=stock_daily_fixtures.SYMBOL,
    )
    good = _published_row(publication)
    assert good.business_date is not None
    datetime_instead_of_date = replace(
        good,
        business_date=datetime.combine(
            good.business_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
    )
    mismatched_market = replace(good, payload={**good.payload, "market": "CN-SSE"})
    mismatched_trade_date = replace(
        good,
        payload={**good.payload, "trade_date": "2026-10-27"},
    )
    noncanonical_key = replace(
        good,
        business_key='["cn", "600519", "2026-10-26"]',
    )
    invalid_records = (
        replace(good, business_date=None),
        datetime_instead_of_date,
        replace(good, business_date=date(2026, 10, 25)),
        replace(good, business_date=stock_daily_fixtures.END_EXCLUSIVE),
        replace(good, payload=None),
        mismatched_market,
        mismatched_trade_date,
        noncanonical_key,
    )

    for invalid_record in invalid_records:
        with pytest.raises(HTTPException) as error:
            _validate_stock_rows(
                (invalid_record,),
                selection=selection,
                publication=publication,
                snapshot=snapshot,
            )
        assert error.value.status_code == 503


def test_stock_row_validator_rejects_missing_snapshot_bounds(
    stock_daily_publication: tuple[
        PublicationStore,
        DatasetContract,
        PublicationRecord,
        str,
    ],
) -> None:
    """A row page cannot silently drop either fixed date boundary."""
    store, _, publication, publication_id = stock_daily_publication
    selection = _validate_stock_selection(
        market="CN-SSE",
        symbol=stock_daily_fixtures.SYMBOL,
    )
    snapshot = store.create_snapshot(
        DATASET_ID,
        publication_id=publication_id,
        start_date=stock_daily_fixtures.START,
        end_date=stock_daily_fixtures.END_EXCLUSIVE,
        access_scope="test-stock-daily-contract-edges",
        market="cn",
        symbol=stock_daily_fixtures.SYMBOL,
    )

    for incomplete_snapshot in (
        replace(snapshot, start_date=None),
        replace(snapshot, end_date=None),
    ):
        with pytest.raises(HTTPException) as error:
            _validate_stock_rows(
                (_published_row(publication),),
                selection=selection,
                publication=publication,
                snapshot=incomplete_snapshot,
            )
        assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_stock_daily_route_rejects_malformed_quality_before_snapshot_creation(
    stock_daily_api: tuple[FastAPI, PublicationStore, DatasetContract, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted quality receipt is rejected before the route creates a snapshot."""
    app, store, _, publication_id = stock_daily_api
    original_get_publication = store.get_publication
    snapshot_creation_calls: list[None] = []

    def malformed_quality(publication_key: str) -> PublicationRecord | None:
        publication = original_get_publication(publication_key)
        if publication is None:
            return None
        return replace(
            publication,
            quality={"status": "rejected", "report_id": None, "issue_count": 0},
        )

    def forbid_snapshot_creation(*args: object, **kwargs: object) -> NoReturn:
        snapshot_creation_calls.append(None)
        pytest.fail("invalid quality must be rejected before snapshot creation")

    monkeypatch.setattr(store, "get_publication", malformed_quality)
    monkeypatch.setattr(store, "create_snapshot", forbid_snapshot_creation)
    response = await stock_daily_fixtures._request(
        app,
        params=stock_daily_fixtures._first_page_params(publication_id),
    )

    assert response.status_code == 503
    assert snapshot_creation_calls == []
