"""Persist exact copies of validated publication-outbox events.

This receiver boundary uses the producer's frozen outbox implementation for
event parsing, field validation, canonical JSON, and domain-separated hashing.
Its local receipt only proves that these bytes were stored in the supplied
transaction; the external producer pin and contract evidence remain unverified.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import time_ns
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, or_, select, text
from sqlalchemy.exc import IntegrityError

from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS
from opendata.pipeline import publication_outbox as _publication_outbox

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, RowMapping

__all__ = [
    "MetadataPublicationReceiptConflictError",
    "MetadataPublicationReceiptError",
    "MetadataPublicationReceiptResult",
    "receive_metadata_publication_receipt",
]

_EVENT_KEYS = frozenset(
    {
        "contract_evidence_kind",
        "contract_sha256",
        "dataset_id",
        "event_type",
        "event_version",
        "payload_sha256",
        "provenance_sha256",
        "publication_id",
        "publication_sequence",
        "published_at_us",
        "record_count",
        "schema_version",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_EVENT_BYTES = 8_192
_MAX_RECEIPT_TEXT_BYTES = 128
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_MIN_MYSQL_VERSION = (8, 0, 16)
_MYSQL_RECEIPT_ENGINE_QUERY = text(
    "SELECT TABLE_NAME AS table_name, ENGINE AS engine "
    "FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table_name"
)


class MetadataPublicationReceiptError(ValueError):
    """Raised when a publication event or receipt target is unsupported."""


class MetadataPublicationReceiptConflictError(MetadataPublicationReceiptError):
    """Raised when either receipt identity is bound to different stored data."""


@dataclass(frozen=True, slots=True)
class MetadataPublicationReceiptResult:
    """Result of inserting or exactly replaying one receipt.

    Attributes:
        producer_identity: Caller-supplied external pin, not an authenticated identity.
        publication_id: Frozen identifier from the publication event.
        dataset_id: Frozen dataset key from the publication event.
        publication_sequence: Frozen dataset sequence from the publication event.
        event_sha256: Verified domain-separated digest of the exact event bytes.
        received_at_us: Local receiver wall-clock time stored on the first insert.
        replayed: Whether an exact existing receipt was returned without updating it.
    """

    producer_identity: str
    publication_id: str
    dataset_id: str
    publication_sequence: int
    event_sha256: str
    received_at_us: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ValidatedEvent:
    """Canonical source event fields already checked by the production outbox."""

    publication_id: str
    dataset_id: str
    publication_sequence: int
    record_count: int
    payload_sha256: str
    contract_sha256: str | None
    contract_evidence_kind: str
    provenance_sha256: str
    schema_version: str
    event_version: int
    event_json_utf8: bytes
    event_sha256: str
    published_at_us: int


def receive_metadata_publication_receipt(
    connection: Connection,
    *,
    producer_identity: str,
    event_json_utf8: bytes,
    event_sha256: str,
) -> MetadataPublicationReceiptResult:
    """Persist a validated event copy in the caller-owned transaction.

    ``received_at_us`` is generated from this receiver process's wall clock at
    the time this function is called. It is not independently timestamped or
    tamper-proof. An exact replay returns the original stored value. This
    function never commits or rolls back the caller's outer transaction.

    Producer identity is a caller-supplied external pin, not authentication.
    Contract digest evidence is preserved exactly as absent or caller-supplied
    unverified. The upstream publication writer currently accepts Unicode
    ``dataset_id`` and ``schema_version`` labels, while receipt migration 0008
    documents ASCII ``Identifier`` fields; this receiver rejects Unicode and
    other non-``Identifier`` values in both fields until the two contracts are
    reconciled. The existing publication store's ``_label`` is broader than
    that expression.

    Args:
        connection: Active SQLAlchemy connection in a caller-owned transaction.
        producer_identity: Bounded printable ASCII external pin for the producer.
        event_json_utf8: Exact UTF-8 event bytes frozen by publication outbox 0018.
        event_sha256: Source event digest, verified using the outbox hash domain.

    Returns:
        The stored receipt identity, its first local receive time, and whether
        this call was an exact replay.

    Raises:
        MetadataPublicationReceiptConflictError: An event identity already
            exists with different immutable content, or its two identities
            resolve to separate rows.
        MetadataPublicationReceiptError: The event is malformed, a target is
            unsupported, or a duplicate-key error has no matching receipt.
        IntegrityError: A non-identity database integrity failure occurred.
        SQLAlchemyError: The database rejected a statement or transaction.
    """
    if not connection.in_transaction():
        raise MetadataPublicationReceiptError(
            "receipt writer requires an active caller-owned transaction"
        )

    _validate_producer_identity(producer_identity)
    event = _validate_event(event_json_utf8, event_sha256)
    _require_supported_connection(connection)
    received_at_us = _local_received_at_us()

    if connection.dialect.name == "sqlite":
        # Pysqlite may defer its physical BEGIN until the first DML. This
        # zero-row UPDATE makes the savepoint below a true nested transaction.
        connection.exec_driver_sql(
            "UPDATE od_metadata_publication_receipts "
            "SET received_at_us = received_at_us WHERE 1 = 0"
        )

    values = _receipt_values(producer_identity, event, received_at_us)
    table = METADATA_PUBLICATION_RECEIPTS
    identity_duplicate: IntegrityError | None = None
    try:
        with connection.begin_nested():
            try:
                connection.execute(insert(table).values(**values))
            except IntegrityError as error:
                if not _is_identity_duplicate_error(connection.dialect.name, error):
                    raise
                identity_duplicate = error
                # Re-raise so the savepoint context rolls back before a
                # MySQL locking/current read tries to find the conflicting row.
                raise
    except IntegrityError as error:
        if identity_duplicate is None or error is not identity_duplicate:
            raise

    if identity_duplicate is not None:
        # The failed insert's savepoint has now been rolled back. On MySQL,
        # FOR UPDATE bypasses any older REPEATABLE READ snapshot and observes
        # the transaction that won the unique-key race.
        matches = (
            connection.execute(
                select(table)
                .where(
                    or_(
                        (table.c.producer_identity == producer_identity)
                        & (table.c.publication_id == event.publication_id),
                        (table.c.producer_identity == producer_identity)
                        & (table.c.dataset_id == event.dataset_id)
                        & (table.c.publication_sequence == event.publication_sequence),
                    )
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if not matches:
            raise MetadataPublicationReceiptError(
                "duplicate-key failure did not resolve to either receipt identity"
            ) from identity_duplicate
        if len(matches) != 1:
            raise MetadataPublicationReceiptConflictError(
                "publication ID and dataset sequence resolve to different receipts"
            ) from identity_duplicate
        stored_received_at_us = _require_exact_replay(
            matches[0], values, event.event_json_utf8
        )
        return MetadataPublicationReceiptResult(
            producer_identity=producer_identity,
            publication_id=event.publication_id,
            dataset_id=event.dataset_id,
            publication_sequence=event.publication_sequence,
            event_sha256=event.event_sha256,
            received_at_us=stored_received_at_us,
            replayed=True,
        )

    return MetadataPublicationReceiptResult(
        producer_identity=producer_identity,
        publication_id=event.publication_id,
        dataset_id=event.dataset_id,
        publication_sequence=event.publication_sequence,
        event_sha256=event.event_sha256,
        received_at_us=received_at_us,
        replayed=False,
    )


def _validate_producer_identity(producer_identity: str) -> None:
    """Require a bounded printable ASCII external pin with stable whitespace."""
    if (
        not isinstance(producer_identity, str)
        or not producer_identity.isascii()
        or not 1 <= len(producer_identity) <= _MAX_RECEIPT_TEXT_BYTES
        or producer_identity != producer_identity.strip()
        or any(ord(character) < 32 or ord(character) > 126 for character in producer_identity)
    ):
        raise MetadataPublicationReceiptError(
            "producer_identity must be 1–128 printable ASCII characters without padding"
        )


def _validate_event(event_json_utf8: bytes, event_sha256: str) -> _ValidatedEvent:
    """Validate exact bytes against the frozen publication-outbox contract."""
    if not isinstance(event_json_utf8, bytes) or not 2 <= len(event_json_utf8) <= _MAX_EVENT_BYTES:
        raise MetadataPublicationReceiptError("event must be 2–8192 exact UTF-8 bytes")
    if not isinstance(event_sha256, str) or not _SHA256.fullmatch(event_sha256):
        raise MetadataPublicationReceiptError("event_sha256 must be lowercase SHA-256 hex")

    try:
        event_text = event_json_utf8.decode("utf-8", errors="strict")
        payload = json.loads(
            event_text,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_non_finite_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise MetadataPublicationReceiptError(
            "event bytes must contain one valid UTF-8 JSON object without duplicate keys"
        ) from error
    if not isinstance(payload, dict) or frozenset(payload) != _EVENT_KEYS:
        raise MetadataPublicationReceiptError("event fields do not match outbox version 1")
    if payload["event_type"] != _publication_outbox._EVENT_TYPE:
        raise MetadataPublicationReceiptError("event_type does not match publication outbox")
    if type(payload["event_version"]) is not int or payload["event_version"] != 1:
        raise MetadataPublicationReceiptError("event_version must be integer version 1")

    candidate = _publication_outbox._PublicationOutboxCandidate(
        publication_id=payload["publication_id"],
        dataset_id=payload["dataset_id"],
        sequence=payload["publication_sequence"],
        record_count=payload["record_count"],
        payload_sha256=payload["payload_sha256"],
        contract_sha256=payload["contract_sha256"],
        contract_evidence_kind=payload["contract_evidence_kind"],
        provenance_sha256=payload["provenance_sha256"],
        schema_version=payload["schema_version"],
        published_at_us=payload["published_at_us"],
    )
    try:
        candidate = _publication_outbox._normalize_candidate(candidate)
        canonical_event = _publication_outbox._build_publication_event(candidate)
        canonical_bytes = canonical_event.event_json.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise MetadataPublicationReceiptError(
            "event values do not satisfy publication outbox version 1"
        ) from error

    # Migration 0008 documents ASCII Identifier fields. The current publisher's
    # _label accepts Unicode and other non-Identifier labels, so this is an
    # explicit compatibility boundary until both contracts are reconciled.
    if not _IDENTIFIER.fullmatch(candidate.dataset_id):
        raise MetadataPublicationReceiptError(
            "Unicode or non-Identifier dataset_id is not compatible with receipt schema 0008"
        )
    if not _IDENTIFIER.fullmatch(candidate.schema_version):
        raise MetadataPublicationReceiptError(
            "non-Identifier schema_version is not compatible with receipt schema 0008"
        )
    if canonical_bytes != event_json_utf8:
        raise MetadataPublicationReceiptError(
            "event bytes are not the exact canonical publication-outbox representation"
        )
    if canonical_event.event_sha256 != event_sha256:
        raise MetadataPublicationReceiptError(
            "event_sha256 does not match the domain-separated event bytes"
        )

    return _ValidatedEvent(
        publication_id=candidate.publication_id,
        dataset_id=candidate.dataset_id,
        publication_sequence=candidate.sequence,
        record_count=candidate.record_count,
        payload_sha256=candidate.payload_sha256,
        contract_sha256=candidate.contract_sha256,
        contract_evidence_kind=candidate.contract_evidence_kind,
        provenance_sha256=candidate.provenance_sha256,
        schema_version=candidate.schema_version,
        event_version=canonical_event.event_version,
        event_json_utf8=event_json_utf8,
        event_sha256=canonical_event.event_sha256,
        published_at_us=candidate.published_at_us,
    )


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate names while decoding the event JSON object."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate event JSON key")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    """Reject JSON parser extensions for NaN and infinities."""
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _require_supported_connection(connection: Connection) -> None:
    """Require SQLite or a current InnoDB table on supported MySQL 8."""
    dialect = connection.dialect
    if dialect.name == "sqlite":
        return
    if dialect.name != "mysql" or getattr(dialect, "is_mariadb", False):
        raise MetadataPublicationReceiptError(
            "metadata publication receipts support SQLite and MySQL 8.0.16+ only"
        )
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < _MIN_MYSQL_VERSION
    ):
        raise MetadataPublicationReceiptError(
            "metadata publication receipts require MySQL 8.0.16 or later within MySQL 8"
        )
    try:
        rows = (
            connection.execute(
                _MYSQL_RECEIPT_ENGINE_QUERY,
                {"table_name": METADATA_PUBLICATION_RECEIPTS.name},
            )
            .mappings()
            .all()
        )
    except Exception as error:
        raise MetadataPublicationReceiptError(
            "could not verify metadata receipt table engine; refusing write"
        ) from error
    if len(rows) != 1:
        raise MetadataPublicationReceiptError(
            "could not identify exactly one metadata receipt table; refusing write"
        )
    table_name = rows[0]["table_name"]
    engine = rows[0]["engine"]
    if table_name != METADATA_PUBLICATION_RECEIPTS.name or not isinstance(engine, str):
        raise MetadataPublicationReceiptError(
            "metadata receipt table engine metadata is invalid; refusing write"
        )
    if engine.casefold() != "innodb":
        raise MetadataPublicationReceiptError(
            "metadata receipt table must use InnoDB; refusing write"
        )


def _local_received_at_us() -> int:
    """Return the receiver host's current wall-clock time in UTC epoch micros."""
    value = time_ns() // 1_000
    if type(value) is not int or not 0 <= value <= _MAX_TIMESTAMP_US:
        raise MetadataPublicationReceiptError(
            "receiver host clock is outside the supported receipt timestamp range"
        )
    return value


def _receipt_values(
    producer_identity: str,
    event: _ValidatedEvent,
    received_at_us: int,
) -> dict[str, object]:
    """Build all receipt columns from validated event data and receiver time."""
    return {
        "producer_identity": producer_identity,
        "producer_identity_evidence_kind": "caller_supplied_external_pin_unverified",
        "publication_id": event.publication_id,
        "dataset_id": event.dataset_id,
        "publication_sequence": event.publication_sequence,
        "record_count": event.record_count,
        "payload_sha256": event.payload_sha256,
        "contract_sha256": event.contract_sha256,
        "contract_evidence_kind": event.contract_evidence_kind,
        "provenance_sha256": event.provenance_sha256,
        "schema_version": event.schema_version,
        "event_version": event.event_version,
        "event_json_utf8": event.event_json_utf8,
        "event_sha256": event.event_sha256,
        "published_at_us": event.published_at_us,
        "received_at_us": received_at_us,
    }


def _is_identity_duplicate_error(dialect_name: str, error: IntegrityError) -> bool:
    """Recognize only reviewed SQLite and MySQL duplicate-identity errors."""
    original = error.orig
    if dialect_name == "sqlite":
        error_name = getattr(original, "sqlite_errorname", None)
        error_code = getattr(original, "sqlite_errorcode", None)
        if error_name in {
            "SQLITE_CONSTRAINT_PRIMARYKEY",
            "SQLITE_CONSTRAINT_UNIQUE",
        } or error_code in {1555, 2067}:
            return True
        # Python 3.10 sqlite3 errors may not expose sqlite_errorcode/name.
        # Accept only the exact column lists for the two 0008 unique keys.
        arguments = getattr(original, "args", ())
        message = arguments[0] if arguments and isinstance(arguments[0], str) else None
        return message in {
            "UNIQUE constraint failed: "
            "od_metadata_publication_receipts.producer_identity, "
            "od_metadata_publication_receipts.publication_id",
            "UNIQUE constraint failed: "
            "od_metadata_publication_receipts.producer_identity, "
            "od_metadata_publication_receipts.dataset_id, "
            "od_metadata_publication_receipts.publication_sequence",
        }
    if dialect_name == "mysql":
        arguments = getattr(original, "args", ())
        return bool(arguments) and arguments[0] == 1062
    return False


def _require_exact_replay(
    row: RowMapping,
    expected: dict[str, object],
    event_bytes: bytes,
) -> int:
    """Require every immutable column and event byte to match the retry."""
    for column, value in expected.items():
        if column == "received_at_us":
            continue
        stored = row[column]
        if column == "event_json_utf8" and not isinstance(stored, bytes):
            raise MetadataPublicationReceiptConflictError(
                "stored event bytes are not an exact binary receipt"
            )
        if stored != value:
            raise MetadataPublicationReceiptConflictError(
                "receipt identity already exists with different stored event content"
            )
    if row["event_json_utf8"] != event_bytes:
        raise MetadataPublicationReceiptConflictError(
            "stored event bytes differ from the replayed event"
        )
    received_at_us = row["received_at_us"]
    if (
        type(received_at_us) is not int
        or not 0 <= received_at_us <= _MAX_TIMESTAMP_US
    ):
        raise MetadataPublicationReceiptConflictError(
            "stored first receive time is outside the receipt schema bounds"
        )
    return received_at_us
