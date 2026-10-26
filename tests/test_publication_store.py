"""Behavioral tests for immutable publications and scoped fixed snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.engine.result import MappingResult
from sqlalchemy.schema import CreateTable

from opendata.pipeline import publication_store
from opendata.pipeline.publication_store import (
    MAX_PAGE_BYTES,
    MAX_SNAPSHOT_TTL,
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    SNAPSHOTS,
    PageSizeLimitError,
    PublicationConflictError,
    PublicationStore,
    PublishedRecordInput,
    SnapshotError,
    SnapshotExpiredError,
    SnapshotSizeLimitError,
    publish_in_connection,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

CURSOR_KEY = b"test-only-cursor-signing-key-32-bytes-min"
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create a migrated isolated SQLite publication database."""
    db_path = tmp_path / "publication.sqlite"
    created = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    PUBLICATION_METADATA.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def make_store(database: Engine) -> PublicationStore:
    """Bind the test engine to a stable non-production cursor key."""
    return PublicationStore(database, cursor_signing_key=CURSOR_KEY)


def publish(
    store: PublicationStore,
    key: str,
    records: list[PublishedRecordInput],
    *,
    watermark: dict[str, object] | None = None,
):
    """Publish a standard typed stock-like record delta."""
    return store.publish(
        dataset_id="equity.daily",
        source_id="provider.alpha",
        schema_version="2",
        semantic_version="2.1",
        unit="CNY/share; shares; CNY",
        adjustment_basis="unadjusted",
        quality={"status": "passed", "rejected": 0},
        provenance={"provider": "alpha", "response_hash": "a" * 64},
        idempotency_key=key,
        records=records,
        watermark=watermark,
    )


def record(
    symbol: str,
    day: date,
    close: float,
    *,
    observed_at: datetime = NOW,
    event_at: datetime | None = None,
) -> PublishedRecordInput:
    """Build one record with deliberately separate business and observed time."""
    return PublishedRecordInput(
        business_key=f"{symbol}:{day.isoformat()}",
        business_date=day,
        observed_at=observed_at,
        source_published_at=observed_at - timedelta(minutes=1),
        available_at=observed_at + timedelta(milliseconds=500),
        event_at=event_at,
        payload={"symbol": symbol, "close": close, "unit": "CNY/share"},
    )


def test_publish_retry_is_idempotent_and_changed_payload_conflicts(engine: Engine) -> None:
    """An exact retry returns one publication; changed input cannot reuse its key."""
    store = make_store(engine)
    rows = [record("600000.SH", date(2026, 10, 23), 12.4)]
    first = publish(store, "daily:2026-10-26", rows, watermark={"date": "2026-10-23"})
    retry = publish(store, "daily:2026-10-26", rows, watermark={"date": "2026-10-23"})

    assert first.publication_id == retry.publication_id
    assert first.content_sha256 == retry.payload_hash
    assert len(store.list_publications("equity.daily")) == 1
    with pytest.raises(PublicationConflictError, match="different publication payload"):
        publish(
            store,
            "daily:2026-10-26",
            [record("600000.SH", date(2026, 10, 23), 12.5)],
            watermark={"date": "2026-10-23"},
        )


def test_delta_publication_preserves_revisions_and_unchanged_business_keys(engine: Engine) -> None:
    """Corrections append a revision while omitted and unchanged keys remain visible."""
    store = make_store(engine)
    day = date(2026, 10, 23)
    first = publish(
        store, "batch-1", [record("600000.SH", day, 12.4), record("600001.SH", day, 8.1)]
    )
    unchanged = publish(store, "batch-2", [record("600001.SH", day, 8.1)])
    corrected = publish(store, "batch-3", [record("600000.SH", day, 12.6)])

    page = store.read_page(store.create_snapshot("equity.daily", page_size=10).snapshot_id)
    keyed = {item.business_key: item for item in page.records}
    assert first.sequence == 1
    assert unchanged.sequence == 2
    assert corrected.sequence == 3
    assert keyed["600000.SH:2026-10-23"].revision == 2
    assert keyed["600000.SH:2026-10-23"].payload["close"] == 12.6
    assert keyed["600001.SH:2026-10-23"].revision == 1
    with engine.connect() as connection:
        revisions = connection.execute(
            select(func.count())
            .select_from(PUBLISHED_FACT_VERSIONS)
            .where(PUBLISHED_FACT_VERSIONS.c.business_key == "600000.SH:2026-10-23")
        ).scalar_one()
    assert revisions == 2


def test_fixed_snapshot_excludes_later_publications_and_keeps_page_order(engine: Engine) -> None:
    """A cursor continues the frozen publication even after a newer revision exists."""
    store = make_store(engine)
    day_a = date(2026, 10, 21)
    day_b = date(2026, 10, 22)
    first = publish(
        store,
        "initial",
        [
            record("600002.SH", day_b, 5.1),
            record("600001.SH", day_a, 7.2),
            record("600000.SH", day_a, 12.1),
        ],
    )
    snapshot = store.create_snapshot(
        "equity.daily", publication_id=first.publication_id, page_size=2
    )
    page_one = store.read_page(snapshot.snapshot_id)
    assert [item.business_key for item in page_one.records] == [
        "600000.SH:2026-10-21",
        "600001.SH:2026-10-21",
    ]
    assert page_one.next_cursor is not None

    publish(store, "correction", [record("600002.SH", day_b, 99.0)])
    page_two = store.read_page(snapshot.snapshot_id, cursor=page_one.next_cursor)
    assert [item.business_key for item in page_two.records] == ["600002.SH:2026-10-22"]
    assert page_two.records[0].payload["close"] == 5.1
    assert page_two.records[0].publication_id == first.publication_id
    assert page_two.next_cursor is None


def test_window_is_half_open_and_content_hash_covers_scoped_subset(engine: Engine) -> None:
    """Snapshot hashes differ for a narrowed business-date window and access scope."""
    store = make_store(engine)
    publish(
        store,
        "range",
        [
            record("600000.SH", date(2026, 10, 20), 10.0),
            record("600000.SH", date(2026, 10, 21), 11.0),
            record("600000.SH", date(2026, 10, 22), 12.0),
        ],
    )
    all_dates = store.create_snapshot("equity.daily", page_size=1)
    one_day = store.create_snapshot(
        "equity.daily",
        start_date=date(2026, 10, 21),
        end_date=date(2026, 10, 22),
        page_size=1,
    )
    other_scope = store.create_snapshot("equity.daily", page_size=1, access_scope="research:team2")

    page = store.read_page(one_day.snapshot_id)
    assert [row.business_date for row in page.records] == [date(2026, 10, 21)]
    assert one_day.content_hash != all_dates.content_hash
    assert one_day.content_hash != other_scope.content_hash
    assert one_day.selection_hash != other_scope.selection_hash
    assert one_day.record_count == 1
    assert one_day.as_of == store.get_publication(one_day.publication_id).published_at


def test_cursor_rejects_tampering_other_snapshot_and_other_scope(engine: Engine) -> None:
    """A valid continuation token cannot move across its signed bindings."""
    store = make_store(engine)
    publish(
        store,
        "pages",
        [record(f"60000{i}.SH", date(2026, 10, 20), 10 + i) for i in range(3)],
    )
    snapshot = store.create_snapshot("equity.daily", page_size=1)
    page = store.read_page(snapshot.snapshot_id)
    assert page.next_cursor is not None
    with pytest.raises(SnapshotError, match="signature"):
        replacement = "A" if page.next_cursor[-1] != "A" else "B"
        store.read_page(snapshot.snapshot_id, cursor=page.next_cursor[:-1] + replacement)
    another = store.create_snapshot("equity.daily", page_size=1)
    with pytest.raises(SnapshotError, match="different snapshot"):
        store.read_page(another.snapshot_id, cursor=page.next_cursor)
    with pytest.raises(SnapshotError, match="requested scope"):
        store.read_page(
            snapshot.snapshot_id, cursor=page.next_cursor, access_scope="research:team2"
        )


def test_snapshot_expiry_is_persisted_and_reported(engine: Engine) -> None:
    """Expired snapshots fail closed and TTL has a hard upper bound."""
    store = make_store(engine)
    publish(store, "expiry", [record("600000.SH", date(2026, 10, 23), 12.4)])
    with pytest.raises(ValueError, match="seven days"):
        store.create_snapshot("equity.daily", ttl=MAX_SNAPSHOT_TTL + timedelta(seconds=1))
    snapshot = store.create_snapshot("equity.daily", ttl=timedelta(seconds=1))
    with engine.begin() as connection:
        connection.execute(
            SNAPSHOTS.update()
            .where(SNAPSHOTS.c.snapshot_id == snapshot.snapshot_id)
            .values(expires_at_us=1)
        )
    with pytest.raises(SnapshotExpiredError):
        store.get_snapshot(snapshot.snapshot_id)
    with pytest.raises(SnapshotExpiredError):
        store.read_page(snapshot.snapshot_id)


def test_publication_failure_rolls_back_facts_head_and_watermark(engine: Engine) -> None:
    """The connection-level primitive leaves all publication state to its owner."""
    with (
        pytest.raises(RuntimeError, match="abort enclosing transaction"),
        engine.begin() as connection,
    ):
        result = publish_in_connection(
            connection,
            dataset_id="equity.daily",
            source_id="provider.alpha",
            schema_version="2",
            semantic_version="2.1",
            unit="CNY/share",
            adjustment_basis="unadjusted",
            quality={"status": "passed"},
            provenance={"provider": "alpha"},
            idempotency_key="will-rollback",
            records=[record("600000.SH", date(2026, 10, 23), 12.4)],
            watermark={"date": "2026-10-23"},
        )
        assert result.sequence == 1
        raise RuntimeError("abort enclosing transaction")

    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(PUBLICATIONS)).scalar_one() == 0
        assert (
            connection.execute(
                select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(select(func.count()).select_from(PUBLICATION_HEADS)).scalar_one()
            == 0
        )


def test_two_engine_concurrent_publications_advance_unique_dataset_sequences(
    tmp_path: Path,
) -> None:
    """Distinct engines serialize SQLite publication heads without duplicate sequence."""
    db_path = tmp_path / "concurrent.sqlite"
    first_engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    PUBLICATION_METADATA.create_all(first_engine)
    second_engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    barrier = Barrier(2)
    stores = (make_store(first_engine), make_store(second_engine))

    def write(index: int):
        """Wait for both engine instances, then publish an independent key."""
        barrier.wait(timeout=5)
        return publish(
            stores[index],
            f"concurrent-{index}",
            [record(f"60000{index}.SH", date(2026, 10, 23), 10 + index)],
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            publications = list(executor.map(write, range(2)))
        assert sorted(item.sequence for item in publications) == [1, 2]
        assert len(stores[0].list_publications("equity.daily")) == 2
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_list_apis_and_time_dimensions_are_explicit(engine: Engine) -> None:
    """Catalog reads expose platform publish time distinct from source observation."""
    store = make_store(engine)
    observed = datetime(2026, 10, 23, 15, 0, tzinfo=timezone(timedelta(hours=8)))
    event_at = datetime(2026, 10, 23, 14, 58, tzinfo=timezone(timedelta(hours=8)))
    publication = publish(
        store,
        "catalog",
        [
            record(
                "600000.SH",
                date(2026, 10, 23),
                12.4,
                observed_at=observed,
                event_at=event_at,
            )
        ],
    )
    datasets = store.list_datasets()
    publications = store.list_publications("equity.daily")
    snapshot = store.create_snapshot("equity.daily")
    row = store.read_page(snapshot.snapshot_id).records[0]

    assert datasets[0].publication_id == publication.publication_id
    assert publications[0].published_at == publication.published_at
    assert store.get_publication(publication.publication_id) == publication
    assert store.get_snapshot(snapshot.snapshot_id) == snapshot
    assert row.business_date == date(2026, 10, 23)
    assert row.event_at == event_at.astimezone(timezone.utc)
    assert row.observed_at == observed.astimezone(timezone.utc)
    assert snapshot.as_of == publication.published_at
    assert snapshot.as_of != row.observed_at


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start_date": datetime(2026, 10, 20, tzinfo=timezone.utc)}, "datetime.date"),
        ({"start_date": date(2026, 10, 21), "end_date": date(2026, 10, 21)}, "nonempty"),
        ({"page_size": True}, "integer"),
        ({"access_scope": "bad\nscope"}, "control"),
    ],
)
def test_snapshot_contract_rejects_invalid_window_and_page_inputs(
    engine: Engine, kwargs: dict[str, object], message: str
) -> None:
    """Strict contracts avoid timestamp/date and bool/integer ambiguities."""
    store = make_store(engine)
    publish(store, "validation", [record("600000.SH", date(2026, 10, 23), 12.4)])
    with pytest.raises(ValueError, match=message):
        store.create_snapshot("equity.daily", **kwargs)


def test_publish_rejects_duplicate_keys_naive_timestamps_and_secret_fields(engine: Engine) -> None:
    """Bad batches fail before becoming visible in a partial publication."""
    store = make_store(engine)
    good = record("600000.SH", date(2026, 10, 23), 12.4)
    with pytest.raises(ValueError, match="duplicate business_key"):
        publish(store, "dupes", [good, good])
    with pytest.raises(ValueError, match="timezone-aware"):
        publish(
            store,
            "naive-time",
            [
                PublishedRecordInput(
                    "600000.SH:2026-10-23", {"close": 12.4}, observed_at=datetime(2026, 10, 23)
                )
            ],
        )
    with pytest.raises(ValueError, match="credential-like field"):
        publish(
            store,
            "secret-field",
            [PublishedRecordInput("600000.SH:2026-10-23", {"api_key": "not-for-storage"})],
        )
    assert store.list_publications("equity.daily") == ()


def test_short_hmac_key_is_rejected() -> None:
    """Cursor signing always requires caller-provided secret material."""
    engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="at least 32 bytes"):
            PublicationStore(engine, cursor_signing_key=b"short")
    finally:
        engine.dispose()


def test_empty_batches_and_read_only_discovery_contracts(engine: Engine) -> None:
    """Empty delta publication advances metadata but publishes no fact rows."""
    store = make_store(engine)
    publication = publish(store, "empty", [])
    snapshot = store.create_snapshot("equity.daily")

    assert publication.record_count == 0
    assert snapshot.record_count == 0
    assert store.read_page(snapshot.snapshot_id).records == ()
    assert store.list_datasets()[0].dataset_id == "equity.daily"
    assert store.list_publications("equity.daily", before_sequence=1) == ()
    assert store.get_publication("0" * 32) is None
    assert store.get_snapshot("0" * 32) is None


def test_read_page_streams_and_rejects_over_budget_without_a_partial_page(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large pages stop at the response budget and release their streaming connection."""
    store = make_store(engine)
    payload = {"blob": "x" * (70 * 1024)}
    rows = [
        PublishedRecordInput(
            business_key=f"large:{index:04d}",
            business_date=date(2026, 10, 1),
            payload=payload,
        )
        for index in range(140)
    ]
    publication = publish(store, "large-page", rows)
    snapshot = store.create_snapshot(
        "equity.daily", publication_id=publication.publication_id, page_size=1_000
    )
    assert snapshot.record_count == len(rows)
    assert MAX_PAGE_BYTES == 8 * 1024 * 1024

    consumed = 0
    original_iter = MappingResult.__iter__

    def tracked_iter(result: MappingResult):
        nonlocal consumed
        for row in original_iter(result):
            consumed += 1
            yield row

    def reject_all(_result: MappingResult):
        raise AssertionError("read_page must stream rows rather than materialize result.all()")

    monkeypatch.setattr(MappingResult, "__iter__", tracked_iter)
    monkeypatch.setattr(MappingResult, "all", reject_all)
    pool_events = {"checkout": 0, "checkin": 0}

    def on_checkout(*_args: object) -> None:
        pool_events["checkout"] += 1

    def on_checkin(*_args: object) -> None:
        pool_events["checkin"] += 1

    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", on_checkin)
    try:
        with pytest.raises(PageSizeLimitError, match="8,388,608-byte response limit"):
            store.read_page(snapshot.snapshot_id)
        assert 0 < consumed < len(rows)
        assert pool_events["checkout"] == pool_events["checkin"]
    finally:
        event.remove(engine, "checkout", on_checkout)
        event.remove(engine, "checkin", on_checkin)


def test_snapshot_hash_has_a_visible_row_limit_and_rolls_back_snapshot(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot hashing refuses a scope beyond its explicit work bound."""
    store = make_store(engine)
    publish(
        store,
        "snapshot-too-large",
        [record(f"60000{index}.SH", date(2026, 10, 23), 10.0 + index) for index in range(3)],
    )
    monkeypatch.setattr(publication_store, "MAX_SNAPSHOT_RECORDS", 2)

    with pytest.raises(SnapshotSizeLimitError, match="2-record limit"):
        store.create_snapshot("equity.daily")
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 0


def test_null_business_dates_page_with_nulls_last_cursor(engine: Engine) -> None:
    """Undated observations stay distinct and can be paged without inventing dates."""
    store = make_store(engine)
    publish(
        store,
        "undated",
        [
            PublishedRecordInput("source-row:b", {"value": 2}),
            PublishedRecordInput("source-row:a", {"value": 1}),
        ],
    )
    snapshot = store.create_snapshot("equity.daily", page_size=1)
    page_one = store.read_page(snapshot.snapshot_id)
    assert page_one.records[0].business_key == "source-row:a"
    assert page_one.records[0].business_date is None
    assert page_one.records[0].observed_at is None
    assert page_one.next_cursor is not None
    page_two = store.read_page(snapshot.snapshot_id, cursor=page_one.next_cursor)
    assert page_two.records[0].business_key == "source-row:b"


def test_store_rejects_malformed_metadata_and_unknown_snapshot_inputs(engine: Engine) -> None:
    """Repository methods validate public identifiers and reject absent versions."""
    store = make_store(engine)
    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        PublicationStore(object(), cursor_signing_key=CURSOR_KEY)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be bytes"):
        PublicationStore(engine, cursor_signing_key="x" * 32)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="nonempty string"):
        store.list_publications(" ")
    with pytest.raises(ValueError, match="before_sequence"):
        store.list_publications("equity.daily", before_sequence=True)
    with pytest.raises(ValueError, match="lowercase 32-character"):
        store.get_publication("not-a-uuid")
    with pytest.raises(ValueError, match="lowercase 32-character"):
        store.get_snapshot("not-a-uuid")
    with pytest.raises(SnapshotError, match="no matching published version"):
        store.create_snapshot("equity.daily")
    publish(store, "wrong-publication", [record("600000.SH", date(2026, 10, 23), 12.4)])
    with pytest.raises(SnapshotError, match="no matching published version"):
        store.create_snapshot("equity.daily", publication_id="0" * 32)
    with pytest.raises(SnapshotError, match="does not exist"):
        store.read_page("0" * 32)


def test_signed_cursor_rejects_invalid_payload_shape_date_and_key(engine: Engine) -> None:
    """The verifier rejects malformed signed JSON before constructing a query."""
    store = make_store(engine)
    publish(
        store,
        "cursor-shape",
        [record(f"60000{i}.SH", date(2026, 10, 20), 10 + i) for i in range(3)],
    )
    snapshot = store.create_snapshot("equity.daily", page_size=1)
    cursor = store.read_page(snapshot.snapshot_id).next_cursor
    assert cursor is not None

    def signed(payload: bytes) -> str:
        """Encode a deliberately malformed payload with a valid test signature."""
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(CURSOR_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"

    with pytest.raises(SnapshotError, match="malformed"):
        store.read_page(snapshot.snapshot_id, cursor="not-a-cursor")
    with pytest.raises(SnapshotError, match="payload is invalid"):
        store.read_page(snapshot.snapshot_id, cursor=signed(b"[]"))
    with pytest.raises(SnapshotError, match="payload is invalid"):
        store.read_page(snapshot.snapshot_id, cursor=signed(b'{"x":1,"x":2}'))

    encoded, _signature = cursor.split(".")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    payload["last_business_date"] = "not-a-date"
    with pytest.raises(SnapshotError, match="business date is invalid"):
        store.read_page(snapshot.snapshot_id, cursor=signed(json.dumps(payload).encode()))
    payload["last_business_date"] = None
    payload["last_business_key"] = ""
    with pytest.raises(SnapshotError, match="continuation key is invalid"):
        store.read_page(snapshot.snapshot_id, cursor=signed(json.dumps(payload).encode()))


def test_cursor_and_json_size_limits_and_non_json_values(engine: Engine) -> None:
    """Bounded contracts reject oversized signed tokens, cyclic and unsafe JSON."""
    store = make_store(engine)
    publish(store, "size-check", [record("600000.SH", date(2026, 10, 23), 12.4)])
    snapshot = store.create_snapshot("equity.daily")
    huge_scope = replace(snapshot, access_scope="x" * 10_000)
    with pytest.raises(SnapshotError, match="cursor exceeds its size limit"):
        store._encode_cursor(huge_scope, None, "key")
    with pytest.raises(SnapshotError, match="malformed or exceeds"):
        store.read_page(snapshot.snapshot_id, cursor="x" * 8_193)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        PublicationStore(engine, cursor_signing_key=b"x" * 31)
    with pytest.raises(TypeError, match="must be bytes"):
        PublicationStore(engine, cursor_signing_key=bytearray(CURSOR_KEY))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between one second and seven days"):
        store.create_snapshot("equity.daily", ttl=1)  # type: ignore[arg-type]

    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    with pytest.raises(ValueError, match="cyclic JSON object"):
        publish(
            store,
            "cycle",
            [PublishedRecordInput("cycle:key", cycle)],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="must be finite"):
        publish(store, "nan", [PublishedRecordInput("nan:key", {"value": float("nan")})])
    with pytest.raises(ValueError, match="secret-like value"):
        publish(store, "bearer", [PublishedRecordInput("bearer:key", {"value": "Bearer abc"})])
    with pytest.raises(ValueError, match="non-JSON value"):
        publish(store, "date-payload", [PublishedRecordInput("date:key", {"value": date.today()})])


def test_record_batch_validation_rejects_wrong_container_shape(engine: Engine) -> None:
    """The delta API fails before writing for malformed iterables and row objects."""
    store = make_store(engine)
    args = {
        "dataset_id": "equity.daily",
        "source_id": "provider.alpha",
        "schema_version": "2",
        "semantic_version": "2.1",
        "unit": "CNY/share",
        "adjustment_basis": "unadjusted",
        "quality": {"status": "passed"},
        "provenance": {"provider": "alpha"},
    }
    with engine.connect() as connection:
        with pytest.raises(ValueError, match="must be a JSON object"):
            invalid_quality_args = args | {"quality": ["invalid"]}
            publish_in_connection(
                connection,
                **invalid_quality_args,  # type: ignore[arg-type]
                idempotency_key="bad-quality",
                records=[],
            )
        with pytest.raises(TypeError, match="Connection"):
            publish_in_connection(object(), **args, idempotency_key="wrong-connection", records=[])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="iterable of PublishedRecordInput"):
            publish_in_connection(
                connection, **args, idempotency_key="wrong-container", records={"row": {}}
            )
        with pytest.raises(ValueError, match="must be iterable"):
            publish_in_connection(
                connection,
                **args,
                idempotency_key="not-iterable",
                records=None,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="must be PublishedRecordInput"):
            publish_in_connection(
                connection,
                **args,
                idempotency_key="wrong-row",
                records=[object()],  # type: ignore[list-item]
            )
        with pytest.raises(ValueError, match="must be datetime.date"):
            publish_in_connection(
                connection,
                **args,
                idempotency_key="datetime-date",
                records=[
                    PublishedRecordInput(
                        "bad-date", {}, business_date=datetime(2026, 10, 23, tzinfo=timezone.utc)
                    )
                ],
            )
    assert store.list_publications("equity.daily") == ()


def test_business_key_collation_is_case_sensitive_and_rejects_trailing_spaces(
    engine: Engine,
) -> None:
    """Storage keys preserve exact provider IDs and cursor sort semantics on MySQL 8."""
    store = make_store(engine)
    publish(
        store,
        "case-sensitive-keys",
        [
            PublishedRecordInput("ABC", {"value": 1}),
            PublishedRecordInput("abc", {"value": 2}),
        ],
    )
    page = store.read_page(store.create_snapshot("equity.daily", page_size=10).snapshot_id)
    assert [item.business_key for item in page.records] == ["ABC", "abc"]
    with pytest.raises(ValueError, match="surrounding whitespace"):
        publish(store, "trailing-space", [PublishedRecordInput("ABC ", {"value": 3})])
    ddl = str(CreateTable(PUBLISHED_FACT_VERSIONS).compile(dialect=mysql_dialect()))
    assert "COLLATE utf8mb4_0900_bin" in ddl


def test_bounded_json_above_mysql_text_limit_uses_mediumtext(engine: Engine) -> None:
    """Documented JSON bounds remain writable for valid rows above 64KiB."""
    store = make_store(engine)
    value = "x" * (70 * 1024)
    publication = publish(
        store,
        "large-json-row",
        [PublishedRecordInput("large:key", {"source_payload": value})],
    )
    snapshot = store.create_snapshot("equity.daily")
    row = store.read_page(snapshot.snapshot_id).records[0]
    ddl = str(CreateTable(PUBLISHED_FACT_VERSIONS).compile(dialect=mysql_dialect()))

    assert publication.record_count == 1
    assert row.payload["source_payload"] == value
    assert "MEDIUMTEXT" in ddl
