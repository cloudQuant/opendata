"""PublishedStore coverage for exact market/symbol snapshot selection."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Engine, create_engine, update

from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PUBLISHED_FACT_VERSIONS,
    SNAPSHOTS,
    PublicationContractConflictError,
    PublicationStore,
    PublishedRecordInput,
    SnapshotError,
    _base64url_decode,
    _canonical_json,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_CURSOR_KEY = b"stock-selection-test-key-32-bytes-minimum"
_OBSERVED_AT = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
_CONTRACT_HASH = "a" * 64


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create an isolated SQLite store using the current published-store schema."""
    database = create_engine(f"sqlite:///{tmp_path / 'stock-selection.sqlite'}")
    PUBLICATION_METADATA.create_all(database)
    try:
        yield database
    finally:
        database.dispose()


def _store(engine: Engine) -> PublicationStore:
    return PublicationStore(engine, cursor_signing_key=_CURSOR_KEY)


def _publish(
    store: PublicationStore,
    key: str,
    records: list[PublishedRecordInput],
):
    return store.publish(
        dataset_id="stock.daily",
        source_id="provider.test",
        schema_version="1",
        semantic_version="1",
        unit="CNY/share",
        adjustment_basis="unadjusted",
        quality={"status": "passed"},
        provenance={"provider": "test", "contract_sha256": _CONTRACT_HASH},
        idempotency_key=key,
        records=records,
    )


def _stock_record(
    market: str,
    symbol: str,
    day: date,
    close: float,
) -> PublishedRecordInput:
    """Build one record with exact selector columns and canonical key order."""
    business_key = _canonical_json([market, symbol, day.isoformat()])
    return PublishedRecordInput(
        business_key=business_key,
        payload={
            "market": market,
            "symbol": symbol,
            "trade_date": day.isoformat(),
            "close": close,
        },
        business_date=day,
        observed_at=_OBSERVED_AT,
        market=market,
        symbol=symbol,
    )


def test_market_symbol_snapshot_uses_exact_pair_and_binds_cursor(engine: Engine) -> None:
    """Pages include only exact pair matches and stock cursors cannot cross snapshots."""
    store = _store(engine)
    first_day = date(2026, 10, 27)
    second_day = date(2026, 10, 28)
    publication = _publish(
        store,
        "stock-pairs",
        [
            _stock_record("CN", "600000", first_day, 10.1),
            _stock_record("CN", "600000", second_day, 10.2),
            _stock_record("cn", "600000", first_day, 90.0),
            _stock_record("CN", "600000A", first_day, 80.0),
        ],
    )

    snapshot = store.create_snapshot(
        "stock.daily",
        publication_id=publication.publication_id,
        page_size=1,
        market="CN",
        symbol="600000",
    )
    lowercase_snapshot = store.create_snapshot(
        "stock.daily",
        publication_id=publication.publication_id,
        market="cn",
        symbol="600000",
    )

    first_page = store.read_page(snapshot.snapshot_id)
    assert snapshot.record_count == 2
    assert [record.business_date for record in first_page.records] == [first_day]
    assert first_page.next_cursor is not None
    cursor_data = json.loads(_base64url_decode(first_page.next_cursor.split(".")[0]))
    assert cursor_data["version"] == 3
    assert cursor_data["selection_profile"] == "market_symbol_v1"
    assert cursor_data["market"] == "CN"
    assert cursor_data["symbol"] == "600000"
    assert snapshot.selection_sha256 != lowercase_snapshot.selection_sha256
    assert snapshot.content_hash != lowercase_snapshot.content_hash

    second_page = store.read_page(snapshot.snapshot_id, cursor=first_page.next_cursor)
    assert [record.business_date for record in second_page.records] == [second_day]
    assert second_page.next_cursor is None
    with pytest.raises(SnapshotError, match="different snapshot"):
        store.read_page(lowercase_snapshot.snapshot_id, cursor=first_page.next_cursor)
    assert lowercase_snapshot.record_count == 1
    assert store.read_page(lowercase_snapshot.snapshot_id).records[0].payload["market"] == "cn"

    with pytest.raises(SnapshotError, match="market is invalid"):
        store.create_snapshot("stock.daily", market="CN ", symbol="600000")


def test_market_symbol_publication_requires_matching_payload_and_canonical_key(
    engine: Engine,
) -> None:
    """Selector metadata must match both payload values and the canonical JSON key."""
    store = _store(engine)
    day = date(2026, 10, 27)
    valid = _stock_record("CN", "600000", day, 10.1)

    with pytest.raises(ValueError, match="selector metadata does not match its payload"):
        _publish(
            store,
            "bad-stock-payload",
            [
                PublishedRecordInput(
                    business_key=valid.business_key,
                    payload={**valid.payload, "market": "cn"},
                    business_date=valid.business_date,
                    market="CN",
                    symbol="600000",
                )
            ],
        )

    with pytest.raises(ValueError, match="canonical JSON array"):
        _publish(
            store,
            "bad-stock-key-format",
            [
                PublishedRecordInput(
                    business_key='["CN", "600000", "2026-10-27"]',
                    payload=valid.payload,
                    business_date=valid.business_date,
                    market="CN",
                    symbol="600000",
                )
            ],
        )

    with pytest.raises(ValueError, match="separate elements"):
        _publish(
            store,
            "bad-stock-key-values",
            [
                PublishedRecordInput(
                    business_key=_canonical_json(["CN", "600001", day.isoformat()]),
                    payload=valid.payload,
                    business_date=valid.business_date,
                    market="CN",
                    symbol="600000",
                )
            ],
        )


def test_incomplete_historical_selector_columns_fail_closed(engine: Engine) -> None:
    """Legacy NULL metadata blocks reads and cannot be backfilled by re-publishing a key."""
    store = _store(engine)
    day = date(2026, 10, 27)
    selected_record = _stock_record("CN", "600000", day, 10.1)
    legacy_record = PublishedRecordInput(
        business_key=selected_record.business_key,
        payload=selected_record.payload,
        business_date=selected_record.business_date,
        observed_at=selected_record.observed_at,
    )
    publication = _publish(
        store,
        "stock-legacy-unmaterialized",
        [legacy_record],
    )

    with pytest.raises(SnapshotError, match="complete stored history"):
        store.create_snapshot(
            "stock.daily",
            publication_id=publication.publication_id,
            market="CN",
            symbol="600000",
        )
    with pytest.raises(PublicationContractConflictError, match="selector metadata is immutable"):
        _publish(
            store,
            "stock-attempt-backfill",
            [_stock_record("CN", "600000", day, 10.2)],
        )


def test_same_pair_tombstone_remains_latest_and_does_not_resurrect_old_row(
    engine: Engine,
) -> None:
    """The latest same-pair tombstone suppresses its prior live revision after ranking."""
    store = _store(engine)
    day = date(2026, 10, 27)
    business_key = _canonical_json(["CN", "600000", day.isoformat()])
    _publish(
        store,
        "stock-before-tombstone",
        [_stock_record("CN", "600000", day, 10.1)],
    )
    latest = _publish(
        store,
        "stock-tombstone-sequence",
        [_stock_record("CN", "600000", day, 10.2)],
    )
    with engine.begin() as connection:
        connection.execute(
            update(PUBLISHED_FACT_VERSIONS)
            .where(
                PUBLISHED_FACT_VERSIONS.c.dataset_id == "stock.daily",
                PUBLISHED_FACT_VERSIONS.c.business_key == business_key,
                PUBLISHED_FACT_VERSIONS.c.revision == 2,
            )
            .values(is_deleted=True)
        )

    snapshot = store.create_snapshot(
        "stock.daily",
        publication_id=latest.publication_id,
        market="CN",
        symbol="600000",
    )
    assert snapshot.record_count == 0
    assert store.read_page(snapshot.snapshot_id).records == ()


def test_tombstone_selector_change_cannot_resurrect_older_stock_revision(engine: Engine) -> None:
    """Every revision, including a tombstone, must retain the business key's exact pair."""
    store = _store(engine)
    day = date(2026, 10, 27)
    business_key = _canonical_json(["CN", "600000", day.isoformat()])
    first = _publish(store, "stock-before-correction", [_stock_record("CN", "600000", day, 10.1)])
    latest = _publish(store, "stock-correction", [_stock_record("CN", "600000", day, 10.2)])
    assert latest.sequence > first.sequence
    with engine.begin() as connection:
        connection.execute(
            update(PUBLISHED_FACT_VERSIONS)
            .where(
                PUBLISHED_FACT_VERSIONS.c.dataset_id == "stock.daily",
                PUBLISHED_FACT_VERSIONS.c.business_key == business_key,
                PUBLISHED_FACT_VERSIONS.c.revision == 2,
            )
            .values(is_deleted=True, market="cn")
        )

    with pytest.raises(SnapshotError, match="changed across published revisions"):
        store.create_snapshot(
            "stock.daily",
            publication_id=latest.publication_id,
            market="CN",
            symbol="600000",
        )


def test_tampered_snapshot_pair_fails_closed_without_a_cursor(engine: Engine) -> None:
    """A valid alternate pair cannot bypass selection-hash validation on first-page reads."""
    store = _store(engine)
    day = date(2026, 10, 27)
    publication = _publish(
        store,
        "stock-snapshot-pair-tamper",
        [
            _stock_record("CN", "600000", day, 10.1),
            _stock_record("cn", "600000", day, 90.0),
        ],
    )
    snapshot = store.create_snapshot(
        "stock.daily",
        publication_id=publication.publication_id,
        market="CN",
        symbol="600000",
    )
    with engine.begin() as connection:
        connection.execute(
            update(SNAPSHOTS)
            .where(SNAPSHOTS.c.snapshot_id == snapshot.snapshot_id)
            .values(market="cn", symbol="600000")
        )

    with pytest.raises(SnapshotError, match="does not match its stored hash"):
        store.get_snapshot(snapshot.snapshot_id)
    with pytest.raises(SnapshotError, match="does not match its stored hash"):
        store.read_page(snapshot.snapshot_id)


def test_sina_capture_hash_and_cursor_version_remain_compatible(engine: Engine) -> None:
    """The market selector extension leaves the existing Sina selection v2 intact."""
    store = _store(engine)
    instrument_id = "600000.SH"
    capture_id = "b" * 64
    records = [
        PublishedRecordInput(
            business_key=f"sina:key:{index}",
            payload={"instrument_id": instrument_id, "capture_id": capture_id, "value": index},
            business_date=date(2026, 10, 27),
            instrument_id=instrument_id,
            capture_id=capture_id,
        )
        for index in range(2)
    ]
    publication = _publish(store, "sina-capture-pair", records)
    snapshot = store.create_snapshot(
        "stock.daily",
        publication_id=publication.publication_id,
        page_size=1,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )
    expected_selection = {
        "dataset_id": "stock.daily",
        "source_id": publication.source_id,
        "publication_id": publication.publication_id,
        "publication_sequence": publication.sequence,
        "schema_version": publication.schema_version,
        "semantic_version": publication.semantic_version,
        "as_of": publication.published_at.isoformat(),
        "start_date": None,
        "end_date": None,
        "access_scope": "default",
        "page_size": 1,
        "sort": "business_date_asc_nulls_last_business_key_asc",
        "selection_version": 2,
        "instrument_id": instrument_id,
        "capture_id": capture_id,
    }
    assert (
        snapshot.selection_sha256
        == hashlib.sha256(_canonical_json(expected_selection).encode("utf-8")).hexdigest()
    )
    page = store.read_page(snapshot.snapshot_id)
    assert page.next_cursor is not None
    cursor_data = json.loads(_base64url_decode(page.next_cursor.split(".")[0]))
    assert cursor_data["version"] == 2
    assert cursor_data["instrument_id"] == instrument_id
    assert cursor_data["capture_id"] == capture_id
