"""Storage boundary tests for explicit instrument/capture publication scope."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, func, insert, select

from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
    SNAPSHOTS,
    PublicationConflictError,
    PublicationContractConflictError,
    PublicationStore,
    PublishedRecordInput,
    SnapshotError,
    SnapshotRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

CURSOR_KEY = b"test-only-cursor-signing-key-32-bytes-min"
DATASET_ID = "sina_fzb_report_observations"
SOURCE_ID = "sina-fzb"
SCHEMA_VERSION = "source-native-report-v1"
SEMANTIC_VERSION = "sina-fzb-source-native-v1"
UNIT = "per-field"
ADJUSTMENT_BASIS = "not_applicable"
QUALITY = {"status": "unknown", "report_id": None, "issue_count": 0}
PROVENANCE = {"fixture": "capture-scope-test"}
INSTRUMENT_A = "600000.SH"
INSTRUMENT_B = "000001.SZ"
CAPTURE_A = "a" * 64
CAPTURE_B = "b" * 64
GOLDEN_BUSINESS_KEY = (
    '["600000.SH","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"fzb","2024-12-31"]'
)
GOLDEN_PAYLOAD_JSON = (
    '{"capture_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"entries":[{"kind":"item","raw_value":"1"}],"instrument_id":"600000.SH",'
    '"report_period":"2024-12-31","statement_code":"fzb"}'
)

# Precomputed from the pinned pre-W11 canonical JSON contract and fixed inputs.
# These constants intentionally do not call publication_store's serializers.
LEGACY_RECORD_SHA256 = "d842a998d215adda335fe72fb83d3951cfc4d708ebfdaef79f1554001e15efe4"
LEGACY_PUBLICATION_SHA256 = "b180107a1de3d5753522f0ebefe157f9a2f122b0ab31c5a53faa775cb8009fed"
SELECTED_RECORD_SHA256 = "31129db3143b7758411b55a438432092d617254de230b7f56b6c1fa5272a892d"
SELECTED_PUBLICATION_SHA256 = "9fb6b4681620c584120989386e689bc994b948cc449cdebb35b017c6fb94110c"
LEGACY_SELECTION_SHA256 = "95bac682512839a0e303d1ded99e1c4df7c3beb7ff79caf99e1575ca72976964"
LEGACY_CONTENT_SHA256 = "9bd607890a120bc7f45d17b57f489c87a94fc382cf285914d9fd51dcea63abe0"
LEGACY_CURSOR_V1 = (
    "eyJhY2Nlc3Nfc2NvcGUiOiJkZWZhdWx0IiwiYXNfb2ZfdXMiOjE3OTMwMDE2MDAwMDAwMDAs"
    "ImRhdGFzZXRfaWQiOiJzaW5hX2Z6Yl9yZXBvcnRfb2JzZXJ2YXRpb25zIiwiZW5kX2RhdGUiOm51bGws"
    "ImV4cGlyZXNfYXRfdXMiOjQxMDI0NDQ4MDAwMDAwMDAsImxhc3RfYnVzaW5lc3NfZGF0ZSI6bnVsbCw"
    "ibGFzdF9idXNpbmVzc19rZXkiOiJbXCI2MDAwMDAuU0hcIixcImFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh"
    "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFcIixcImZ6YlwiLFwiMjAyNC0xMi"
    "0zMVwiXSIsInBhZ2Vfc2l6ZSI6MSwicHVibGljYXRpb25faWQiOiIxMTIyMzM0NDU1NjY3Nzg4OTkwMGFh"
    "YmJjY2RkZWVmZiIsInB1YmxpY2F0aW9uX3NlcXVlbmNlIjoxLCJzY2hlbWFfdmVyc2lvbiI6InNvdXJjZS1u"
    "YXRpdmUtcmVwb3J0LXYxIiwic2VsZWN0aW9uX3NoYTI1NiI6Ijk1YmFjNjgyNTEyODM5YTBlMzAzZDFkZWQ5"
    "OWUxYzRkZjdjM2JlYjdmZjc5Y2FmOTllMTU3NWNhNzI5NzY5NjQiLCJzbmFwc2hvdF9pZCI6ImZmZWVkZGNj"
    "YmJhYTAwOTk4ODc3NjY1NTQ0MzMyMjExIiwic29ydCI6ImJ1c2luZXNzX2RhdGVfYXNjX251bGxzX2xhc3Rf"
    "YnVzaW5lc3Nfa2V5X2FzYyIsInN0YXJ0X2RhdGUiOm51bGwsInZlcnNpb24iOjF9."
    "9l2JGYt0aHua1TYQgdapzr3rimv6KYsqHi2p6l6nh0k"
)
GOLDEN_PUBLICATION_ID = "11223344556677889900aabbccddeeff"
GOLDEN_SNAPSHOT_ID = "ffeeddccbbaa00998877665544332211"
GOLDEN_AS_OF = datetime(2026, 10, 26, 8, 0, 0, tzinfo=timezone.utc)
GOLDEN_AS_OF_US = 1_793_001_600_000_000


@pytest.fixture
def capture_engine(tmp_path: Path) -> Iterator[Engine]:
    """Create only runtime metadata in an owned SQLite file and dispose it."""
    engine = create_engine(f"sqlite:///{tmp_path / 'capture-scope.sqlite'}")
    try:
        PUBLICATION_METADATA.create_all(engine)
        yield engine
    finally:
        engine.dispose()


def _store(engine: Engine) -> PublicationStore:
    """Bind a store to the test-owned engine and stable test-only key."""
    return PublicationStore(engine, cursor_signing_key=CURSOR_KEY)


def _business_key(instrument_id: str, capture_id: str, period: str) -> str:
    """Return the fixture's canonical source capture identity."""
    return json.dumps(
        [instrument_id, capture_id, "fzb", period],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _record(
    instrument_id: str,
    capture_id: str,
    period: str,
    amount: str,
    *,
    indexed: bool = True,
    business_key: str | None = None,
) -> PublishedRecordInput:
    """Build one date-null FZB-like row with optional explicit storage scope."""
    key = business_key or _business_key(instrument_id, capture_id, period)
    payload = {
        "instrument_id": instrument_id,
        "capture_id": capture_id,
        "statement_code": "fzb",
        "report_period": period,
        "entries": [{"kind": "item", "raw_value": amount}],
    }
    if not indexed:
        # Preserve the old seven-position constructor and deliberately do not
        # infer indexed metadata from similarly named payload members.
        return PublishedRecordInput(key, payload, None, None, None, None, None)
    return PublishedRecordInput(
        business_key=key,
        payload=payload,
        business_date=None,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )


def _publish(
    store: PublicationStore,
    idempotency_key: str,
    records: list[PublishedRecordInput],
    *,
    dataset_id: str = DATASET_ID,
    watermark: dict[str, object] | None = None,
):
    """Publish one batch under stable fixture metadata."""
    return store.publish(
        dataset_id=dataset_id,
        source_id=SOURCE_ID,
        schema_version=SCHEMA_VERSION,
        semantic_version=SEMANTIC_VERSION,
        unit=UNIT,
        adjustment_basis=ADJUSTMENT_BASIS,
        quality=QUALITY,
        provenance=PROVENANCE,
        idempotency_key=idempotency_key,
        records=records,
        watermark=watermark,
    )


def _golden_record(*, indexed: bool) -> PublishedRecordInput:
    """Build the fixed record used by the v1/v2 canonical hash vectors."""
    return _record(INSTRUMENT_A, CAPTURE_A, "2024-12-31", "1", indexed=indexed)


def _seed_pre_w11_publication(engine: Engine) -> None:
    """Insert a deterministic legacy publication row for snapshot hash vectors."""
    with engine.begin() as connection:
        connection.execute(
            insert(PUBLICATIONS).values(
                publication_id=GOLDEN_PUBLICATION_ID,
                dataset_id=DATASET_ID,
                source_id=SOURCE_ID,
                sequence=1,
                schema_version=SCHEMA_VERSION,
                semantic_version=SEMANTIC_VERSION,
                unit=UNIT,
                adjustment_basis=ADJUSTMENT_BASIS,
                quality_json='{"issue_count":0,"report_id":null,"status":"unknown"}',
                provenance_json='{"fixture":"capture-scope-test"}',
                watermark_json=None,
                idempotency_key="pre-w11-golden",
                payload_hash=LEGACY_PUBLICATION_SHA256,
                record_count=1,
                published_at_us=GOLDEN_AS_OF_US,
            )
        )
        connection.execute(
            insert(PUBLICATION_HEADS).values(
                dataset_id=DATASET_ID,
                latest_sequence=1,
                latest_publication_id=GOLDEN_PUBLICATION_ID,
                watermark_json=None,
                updated_at_us=GOLDEN_AS_OF_US,
            )
        )
        connection.execute(
            insert(PUBLISHED_FACT_VERSIONS).values(
                dataset_id=DATASET_ID,
                business_key=GOLDEN_BUSINESS_KEY,
                revision=1,
                publication_id=GOLDEN_PUBLICATION_ID,
                publication_sequence=1,
                business_date=None,
                event_at=None,
                observed_at=None,
                source_published_at=None,
                available_at=None,
                record_json=GOLDEN_PAYLOAD_JSON,
                record_hash=LEGACY_RECORD_SHA256,
                is_deleted=False,
            )
        )


def _decode_cursor_payload(token: str) -> dict[str, object]:
    """Decode an already signed test cursor to inspect its wire version/binding."""
    encoded, _signature = token.split(".")
    padded = encoded + "=" * (-len(encoded) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _sign_cursor_payload(payload: dict[str, object]) -> str:
    """Sign a deliberately altered cursor with this fixture's known test key."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(CURSOR_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"{encoded}.{signature}"


def test_legacy_hash_vectors_and_payload_selector_names_do_not_infer_scope(
    capture_engine: Engine,
) -> None:
    """Legacy positional records keep their original record and publication hashes."""
    store = _store(capture_engine)
    publication = _publish(store, "golden-legacy", [_golden_record(indexed=False)])
    snapshot = store.create_snapshot(DATASET_ID, page_size=1)
    page = store.read_page(snapshot.snapshot_id)

    assert publication.payload_hash == LEGACY_PUBLICATION_SHA256
    assert page.records[0].record_hash == LEGACY_RECORD_SHA256
    assert page.records[0].payload["instrument_id"] == INSTRUMENT_A
    assert page.records[0].payload["capture_id"] == CAPTURE_A
    with capture_engine.connect() as connection:
        fact = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).where(
                    PUBLISHED_FACT_VERSIONS.c.business_key == GOLDEN_BUSINESS_KEY
                )
            )
            .mappings()
            .one()
        )
    assert fact["instrument_id"] is None
    assert fact["capture_id"] is None


def test_selected_pair_is_bound_into_record_and_publication_hashes(
    capture_engine: Engine,
) -> None:
    """Explicit paired metadata changes canonical hashes and populates only its own index."""
    store = _store(capture_engine)
    record = _golden_record(indexed=True)
    publication = _publish(store, "golden-selected", [record])
    retry = _publish(store, "golden-selected", [record])
    snapshot = store.create_snapshot(
        DATASET_ID,
        instrument_id=INSTRUMENT_A,
        capture_id=CAPTURE_A,
        page_size=1,
    )
    page = store.read_page(snapshot.snapshot_id)

    assert publication.publication_id == retry.publication_id
    assert publication.payload_hash == SELECTED_PUBLICATION_SHA256
    assert len(page.records) == 1
    assert page.records[0].record_hash == SELECTED_RECORD_SHA256
    assert snapshot.instrument_id == INSTRUMENT_A
    assert snapshot.capture_id == CAPTURE_A
    with capture_engine.connect() as connection:
        fact = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).where(
                    PUBLISHED_FACT_VERSIONS.c.business_key == GOLDEN_BUSINESS_KEY
                )
            )
            .mappings()
            .one()
        )
    assert (fact["instrument_id"], fact["capture_id"]) == (INSTRUMENT_A, CAPTURE_A)


def test_legacy_snapshot_hashes_and_cursor_v1_bytes_match_precomputed_vectors(
    capture_engine: Engine,
) -> None:
    """Migrated legacy rows retain their exact selection/content hashes and v1 cursor bytes."""
    _seed_pre_w11_publication(capture_engine)
    store = _store(capture_engine)

    snapshot = store.create_snapshot(DATASET_ID, page_size=1)
    page = store.read_page(snapshot.snapshot_id)
    assert snapshot.selection_hash == LEGACY_SELECTION_SHA256
    assert snapshot.content_hash == LEGACY_CONTENT_SHA256
    assert page.records[0].record_hash == LEGACY_RECORD_SHA256

    fixed_snapshot = SnapshotRecord(
        snapshot_id=GOLDEN_SNAPSHOT_ID,
        dataset_id=DATASET_ID,
        publication_id=GOLDEN_PUBLICATION_ID,
        schema_version=SCHEMA_VERSION,
        publication_sequence=1,
        start_date=None,
        end_date=None,
        page_size=1,
        access_scope="default",
        selection_sha256=LEGACY_SELECTION_SHA256,
        content_hash=LEGACY_CONTENT_SHA256,
        record_count=1,
        created_at=GOLDEN_AS_OF,
        expires_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
        as_of=GOLDEN_AS_OF,
    )
    cursor = store._encode_cursor(fixed_snapshot, None, GOLDEN_BUSINESS_KEY)

    assert cursor == LEGACY_CURSOR_V1
    assert store._decode_cursor(cursor, fixed_snapshot) == (None, GOLDEN_BUSINESS_KEY)


def test_capture_snapshot_selects_exact_pair_and_pages_date_null_rows(
    capture_engine: Engine,
) -> None:
    """Two instruments and two captures remain isolated across stable null-date pages."""
    store = _store(capture_engine)
    rows = [
        _record(instrument, capture, period, f"{instrument}-{capture[:1]}-{period}")
        for instrument in (INSTRUMENT_A, INSTRUMENT_B)
        for capture in (CAPTURE_A, CAPTURE_B)
        for period in ("2024-12-31", "2025-12-31")
    ]
    publication = _publish(store, "four-capture-pairs", rows)

    selected = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        instrument_id=INSTRUMENT_A,
        capture_id=CAPTURE_A,
        page_size=1,
    )
    repeat = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        instrument_id=INSTRUMENT_A,
        capture_id=CAPTURE_A,
        page_size=1,
    )
    other_capture = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        instrument_id=INSTRUMENT_A,
        capture_id=CAPTURE_B,
        page_size=1,
    )
    other_instrument = store.create_snapshot(
        DATASET_ID,
        publication_id=publication.publication_id,
        instrument_id=INSTRUMENT_B,
        capture_id=CAPTURE_A,
        page_size=1,
    )

    first_page = store.read_page(selected.snapshot_id)
    assert selected.record_count == 2
    assert selected.selection_hash == repeat.selection_hash
    assert selected.selection_hash != other_capture.selection_hash
    assert selected.selection_hash != other_instrument.selection_hash
    assert selected.content_hash == repeat.content_hash
    assert selected.content_hash != other_capture.content_hash
    assert selected.content_hash != other_instrument.content_hash
    assert first_page.next_cursor is not None
    first_row = first_page.records[0]
    assert first_row.business_date is None
    assert (first_row.payload["instrument_id"], first_row.payload["capture_id"]) == (
        INSTRUMENT_A,
        CAPTURE_A,
    )

    cursor_payload = _decode_cursor_payload(first_page.next_cursor)
    assert cursor_payload["version"] == 2
    assert cursor_payload["instrument_id"] == INSTRUMENT_A
    assert cursor_payload["capture_id"] == CAPTURE_A

    second_page = store.read_page(selected.snapshot_id, cursor=first_page.next_cursor)
    assert len(second_page.records) == 1
    second_row = second_page.records[0]
    assert second_row.business_date is None
    assert (second_row.payload["instrument_id"], second_row.payload["capture_id"]) == (
        INSTRUMENT_A,
        CAPTURE_A,
    )
    assert first_row.business_key < second_row.business_key
    assert first_page.next_cursor is not None
    assert second_page.next_cursor is None

    with pytest.raises(SnapshotError):
        store.read_page(other_capture.snapshot_id, cursor=first_page.next_cursor)
    forged_payload = dict(cursor_payload)
    forged_payload["capture_id"] = CAPTURE_B
    with pytest.raises(SnapshotError):
        store.read_page(selected.snapshot_id, cursor=_sign_cursor_payload(forged_payload))


@pytest.mark.parametrize(
    "case",
    ["half-metadata", "payload-mismatch", "invalid-instrument", "invalid-capture", "wrong-type"],
)
def test_store_rejects_invalid_or_mismatched_selector_metadata(
    capture_engine: Engine, case: str
) -> None:
    """Only a valid explicit pair equal to the payload may enter the indexed store path."""
    store = _store(capture_engine)
    valid = _record(INSTRUMENT_A, CAPTURE_A, "2024-12-31", "1")
    if case == "half-metadata":
        invalid = replace(valid, capture_id=None)
    elif case == "payload-mismatch":
        invalid = replace(valid, payload={**valid.payload, "capture_id": CAPTURE_B})
    elif case == "invalid-instrument":
        invalid = replace(
            valid,
            instrument_id="600 000.SH",
            payload={**valid.payload, "instrument_id": "600 000.SH"},
        )
    elif case == "invalid-capture":
        invalid = replace(
            valid,
            capture_id="A" * 64,
            payload={**valid.payload, "capture_id": "A" * 64},
        )
    else:
        invalid = replace(valid, instrument_id=123, payload={**valid.payload, "instrument_id": 123})

    with pytest.raises(ValueError):
        _publish(store, f"invalid-selector-{case}", [invalid])

    with capture_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(PUBLICATIONS)).scalar_one() == 0
        assert (
            connection.execute(select(func.count()).select_from(PUBLICATION_HEADS)).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                select(func.count()).select_from(PUBLISHED_FACT_VERSIONS)
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("instrument_id", "capture_id"),
    [
        (INSTRUMENT_A, None),
        ("600 000.SH", CAPTURE_A),
        (INSTRUMENT_A, "A" * 64),
    ],
)
def test_snapshot_rejects_half_or_malformed_selector_pair(
    capture_engine: Engine, instrument_id: str, capture_id: str | None
) -> None:
    """Snapshot creation requires a complete, valid selector before persisting scope."""
    store = _store(capture_engine)
    publication = _publish(
        store,
        "snapshot-selector-validation",
        [_record(INSTRUMENT_A, CAPTURE_A, "2024-12-31", "1")],
    )

    with pytest.raises(ValueError):
        store.create_snapshot(
            DATASET_ID,
            publication_id=publication.publication_id,
            instrument_id=instrument_id,
            capture_id=capture_id,
        )

    with capture_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(SNAPSHOTS)).scalar_one() == 0


@pytest.mark.parametrize(
    ("initial_pair", "incoming_pair"),
    [
        (None, (INSTRUMENT_A, CAPTURE_A)),
        ((INSTRUMENT_A, CAPTURE_A), None),
        ((INSTRUMENT_A, CAPTURE_A), (INSTRUMENT_B, CAPTURE_B)),
    ],
)
def test_business_key_scope_moves_fail_atomically(
    capture_engine: Engine,
    initial_pair: tuple[str, str] | None,
    incoming_pair: tuple[str, str] | None,
) -> None:
    """NULL/pair and pair/pair changes leave publication head and revisions untouched."""
    store = _store(capture_engine)
    payload_pair = initial_pair or (INSTRUMENT_A, CAPTURE_A)
    initial = _record(
        *payload_pair,
        "2024-12-31",
        "1",
        indexed=initial_pair is not None,
        business_key="immutable-source-key",
    )
    first = _publish(
        store,
        "initial-scope",
        [initial],
        watermark={"marker": "initial"},
    )
    incoming_values = incoming_pair or payload_pair
    changed = _record(
        *incoming_values,
        "2024-12-31",
        "2",
        indexed=incoming_pair is not None,
        business_key="immutable-source-key",
    )

    with pytest.raises(PublicationContractConflictError):
        _publish(
            store,
            "forbidden-scope-move",
            [changed],
            watermark={"marker": "must-not-advance"},
        )

    with capture_engine.connect() as connection:
        head = (
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == DATASET_ID)
            )
            .mappings()
            .one()
        )
        publications = connection.execute(
            select(func.count()).select_from(PUBLICATIONS)
        ).scalar_one()
        facts = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).where(
                    PUBLISHED_FACT_VERSIONS.c.dataset_id == DATASET_ID
                )
            )
            .mappings()
            .all()
        )
    assert head["latest_sequence"] == 1
    assert head["latest_publication_id"] == first.publication_id
    assert json.loads(head["watermark_json"]) == {"marker": "initial"}
    assert publications == 1
    assert len(facts) == 1
    assert facts[0]["revision"] == 1
    assert (facts[0]["instrument_id"], facts[0]["capture_id"]) == (initial_pair or (None, None))


def test_same_retry_key_cannot_switch_from_unindexed_to_indexed(capture_engine: Engine) -> None:
    """Changing only explicit indexing intent changes canonical retry semantics."""
    store = _store(capture_engine)
    legacy = _record(INSTRUMENT_A, CAPTURE_A, "2024-12-31", "1", indexed=False)
    selected = _record(INSTRUMENT_A, CAPTURE_A, "2024-12-31", "1", indexed=True)
    first = _publish(store, "same-retry-key", [legacy], watermark={"marker": "initial"})

    with pytest.raises(PublicationConflictError, match="different publication payload"):
        _publish(store, "same-retry-key", [selected], watermark={"marker": "initial"})

    assert len(store.list_publications(DATASET_ID)) == 1
    assert store.list_publications(DATASET_ID)[0].publication_id == first.publication_id
    with capture_engine.connect() as connection:
        head = (
            connection.execute(
                select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == DATASET_ID)
            )
            .mappings()
            .one()
        )
        fact = (
            connection.execute(
                select(PUBLISHED_FACT_VERSIONS).where(
                    PUBLISHED_FACT_VERSIONS.c.business_key == legacy.business_key
                )
            )
            .mappings()
            .one()
        )
    assert head["latest_sequence"] == 1
    assert head["latest_publication_id"] == first.publication_id
    assert fact["revision"] == 1
    assert fact["instrument_id"] is None
    assert fact["capture_id"] is None
