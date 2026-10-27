"""Adversarial tests for verified visible publication sets."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, func, insert, select, update
from sqlalchemy.event import listen
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

from opendata.pipeline.publication_store import (
    MAX_METADATA_BYTES,
    MAX_RECORD_BYTES,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    SNAPSHOTS,
    CoverageObservationLimitError,
    CoverageRecordIntegrityError,
    CoverageSnapshotConsistencyError,
    PublicationStore,
    PublishedRecordInput,
)

CURSOR_KEY = b"coverage-integrity-test-key-with-32-bytes"
CONTRACT_SHA256 = "a" * 64
DAY = date(2026, 10, 26)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Engine, PublicationStore]]:
    """Create a fresh, file-backed SQLite store owned by this test."""
    engine = create_engine(f"sqlite:///{tmp_path / 'publication-coverage.sqlite'}")
    PUBLICATION_METADATA.create_all(engine)
    try:
        yield engine, PublicationStore(engine, cursor_signing_key=CURSOR_KEY)
    finally:
        engine.dispose()


def publish_rows(
    store: PublicationStore,
    idempotency_key: str,
    rows: list[PublishedRecordInput],
    *,
    schema_version: str = "2",
    provenance: dict[str, object] | None = None,
):
    """Publish a contract-bound row delta for these isolated tests."""
    return store.publish(
        dataset_id="equity.daily",
        source_id="provider.alpha",
        schema_version=schema_version,
        semantic_version="2.1",
        unit="CNY/share",
        adjustment_basis="unadjusted",
        quality={"status": "passed", "rejected": 0},
        provenance=(
            provenance
            if provenance is not None
            else {"provider": "alpha", "contract_sha256": CONTRACT_SHA256}
        ),
        idempotency_key=idempotency_key,
        records=rows,
    )


def stock_row(symbol: str, close: float, *, business_date: date | None = DAY):
    """Build a stock record with a stable business key and native unit."""
    suffix = business_date.isoformat() if business_date is not None else "undated"
    return PublishedRecordInput(
        business_key=f"{symbol}:{suffix}",
        business_date=business_date,
        payload={"symbol": symbol, "close": close, "unit": "CNY/share"},
    )


def test_visible_set_digest_is_independently_reproducible_and_snapshot_is_separate(
    database: tuple[Engine, PublicationStore],
) -> None:
    """The digest binds a sorted full set while a fixed snapshot stays independent."""
    engine, store = database
    publication = publish_rows(
        store,
        "coverage-canonical-order",
        [
            stock_row("证券:乙", 8.1, business_date=DAY),
            stock_row("证券:甲", 12.4, business_date=DAY),
            stock_row("undated", 4.2, business_date=None),
        ],
    )
    snapshot = store.create_snapshot(
        "equity.daily", publication_id=publication.publication_id, page_size=2
    )
    rows = store.read_coverage_record_set(
        "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
    )

    assert rows is not None
    assert [row.business_key for row in rows.records] == [
        "证券:乙:2026-10-26",
        "证券:甲:2026-10-26",
        "undated:undated",
    ]
    header = {
        "adjustment_basis": "unadjusted",
        "algorithm": "visible-record-set-v1",
        "contract_sha256": CONTRACT_SHA256,
        "dataset_id": "equity.daily",
        "publication_id": publication.publication_id,
        "publication_sequence": publication.sequence,
        "record_count": 3,
        "row_hash_algorithm": "normalize-records-canonical-v1",
        "schema_version": "2",
        "semantic_version": "2.1",
        "sort_id": "business-date-null-last-date-key:utf8-lex-v1",
        "source_id": "provider.alpha",
        "unit": "CNY/share",
    }
    ordered_rows = sorted(
        rows.records,
        key=lambda row: (
            row.business_date is None,
            row.business_date or date.min,
            row.business_key.encode("utf-8"),
        ),
    )
    canonical_lines = [
        json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ]
    canonical_lines.extend(
        json.dumps(
            {
                "business_date": row.business_date.isoformat() if row.business_date else None,
                "business_key": row.business_key,
                "publication_id": row.publication_id,
                "publication_sequence": row.publication_sequence,
                "record_hash": row.record_hash,
                "revision": row.revision,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in ordered_rows
    )
    expected = hashlib.sha256(("\n".join(canonical_lines) + "\n").encode("utf-8")).hexdigest()

    assert rows.sha256 == expected
    assert rows.record_count == 3
    assert store.read_page(snapshot.snapshot_id).snapshot == snapshot
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 1


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("quality_json", "x" * (MAX_METADATA_BYTES + 1), "metadata bound"),
        (
            "provenance_json",
            json.dumps({"provider": "中" * 90_000}, ensure_ascii=False, separators=(",", ":")),
            "metadata byte bound",
        ),
        ("provenance_json", "[]", "not an object"),
        (
            "provenance_json",
            '{"contract_sha256":"' + CONTRACT_SHA256 + '", "provider":"alpha"}',
            "canonically encoded",
        ),
        ("provenance_json", "{malformed", "malformed"),
    ],
)
def test_coverage_rejects_corrupted_publication_metadata(
    database: tuple[Engine, PublicationStore], column: str, value: str, message: str
) -> None:
    """Malformed, oversized, and noncanonical metadata cannot establish a pin."""
    engine, store = database
    publication = publish_rows(
        store,
        f"metadata-{message.replace(' ', '-')}",
        [stock_row("600000.SH", 12.4)],
    )
    with engine.begin() as connection:
        connection.execute(
            update(PUBLICATIONS)
            .where(PUBLICATIONS.c.publication_id == publication.publication_id)
            .values({column: value})
        )

    with pytest.raises(CoverageRecordIntegrityError, match=message):
        store.read_coverage_record_set(
            "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
        )


def test_legacy_schema_without_an_independent_contract_pin_fails_closed(
    database: tuple[Engine, PublicationStore],
) -> None:
    """A schema-1 publication without contract provenance cannot satisfy schema-2 proof."""
    _, store = database
    publication = publish_rows(
        store,
        "legacy-schema-without-pin",
        [stock_row("600000.SH", 12.4)],
        schema_version="1",
        provenance={"provider": "alpha"},
    )

    with pytest.raises(CoverageRecordIntegrityError, match="contract hash"):
        store.read_coverage_record_set(
            "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
        )


@pytest.mark.parametrize(
    "tamper",
    [
        "hash",
        "noncanonical-json",
        "malformed-json",
        "orphan-publication",
        "oversized-json",
        "oversized-selector",
        "invalid-timestamp",
    ],
)
def test_coverage_rejects_tampered_visible_fact_rows(
    database: tuple[Engine, PublicationStore], tamper: str
) -> None:
    """A persisted row mutation is detected before it enters the verified visible set."""
    engine, store = database
    publication = publish_rows(store, f"fact-{tamper}", [stock_row("600000.SH", 12.4)])
    key = "600000.SH:2026-10-26"
    values = {
        "hash": {"record_hash": "0" * 64},
        "noncanonical-json": {
            "record_json": '{ "close": 12.4, "symbol": "600000.SH", "unit": "CNY/share" }'
        },
        "malformed-json": {"record_json": "[]"},
        "orphan-publication": {"publication_id": "f" * 32},
        "oversized-json": {"record_json": "x" * (MAX_RECORD_BYTES + 1)},
        "oversized-selector": {"instrument_id": "i" * 129, "capture_id": "c" * 64},
        "invalid-timestamp": {"observed_at": "not-a-timestamp"},
    }[tamper]
    with engine.begin() as connection:
        connection.execute(
            update(PUBLISHED_FACT_VERSIONS)
            .where(
                PUBLISHED_FACT_VERSIONS.c.dataset_id == "equity.daily",
                PUBLISHED_FACT_VERSIONS.c.business_key == key,
                PUBLISHED_FACT_VERSIONS.c.revision == 1,
            )
            .values(values)
        )

    expected_message = {
        "hash": "record_hash does not match",
        "noncanonical-json": "canonical publication form",
        "malformed-json": "cannot be normalized",
        "orphan-publication": "stored fact publication provenance exceeds its bound",
        "oversized-json": "stored record_json exceeds",
        "oversized-selector": "stored instrument_id exceeds",
        "invalid-timestamp": "cannot be normalized",
    }[tamper]
    with pytest.raises(CoverageRecordIntegrityError, match=expected_message):
        store.read_coverage_record_set(
            "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
        )


def test_fact_provenance_is_checked_against_the_row_origin_contract(
    database: tuple[Engine, PublicationStore],
) -> None:
    """A retained row cannot inherit trust from a later publication's metadata."""
    engine, store = database
    first = publish_rows(store, "origin-contract-first", [stock_row("600000.SH", 12.4)])
    latest = publish_rows(store, "origin-contract-latest", [stock_row("600001.SH", 8.1)])
    with engine.begin() as connection:
        connection.execute(
            update(PUBLICATIONS)
            .where(PUBLICATIONS.c.publication_id == first.publication_id)
            .values(
                provenance_json=json.dumps(
                    {"provider": "alpha", "contract_sha256": "b" * 64},
                    separators=(",", ":"),
                )
            )
        )

    with pytest.raises(CoverageRecordIntegrityError, match="fact publication contract hash"):
        store.read_coverage_record_set(
            "equity.daily", latest.publication_id, contract_sha256=CONTRACT_SHA256
        )


@pytest.mark.parametrize("tamper", ["malformed", "noncanonical", "oversized"])
def test_originating_publication_provenance_is_bounded_and_canonical(
    database: tuple[Engine, PublicationStore], tamper: str
) -> None:
    """A retained fact's source metadata is verified after a later publication."""
    engine, store = database
    origin = publish_rows(store, f"origin-provenance-{tamper}", [stock_row("600000.SH", 12.4)])
    latest = publish_rows(store, f"origin-provenance-pin-{tamper}", [stock_row("600001.SH", 8.1)])
    provenance = {
        "malformed": "{invalid-json",
        "noncanonical": ('{ "contract_sha256": "' + CONTRACT_SHA256 + '", "provider": "alpha" }'),
        "oversized": "x" * (MAX_METADATA_BYTES + 1),
    }[tamper]
    with engine.begin() as connection:
        connection.execute(
            update(PUBLICATIONS)
            .where(PUBLICATIONS.c.publication_id == origin.publication_id)
            .values(provenance_json=provenance)
        )

    expected_message = {
        "malformed": "fact publication provenance is malformed",
        "noncanonical": "fact publication provenance is not canonically encoded",
        "oversized": "fact publication provenance exceeds its bound",
    }[tamper]
    with pytest.raises(CoverageRecordIntegrityError, match=expected_message):
        store.read_coverage_record_set(
            "equity.daily", latest.publication_id, contract_sha256=CONTRACT_SHA256
        )


def test_capture_scoped_snapshot_requires_explicit_matching_row_metadata(
    database: tuple[Engine, PublicationStore],
) -> None:
    """Capture selection is indexed only from explicit, payload-matching selectors."""
    _, store = database
    capture_id = "c" * 64
    instrument_id = "600000.SH"
    publication = publish_rows(
        store,
        "capture-scoped-snapshot",
        [
            PublishedRecordInput(
                business_key="capture:600000:2026-10-26",
                business_date=DAY,
                instrument_id=instrument_id,
                capture_id=capture_id,
                payload={
                    "instrument_id": instrument_id,
                    "capture_id": capture_id,
                    "close": 12.4,
                },
            )
        ],
    )

    snapshot = store.create_snapshot(
        "equity.daily",
        publication_id=publication.publication_id,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )
    page = store.read_page(snapshot.snapshot_id)

    assert len(page.records) == 1
    assert page.records[0].payload["capture_id"] == capture_id
    assert page.records[0].payload["instrument_id"] == instrument_id


def test_coverage_limit_never_returns_a_truncated_verified_set(
    database: tuple[Engine, PublicationStore],
) -> None:
    """The row limit rejects the entire scan and leaves fixed snapshots untouched."""
    engine, store = database
    publication = publish_rows(
        store,
        "verified-set-over-limit",
        [stock_row("600000.SH", 12.4), stock_row("600001.SH", 8.1)],
    )
    with pytest.raises(CoverageObservationLimitError, match="exceeds the 1-record limit"):
        store.read_coverage_record_set(
            "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256, limit=1
        )
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 0


def test_database_failure_during_fact_scan_is_reported_as_snapshot_inconsistency(
    database: tuple[Engine, PublicationStore],
) -> None:
    """A mid-scan SQL failure cannot be mistaken for an empty or partial visible set."""
    engine, store = database
    publication = publish_rows(store, "fact-scan-database-error", [stock_row("600000.SH", 12.4)])

    def fail_visible_fact_query(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        """Inject a database-layer failure only at the visible-fact read boundary."""
        del connection, cursor, parameters, context, executemany
        if "od_published_fact_versions" in statement:
            raise OperationalError(statement, {}, RuntimeError("temporary read failure"))

    listen(engine, "before_cursor_execute", fail_visible_fact_query)
    with pytest.raises(CoverageSnapshotConsistencyError, match="stable database snapshot"):
        store.read_coverage_record_set(
            "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
        )
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 0


def test_tombstone_pin_and_fixed_snapshot_keep_separate_visibility_boundaries(
    database: tuple[Engine, PublicationStore],
) -> None:
    """An old snapshot stays fixed while the newer coverage pin omits its tombstone."""
    engine, store = database
    first = publish_rows(
        store,
        "snapshot-before-tombstone",
        [stock_row("600000.SH", 12.4), stock_row("600001.SH", 8.1)],
    )
    snapshot = store.create_snapshot("equity.daily", publication_id=first.publication_id)
    deleted = publish_rows(store, "snapshot-after-tombstone", [])
    with engine.begin() as connection:
        connection.execute(
            insert(PUBLISHED_FACT_VERSIONS).values(
                dataset_id="equity.daily",
                business_key="600000.SH:2026-10-26",
                revision=2,
                publication_id=deleted.publication_id,
                publication_sequence=deleted.sequence,
                instrument_id=None,
                capture_id=None,
                business_date=None,
                event_at=None,
                observed_at=None,
                source_published_at=None,
                available_at=None,
                record_json="{}",
                record_hash="0" * 64,
                is_deleted=True,
            )
        )

    old_set = store.read_coverage_record_set(
        "equity.daily", first.publication_id, contract_sha256=CONTRACT_SHA256
    )
    new_set = store.read_coverage_record_set(
        "equity.daily", deleted.publication_id, contract_sha256=CONTRACT_SHA256
    )
    old_page = store.read_page(snapshot.snapshot_id)

    assert old_set is not None and old_set.record_count == 2
    assert new_set is not None and new_set.record_count == 1
    assert [row.business_key for row in new_set.records] == ["600001.SH:2026-10-26"]
    assert {row.business_key for row in old_page.records} == {
        "600000.SH:2026-10-26",
        "600001.SH:2026-10-26",
    }
    assert (
        store.read_coverage_record_set(
            "equity.daily", snapshot.snapshot_id, contract_sha256=CONTRACT_SHA256
        )
        is None
    )
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 1


def test_sqlite_dirty_read_setting_rejects_coverage_before_returning_data(tmp_path: Path) -> None:
    """Coverage refuses SQLite connections configured to observe uncommitted writes."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dirty-read.sqlite'}",
        poolclass=NullPool,
    )

    def enable_dirty_reads(dbapi_connection: object, _: object) -> None:
        """Set the isolated database option that the coverage reader must reject."""
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA read_uncommitted=1")
        cursor.close()

    listen(engine, "connect", enable_dirty_reads)
    PUBLICATION_METADATA.create_all(engine)
    store = PublicationStore(engine, cursor_signing_key=CURSOR_KEY)
    publication = publish_rows(store, "dirty-read-coverage", [stock_row("600000.SH", 12.4)])
    try:
        with pytest.raises(CoverageSnapshotConsistencyError, match="read_uncommitted"):
            store.read_coverage_record_set(
                "equity.daily", publication.publication_id, contract_sha256=CONTRACT_SHA256
            )
    finally:
        engine.dispose()
