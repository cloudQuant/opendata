"""Transactional behavior of the metadata publication-event receipt writer."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import IntegrityError

from opendata.data import metadata_receipt_writer as receipt_writer
from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS
from opendata.pipeline import publication_outbox as outbox

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

_PRODUCER_IDENTITY = "opendata:deployment-pin-alpha"
_PUBLICATION_ID = "a" * 32
_PAYLOAD_SHA256 = "b" * 64
_PUBLISHED_AT_US = 1_792_992_000_000_000
_EXPECTED_EVENT_SHA256 = "d380e093f72707f0139492fea0814b2c11f8ec8bf04ce2b6beac381277871d57"
_EVENT_DOMAIN = b"opendata.publication-outbox.event.v1\n"
_PROVENANCE_JSON = b'{"provider":"synthetic"}'
_PROVENANCE_SHA256 = hashlib.sha256(_PROVENANCE_JSON).hexdigest()
_EXPECTED_EVENT_JSON_UTF8 = (
    b'{"contract_evidence_kind":"absent","contract_sha256":null,'
    b'"dataset_id":"equity.daily","event_type":"opendata.publication.published",'
    b'"event_version":1,"payload_sha256":"'
    + _PAYLOAD_SHA256.encode("ascii")
    + b'","provenance_sha256":"'
    + _PROVENANCE_SHA256.encode("ascii")
    + b'","publication_id":"'
    + _PUBLICATION_ID.encode("ascii")
    + b'","publication_sequence":7,"published_at_us":1792992000000000,'
    b'"record_count":1,"schema_version":"2"}'
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Create and dispose a private file-backed SQLite receipt database."""
    database = create_engine(f"sqlite+pysqlite:///{tmp_path / 'metadata-receipt.sqlite'}")
    METADATA_PUBLICATION_RECEIPTS.create(database)
    try:
        yield database
    finally:
        database.dispose()


def _golden_event() -> tuple[bytes, str]:
    """Return the shared publication-outbox v1 golden wire vector."""
    return _EXPECTED_EVENT_JSON_UTF8, _EXPECTED_EVENT_SHA256


def _event_for(**changes: object) -> tuple[bytes, str]:
    """Build deliberate collision variants through the production event contract."""
    candidate = outbox._PublicationOutboxCandidate(
        publication_id=_PUBLICATION_ID,
        dataset_id="equity.daily",
        sequence=7,
        record_count=1,
        payload_sha256=_PAYLOAD_SHA256,
        contract_sha256=None,
        contract_evidence_kind="absent",
        provenance_sha256=_PROVENANCE_SHA256,
        schema_version="2",
        published_at_us=_PUBLISHED_AT_US,
    )
    event = outbox._build_publication_event(replace(candidate, **changes))
    return event.event_json.encode("utf-8"), event.event_sha256


def _digest_event(event_json_utf8: bytes) -> str:
    """Calculate the wire digest used only to exercise malformed envelopes."""
    return hashlib.sha256(_EVENT_DOMAIN + event_json_utf8).hexdigest()


def _receive(
    connection,
    event: tuple[bytes, str] | None = None,
    *,
    producer_identity: str = _PRODUCER_IDENTITY,
):
    """Call the public receiver with either the golden event or a supplied variant."""
    event_json_utf8, event_sha256 = event or _golden_event()
    return receipt_writer.receive_metadata_publication_receipt(
        connection,
        producer_identity=producer_identity,
        event_json_utf8=event_json_utf8,
        event_sha256=event_sha256,
    )


def test_persists_the_exact_outbox_golden_bytes_and_redundant_fields(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receiver stores the exact frozen bytes and copies their validated fields."""
    local_receive_time = 1_800_000_000_000_123
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: local_receive_time * 1_000)

    with engine.begin() as connection:
        result = _receive(connection)
        stored = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().one()

    assert result.replayed is False
    assert result.producer_identity == _PRODUCER_IDENTITY
    assert result.publication_id == _PUBLICATION_ID
    assert result.dataset_id == "equity.daily"
    assert result.publication_sequence == 7
    assert result.event_sha256 == _EXPECTED_EVENT_SHA256
    assert result.received_at_us == local_receive_time
    assert stored["producer_identity_evidence_kind"] == ("caller_supplied_external_pin_unverified")
    assert stored["publication_id"] == _PUBLICATION_ID
    assert stored["dataset_id"] == "equity.daily"
    assert stored["publication_sequence"] == 7
    assert stored["record_count"] == 1
    assert stored["payload_sha256"] == _PAYLOAD_SHA256
    assert stored["contract_sha256"] is None
    assert stored["contract_evidence_kind"] == "absent"
    assert stored["provenance_sha256"] == _PROVENANCE_SHA256
    assert stored["schema_version"] == "2"
    assert stored["event_version"] == 1
    assert stored["event_json_utf8"] == _EXPECTED_EVENT_JSON_UTF8
    assert stored["event_sha256"] == _EXPECTED_EVENT_SHA256
    assert stored["published_at_us"] == _PUBLISHED_AT_US
    assert stored["received_at_us"] == local_receive_time


def test_preserves_caller_supplied_contract_digest_as_unverified_evidence(engine: Engine) -> None:
    """A contract digest survives receipt storage without being upgraded to verified."""
    contract_sha256 = "c" * 64
    event = _event_for(
        contract_sha256=contract_sha256,
        contract_evidence_kind="caller_supplied_unverified",
    )
    with engine.begin() as connection:
        _receive(connection, event)
        stored = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().one()

    assert stored["contract_sha256"] == contract_sha256
    assert stored["contract_evidence_kind"] == "caller_supplied_unverified"


def test_exact_replay_returns_the_first_local_receive_time_without_update(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact duplicate returns the first receive timestamp and leaves the row intact."""
    first_receive_time = 1_800_000_000_000_123
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: first_receive_time * 1_000)
    with engine.begin() as connection:
        first = _receive(connection)
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: (first_receive_time + 99) * 1_000)

    with engine.begin() as connection:
        replay = _receive(connection)
        stored = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().one()

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.received_at_us == first_receive_time
    assert stored["received_at_us"] == first_receive_time
    assert stored["event_json_utf8"] == _EXPECTED_EVENT_JSON_UTF8


def test_outer_rollback_removes_a_receipt_after_its_savepoint_released(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releasing the writer savepoint does not detach its insert from outer rollback."""
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: 1_800_000_000_000_123_000)
    with engine.connect() as connection:
        outer = connection.begin()
        result = _receive(connection)
        assert result.replayed is False
        outer.rollback()

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).all()
    assert rows == []


@pytest.mark.parametrize(
    "variant",
    [
        {"record_count": 2},
        {"publication_id": "c" * 32},
    ],
    ids=["publication-id-conflict", "dataset-sequence-conflict"],
)
def test_each_unique_identity_rejects_different_event_content(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    variant: dict[str, object],
) -> None:
    """Either unique key fails closed when the matching row binds other bytes."""
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: 1_800_000_000_000_123_000)
    with engine.begin() as connection:
        _receive(connection)

    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptConflictError),
    ):
        _receive(connection, _event_for(**variant))

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["event_json_utf8"] == _EXPECTED_EVENT_JSON_UTF8


def test_two_unique_identities_resolving_to_separate_rows_conflict(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receiver rejects split identity matches instead of choosing one row."""
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: 1_800_000_000_000_123_000)
    with engine.begin() as connection:
        _receive(connection)
        _receive(connection, _event_for(publication_id="c" * 32, sequence=8))

    split_match = _event_for(sequence=8)
    with (
        engine.begin() as connection,
        pytest.raises(
            receipt_writer.MetadataPublicationReceiptConflictError,
            match="different receipts",
        ),
    ):
        _receive(connection, split_match)

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().all()
    assert len(rows) == 2


def test_conflicting_replay_rolls_back_only_its_savepoint(engine: Engine) -> None:
    """A handled conflict leaves the caller transaction usable for another receipt."""
    with engine.begin() as connection:
        _receive(connection)

    with engine.begin() as connection:
        with pytest.raises(receipt_writer.MetadataPublicationReceiptConflictError):
            _receive(connection, _event_for(record_count=2))
        next_event = _event_for(
            publication_id="c" * 32,
            dataset_id="equity.weekly",
            sequence=1,
        )
        result = _receive(connection, next_event)
        assert result.replayed is False

    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(METADATA_PUBLICATION_RECEIPTS).order_by(
                    METADATA_PUBLICATION_RECEIPTS.c.publication_id
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2
    assert rows[0]["publication_id"] == _PUBLICATION_ID
    assert rows[1]["publication_id"] == "c" * 32


def test_nonidentity_check_failure_propagates_and_outer_transaction_can_continue(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CHECK failure is not replay and only its savepoint rolls back."""
    original_receipt_values = receipt_writer._receipt_values

    def invalid_record_count(
        producer_identity: str,
        event: receipt_writer._ValidatedEvent,
        received_at_us: int,
    ) -> dict[str, object]:
        values = original_receipt_values(producer_identity, event, received_at_us)
        values["record_count"] = -1
        return values

    monkeypatch.setattr(receipt_writer, "_receipt_values", invalid_record_count)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            _receive(connection)

        monkeypatch.setattr(receipt_writer, "_receipt_values", original_receipt_values)
        next_event = _event_for(
            publication_id="c" * 32,
            dataset_id="equity.weekly",
            sequence=1,
        )
        result = _receive(connection, next_event)
        assert result.replayed is False

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["publication_id"] == "c" * 32


def test_duplicate_error_without_matching_receipt_fails_closed_and_keeps_outer_transaction(
    engine: Engine,
) -> None:
    """A classified duplicate without a matching row is not reported as replay."""

    class DuplicateWithoutReceiptConnection:
        """Inject a reviewed SQLite duplicate while delegating transaction work."""

        def __init__(self, connection: object) -> None:
            self.connection = connection
            self.dialect = connection.dialect
            self.inject_duplicate = True

        def in_transaction(self) -> bool:
            return self.connection.in_transaction()

        def exec_driver_sql(self, statement: str) -> object:
            return self.connection.exec_driver_sql(statement)

        def begin_nested(self) -> object:
            return self.connection.begin_nested()

        def execute(self, statement: object, *args: object, **kwargs: object) -> object:
            if self.inject_duplicate:
                self.inject_duplicate = False
                raise IntegrityError(
                    "INSERT",
                    {},
                    SimpleNamespace(
                        args=(
                            "UNIQUE constraint failed: "
                            "od_metadata_publication_receipts.producer_identity, "
                            "od_metadata_publication_receipts.publication_id",
                        )
                    ),
                )
            return self.connection.execute(statement, *args, **kwargs)

    with engine.begin() as connection:
        adapter = DuplicateWithoutReceiptConnection(connection)
        with pytest.raises(
            receipt_writer.MetadataPublicationReceiptError,
            match="did not resolve to either receipt identity",
        ):
            _receive(adapter)  # type: ignore[arg-type]

        next_event = _event_for(
            publication_id="c" * 32,
            dataset_id="equity.weekly",
            sequence=1,
        )
        result = _receive(connection, next_event)
        assert result.replayed is False

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).mappings().all()
    assert len(rows) == 1
    assert rows[0]["publication_id"] == "c" * 32


@pytest.mark.parametrize(
    "tamper",
    [
        {"payload_sha256": "f" * 64},
        {"event_json_utf8": b'{"tampered":true}'},
        {"event_json_utf8": "tampered text instead of binary"},
        {"event_sha256": "e" * 64},
    ],
    ids=["redundant-column", "event-bytes", "event-text-type", "event-digest"],
)
def test_replay_fails_closed_when_an_existing_row_was_directly_tampered(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tamper: dict[str, object],
) -> None:
    """A new valid event cannot bless a row whose immutable columns changed in SQL."""
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: 1_800_000_000_000_123_000)
    with engine.begin() as connection:
        _receive(connection)
    with engine.begin() as connection:
        if isinstance(tamper.get("event_json_utf8"), str):
            connection.execute(
                text(
                    "UPDATE od_metadata_publication_receipts "
                    "SET event_json_utf8 = :event_json_utf8 "
                    "WHERE publication_id = :publication_id"
                ),
                {
                    "event_json_utf8": tamper["event_json_utf8"],
                    "publication_id": _PUBLICATION_ID,
                },
            )
            stored_event = connection.execute(
                select(METADATA_PUBLICATION_RECEIPTS.c.event_json_utf8).where(
                    METADATA_PUBLICATION_RECEIPTS.c.publication_id == _PUBLICATION_ID
                )
            ).scalar_one()
            assert isinstance(stored_event, str)
        else:
            connection.execute(
                update(METADATA_PUBLICATION_RECEIPTS)
                .where(METADATA_PUBLICATION_RECEIPTS.c.publication_id == _PUBLICATION_ID)
                .values(**tamper)
            )

    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptConflictError),
    ):
        _receive(connection)


@pytest.mark.parametrize(
    "received_at_us",
    [-1, receipt_writer._MAX_TIMESTAMP_US + 1, 1.5, True],
    ids=["negative", "too-large", "float", "boolean"],
)
def test_exact_replay_rejects_corrupted_first_receive_time(
    received_at_us: object,
) -> None:
    """A corrupted persisted receiver timestamp cannot be returned as trusted data."""
    row = {"event_json_utf8": b"event", "received_at_us": received_at_us}

    with pytest.raises(
        receipt_writer.MetadataPublicationReceiptConflictError,
        match="first receive time",
    ):
        receipt_writer._require_exact_replay(  # type: ignore[arg-type]
            row,
            {"event_json_utf8": b"event"},
            b"event",
        )


@pytest.mark.parametrize(
    ("event", "producer_identity", "message"),
    [
        ((b"{}", _EXPECTED_EVENT_SHA256), _PRODUCER_IDENTITY, "event fields"),
        (
            (_EXPECTED_EVENT_JSON_UTF8 + b" ", _digest_event(_EXPECTED_EVENT_JSON_UTF8 + b" ")),
            _PRODUCER_IDENTITY,
            "canonical",
        ),
        (
            (
                _EXPECTED_EVENT_JSON_UTF8.replace(
                    b'"event_type":"opendata.publication.published"',
                    b'"event_type":"opendata.publication.published",'
                    b'"event_type":"opendata.publication.published"',
                ),
                _digest_event(
                    _EXPECTED_EVENT_JSON_UTF8.replace(
                        b'"event_type":"opendata.publication.published"',
                        b'"event_type":"opendata.publication.published",'
                        b'"event_type":"opendata.publication.published"',
                    )
                ),
            ),
            _PRODUCER_IDENTITY,
            "duplicate keys",
        ),
        ((_EXPECTED_EVENT_JSON_UTF8, "0" * 64), _PRODUCER_IDENTITY, "domain-separated"),
        ((_EXPECTED_EVENT_JSON_UTF8, "not-a-sha256"), _PRODUCER_IDENTITY, "lowercase SHA-256"),
        ((b"\xff\xfe", _digest_event(b"\xff\xfe")), _PRODUCER_IDENTITY, "valid UTF-8"),
        ((b"NaN", _digest_event(b"NaN")), _PRODUCER_IDENTITY, "valid UTF-8"),
        ((b"x" * 8_193, _digest_event(b"x" * 8_193)), _PRODUCER_IDENTITY, "2–8192"),
        (
            (
                _EXPECTED_EVENT_JSON_UTF8.replace(
                    b"opendata.publication.published", b"opendata.publication.retracted"
                ),
                _digest_event(
                    _EXPECTED_EVENT_JSON_UTF8.replace(
                        b"opendata.publication.published", b"opendata.publication.retracted"
                    )
                ),
            ),
            _PRODUCER_IDENTITY,
            "event_type",
        ),
        (
            (
                _EXPECTED_EVENT_JSON_UTF8.replace(b'"event_version":1', b'"event_version":true'),
                _digest_event(
                    _EXPECTED_EVENT_JSON_UTF8.replace(b'"event_version":1', b'"event_version":true')
                ),
            ),
            _PRODUCER_IDENTITY,
            "event_version",
        ),
        (
            (
                _EXPECTED_EVENT_JSON_UTF8.replace(
                    b'"publication_sequence":7', b'"publication_sequence":true'
                ),
                _digest_event(
                    _EXPECTED_EVENT_JSON_UTF8.replace(
                        b'"publication_sequence":7', b'"publication_sequence":true'
                    )
                ),
            ),
            _PRODUCER_IDENTITY,
            "event values",
        ),
        ((_EXPECTED_EVENT_JSON_UTF8, _EXPECTED_EVENT_SHA256), "来源:producer", "ASCII"),
    ],
    ids=[
        "wrong-fields",
        "noncanonical",
        "duplicate-json-key",
        "wrong-digest",
        "invalid-digest-shape",
        "invalid-utf8",
        "non-finite-json-number",
        "oversized-event",
        "wrong-event-type",
        "boolean-event-version",
        "boolean-sequence",
        "unicode-pin",
    ],
)
def test_malformed_event_or_external_pin_is_rejected_before_insert(
    engine: Engine,
    event: tuple[bytes, str],
    producer_identity: str,
    message: str,
) -> None:
    """Wire shape, canonical bytes, explicit digest, and pin constraints fail closed."""
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError, match=message),
    ):
        _receive(connection, event, producer_identity=producer_identity)

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).all()
    assert rows == []


def test_deeply_nested_json_is_rejected_independent_of_parser_depth_behavior(
    engine: Engine,
) -> None:
    """A deeply nested non-event cannot be accepted regardless of parser limits."""
    nested_json = b"[" * 1_500 + b"]" * 1_500
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError),
    ):
        receipt_writer.receive_metadata_publication_receipt(
            connection,
            producer_identity=_PRODUCER_IDENTITY,
            event_json_utf8=nested_json,
            event_sha256=_digest_event(nested_json),
        )

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).all()
    assert rows == []


def test_json_parser_recursion_error_is_wrapped_as_receipt_error(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser recursion failure is exposed as the receiver's domain error."""

    def raise_recursion_error(*args: object, **kwargs: object) -> object:
        raise RecursionError("simulated JSON decoder recursion limit")

    monkeypatch.setattr(receipt_writer.json, "loads", raise_recursion_error)
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="valid UTF-8"),
    ):
        _receive(connection)

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).all()
    assert rows == []


@pytest.mark.parametrize(
    "variant",
    [
        {"dataset_id": "equity.每日"},
        {"schema_version": "版本一"},
        {"schema_version": "version one"},
    ],
    ids=["unicode-dataset-id", "unicode-schema-version", "non-identifier-schema-version"],
)
def test_ascii_receipt_contract_rejects_unicode_allowed_by_current_publisher(
    engine: Engine,
    variant: dict[str, object],
) -> None:
    """The current publication _label accepts labels that receipt schema 0008 excludes."""
    event = _event_for(**variant)
    message = "dataset_id|schema_version"
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError, match=message),
    ):
        _receive(connection, event)


def test_wrong_supplied_event_digest_is_not_replaced_by_a_computed_digest(engine: Engine) -> None:
    """The source digest is mandatory and must match the event hash domain exactly."""
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="domain-separated"),
    ):
        receipt_writer.receive_metadata_publication_receipt(
            connection,
            producer_identity=_PRODUCER_IDENTITY,
            event_json_utf8=_EXPECTED_EVENT_JSON_UTF8,
            event_sha256="0" * 64,
        )


@pytest.mark.parametrize(
    "clock_value",
    [-1_000, (receipt_writer._MAX_TIMESTAMP_US + 1) * 1_000, 1.0],
    ids=["negative-clock", "clock-after-schema-range", "non-integer-clock"],
)
def test_invalid_local_receiver_clock_fails_before_insert(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    clock_value: int | float,
) -> None:
    """Invalid host time cannot be persisted as the first receipt timestamp."""
    monkeypatch.setattr(receipt_writer, "time_ns", lambda: clock_value)
    with (
        engine.begin() as connection,
        pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="clock"),
    ):
        _receive(connection)

    with engine.connect() as connection:
        rows = connection.execute(select(METADATA_PUBLICATION_RECEIPTS)).all()
    assert rows == []


def test_writer_requires_an_explicit_outer_transaction(engine: Engine) -> None:
    """A bare connection cannot cause this receiver function to own a commit."""
    with (
        engine.connect() as connection,
        pytest.raises(
            receipt_writer.MetadataPublicationReceiptError, match="caller-owned transaction"
        ),
    ):
        _receive(connection)


def test_sqlite_duplicate_classifier_is_precise_on_python_310() -> None:
    """Old Python SQLite drivers use exact unique messages without error-code attributes."""
    primary_key_error = IntegrityError(
        "INSERT",
        {},
        SimpleNamespace(
            args=(
                "UNIQUE constraint failed: "
                "od_metadata_publication_receipts.producer_identity, "
                "od_metadata_publication_receipts.publication_id",
            )
        ),
    )
    sequence_error = IntegrityError(
        "INSERT",
        {},
        SimpleNamespace(
            args=(
                "UNIQUE constraint failed: "
                "od_metadata_publication_receipts.producer_identity, "
                "od_metadata_publication_receipts.dataset_id, "
                "od_metadata_publication_receipts.publication_sequence",
            )
        ),
    )
    check_error = IntegrityError(
        "INSERT",
        {},
        SimpleNamespace(args=("CHECK constraint failed: ck_od_meta_receipt_sequence",)),
    )

    assert receipt_writer._is_identity_duplicate_error("sqlite", primary_key_error)
    assert receipt_writer._is_identity_duplicate_error("sqlite", sequence_error)
    assert not receipt_writer._is_identity_duplicate_error("sqlite", check_error)
    assert not receipt_writer._is_identity_duplicate_error("mysql", primary_key_error)


def test_mysql_duplicate_classifier_requires_errno_1062() -> None:
    """Only the reviewed MySQL duplicate-key errno enters the idempotent read path."""
    duplicate = IntegrityError("INSERT", {}, SimpleNamespace(args=(1062, "duplicate")))
    check_failure = IntegrityError("INSERT", {}, SimpleNamespace(args=(3819, "check")))

    assert receipt_writer._is_identity_duplicate_error("mysql", duplicate)
    assert not receipt_writer._is_identity_duplicate_error("mysql", check_failure)
    assert not receipt_writer._is_identity_duplicate_error("postgresql", duplicate)


class _FakeMySQLRows:
    """Mapping result for the runtime InnoDB metadata guard."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _FakeMySQLRows:
        """Return this mapping result."""
        return self

    def all(self) -> list[dict[str, object]]:
        """Return configured metadata rows."""
        return self.rows


class _FakeMySQLConnection:
    """Capture the single table-engine check without opening a database."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.dialect = SimpleNamespace(
            name="mysql",
            server_version_info=(8, 0, 36),
            is_mariadb=False,
        )
        self.rows = rows
        self.statement: object | None = None
        self.parameters: dict[str, object] | None = None
        self.query_count = 0
        self.query_error: Exception | None = None
        self.driver_sql_count = 0

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _FakeMySQLRows:
        """Capture the query and return synthetic metadata for path-level tests."""
        self.query_count += 1
        self.statement = statement
        self.parameters = parameters
        if self.query_error is not None:
            raise self.query_error
        return _FakeMySQLRows(self.rows)

    def in_transaction(self) -> bool:
        """Report a synthetic caller-owned transaction."""
        return True

    def begin_nested(self) -> object:
        """Provide a no-op context for verifying dialect dispatch only."""
        return nullcontext()

    def exec_driver_sql(self, statement: str) -> None:
        """Record unexpected SQLite-specific bootstrap calls."""
        self.driver_sql_count += 1


def test_mysql_guard_uses_one_bound_query_and_requires_innodb() -> None:
    """MySQL engine verification is current-schema scoped, parameterized, and uncached."""
    table_name = METADATA_PUBLICATION_RECEIPTS.name
    connection = _FakeMySQLConnection([{"table_name": table_name, "engine": "InnoDB"}])

    receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]

    assert connection.query_count == 1
    assert connection.parameters == {"table_name": table_name}
    assert "TABLE_SCHEMA = DATABASE()" in str(connection.statement)

    myisam = _FakeMySQLConnection([{"table_name": table_name, "engine": "MyISAM"}])
    with pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="InnoDB"):
        receipt_writer._require_supported_connection(myisam)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("dialect_name", "is_mariadb"),
    [("postgresql", False), ("mysql", True)],
    ids=["unsupported-dialect", "mariadb-is-not-mysql"],
)
def test_connection_guard_rejects_non_mysql8_backends(
    dialect_name: str,
    is_mariadb: bool,
) -> None:
    """Only SQLite and non-MariaDB MySQL 8 are accepted by the writer boundary."""
    connection = _FakeMySQLConnection([])
    connection.dialect.name = dialect_name
    connection.dialect.is_mariadb = is_mariadb

    with pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="support SQLite"):
        receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "version",
    [(8, 0, 15), (9, 0, 0), (8, 0)],
    ids=["before-minimum", "unsupported-major-version", "missing-patch"],
)
def test_connection_guard_rejects_unsupported_mysql_versions(
    version: tuple[int, ...],
) -> None:
    """MySQL version validation fails closed before catalog inspection."""
    connection = _FakeMySQLConnection([])
    connection.dialect.server_version_info = version

    with pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="8.0.16"):
        receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]
    assert connection.query_count == 0


def test_connection_guard_wraps_engine_metadata_query_failure() -> None:
    """A failed catalog lookup cannot be mistaken for an InnoDB target."""
    connection = _FakeMySQLConnection([])
    connection.query_error = RuntimeError("synthetic catalog outage")

    with pytest.raises(
        receipt_writer.MetadataPublicationReceiptError, match="could not verify"
    ) as error:
        receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]

    assert isinstance(error.value.__cause__, RuntimeError)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {"table_name": METADATA_PUBLICATION_RECEIPTS.name, "engine": "InnoDB"},
            {"table_name": METADATA_PUBLICATION_RECEIPTS.name, "engine": "InnoDB"},
        ],
    ],
    ids=["missing-table", "ambiguous-table"],
)
def test_connection_guard_requires_exactly_one_receipt_table(
    rows: list[dict[str, object]],
) -> None:
    """Missing or ambiguous catalog identity fails closed."""
    connection = _FakeMySQLConnection(rows)

    with pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="exactly one"):
        receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "row",
    [
        {"table_name": "unexpected_table", "engine": "InnoDB"},
        {"table_name": METADATA_PUBLICATION_RECEIPTS.name, "engine": None},
    ],
    ids=["wrong-table-name", "invalid-engine-metadata"],
)
def test_connection_guard_rejects_malformed_engine_metadata(
    row: dict[str, object],
) -> None:
    """Catalog rows must identify the expected table and a string engine."""
    connection = _FakeMySQLConnection([row])

    with pytest.raises(receipt_writer.MetadataPublicationReceiptError, match="metadata is invalid"):
        receipt_writer._require_supported_connection(connection)  # type: ignore[arg-type]


def test_mysql_dispatch_path_skips_sqlite_bootstrap_with_synthetic_connection() -> None:
    """Path-level dispatch omits SQLite-only SQL; this fake is not MySQL acceptance."""
    table_name = METADATA_PUBLICATION_RECEIPTS.name
    connection = _FakeMySQLConnection([{"table_name": table_name, "engine": "InnoDB"}])

    result = receipt_writer.receive_metadata_publication_receipt(
        connection,  # type: ignore[arg-type]
        producer_identity=_PRODUCER_IDENTITY,
        event_json_utf8=_EXPECTED_EVENT_JSON_UTF8,
        event_sha256=_EXPECTED_EVENT_SHA256,
    )

    assert result.replayed is False
    assert connection.query_count == 2
    assert connection.driver_sql_count == 0
