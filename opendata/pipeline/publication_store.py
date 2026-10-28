"""Append-only publication metadata and fixed, signed warehouse snapshots.

This module provides a bounded generic record envelope for publication and
snapshot mechanics. It is not a substitute for domain-specific DWD schemas,
constraints, or financial semantics. Callers supply the domain's stable
business key and retain business dates separately from observation timestamps.
The caller owns migrations and database lifetime; no table is created here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR
from sqlalchemy.engine import Connection, Engine, RootTransaction, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from opendata.data.capture_selection import (
    InstrumentCaptureSelection,
    InstrumentCaptureSelectionError,
    validate_selector_pair,
)
from opendata.pipeline.publication_outbox import (
    _build_publication_event,
    _candidate_from_publication_row,
    _event_values,
    _find_existing_event,
    _make_publication_outbox_table,
    _PublicationOutboxSourceError,
    _require_exact_event_replay,
    _require_supported_connection,
)

if TYPE_CHECKING:
    from sqlalchemy.sql.selectable import Select, Subquery

MAX_ID_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_BUSINESS_KEY_LENGTH = 512
MAX_PAGE_SIZE = 1_000
MAX_PUBLICATION_RECORDS = 100_000
MAX_SNAPSHOT_RECORDS = 100_000
MAX_COVERAGE_RECORDS = 100_000
MAX_COVERAGE_SCAN_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_DISCOVERY_ROWS = 10_000
_CAPTURE_DISCOVERY_INDEX_NAME = "ix_od_fact_capture_discovery"
_CAPTURE_DISCOVERY_INDEX_COLUMNS = (
    "dataset_id",
    "publication_id",
    "instrument_id",
    "publication_sequence",
    "is_deleted",
    "observed_at",
    "capture_id",
)
_CAPTURE_DISCOVERY_BINARY_COLUMNS = ("dataset_id", "instrument_id", "capture_id")
_MYSQL_CAPTURE_DISCOVERY_INDEX_METADATA_SQL = (
    "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, "
    "INDEX_TYPE, IS_VISIBLE, COLLATION FROM information_schema.STATISTICS "
    "WHERE TABLE_SCHEMA = DATABASE() "
    "AND TABLE_NAME = 'od_published_fact_versions' "
    "AND INDEX_NAME = :index_name ORDER BY SEQ_IN_INDEX"
)
CAPTURE_DISCOVERY_CURSOR_TTL = timedelta(hours=24)
VISIBLE_RECORD_SET_ALGORITHM = "visible-record-set-v1"
VISIBLE_RECORD_SET_SORT = "business-date-null-last-date-key:utf8-lex-v1"
VISIBLE_RECORD_HASH_ALGORITHM = "normalize-records-canonical-v1"
MAX_RECORD_BYTES = 256 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_CURSOR_BYTES = 8_192
_PAGE_ENVELOPE_BYTES = 16 * 1024
_PAGE_ROW_OVERHEAD_BYTES = 1_024
_READ_YIELD_PER = 16
_HASH_YIELD_PER = 256
MIN_SNAPSHOT_TTL = timedelta(seconds=1)
MAX_SNAPSHOT_TTL = timedelta(days=7)
_UTC = timezone.utc
_SECRET_KEY_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "privatekey",
    "secret",
)
_BEARER_VALUE = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_COMMON_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})$"
)
_CAPTURE_DISCOVERY_INSTRUMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]*", re.ASCII)

PUBLICATION_METADATA = MetaData()


def _key_type(length: int) -> String:
    """Use exact, case-sensitive, no-pad MySQL key comparisons and SQLite defaults."""
    return String(length).with_variant(
        VARCHAR(length, collation="utf8mb4_0900_bin"),
        "mysql",
    )


def _json_storage_type() -> Text:
    """Allow the documented bounded payloads on MySQL beyond its 64KiB TEXT cap."""
    return Text().with_variant(MEDIUMTEXT(), "mysql")


PUBLICATION_HEADS = Table(
    "od_publication_heads",
    PUBLICATION_METADATA,
    Column("dataset_id", _key_type(MAX_ID_LENGTH), primary_key=True),
    Column("latest_sequence", BigInteger, nullable=False),
    Column("latest_publication_id", String(32), nullable=True),
    Column("watermark_json", _json_storage_type(), nullable=True),
    Column("updated_at_us", BigInteger, nullable=False),
    CheckConstraint("latest_sequence >= 0", name="ck_od_publication_head_sequence"),
    mysql_engine="InnoDB",
)
PUBLICATIONS = Table(
    "od_publications",
    PUBLICATION_METADATA,
    Column("publication_id", String(32), primary_key=True),
    Column("dataset_id", _key_type(MAX_ID_LENGTH), nullable=False),
    Column("source_id", _key_type(MAX_ID_LENGTH), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("semantic_version", String(64), nullable=False),
    Column("unit", String(128), nullable=False),
    Column("adjustment_basis", String(128), nullable=False),
    Column("quality_json", _json_storage_type(), nullable=False),
    Column("provenance_json", _json_storage_type(), nullable=False),
    Column("watermark_json", _json_storage_type(), nullable=True),
    Column("idempotency_key", _key_type(MAX_IDEMPOTENCY_KEY_LENGTH), nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("record_count", Integer, nullable=False),
    Column("published_at_us", BigInteger, nullable=False),
    CheckConstraint("sequence > 0", name="ck_od_publication_sequence"),
    CheckConstraint("record_count >= 0", name="ck_od_publication_record_count"),
    Index(
        "uq_od_publication_dataset_sequence",
        "dataset_id",
        "sequence",
        unique=True,
    ),
    Index(
        "uq_od_publication_dataset_idempotency",
        "dataset_id",
        "idempotency_key",
        unique=True,
    ),
    Index("ix_od_publication_dataset_published", "dataset_id", "published_at_us"),
    mysql_engine="InnoDB",
)
PUBLICATION_OUTBOX_EVENTS = _make_publication_outbox_table(PUBLICATION_METADATA)
PUBLISHED_FACT_VERSIONS = Table(
    "od_published_fact_versions",
    PUBLICATION_METADATA,
    Column("dataset_id", _key_type(MAX_ID_LENGTH), primary_key=True),
    Column("business_key", _key_type(MAX_BUSINESS_KEY_LENGTH), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column(
        "publication_id", String(32), ForeignKey("od_publications.publication_id"), nullable=False
    ),
    Column("publication_sequence", BigInteger, nullable=False),
    Column("instrument_id", _key_type(MAX_ID_LENGTH), nullable=True),
    Column("capture_id", _key_type(64), nullable=True),
    Column("business_date", Date, nullable=True),
    Column("event_at", String(40), nullable=True),
    Column("observed_at", String(40), nullable=True),
    Column("source_published_at", String(40), nullable=True),
    Column("available_at", String(40), nullable=True),
    Column("record_json", _json_storage_type(), nullable=False),
    Column("record_hash", String(64), nullable=False),
    Column("is_deleted", Boolean, nullable=False, default=False),
    CheckConstraint("revision > 0", name="ck_od_published_fact_revision"),
    CheckConstraint(
        "(instrument_id IS NULL AND capture_id IS NULL) OR "
        "(instrument_id IS NOT NULL AND capture_id IS NOT NULL)",
        name="ck_od_fact_capture_pair",
    ),
    Index(
        "ix_od_published_fact_asof",
        "dataset_id",
        "business_key",
        "publication_sequence",
        "revision",
    ),
    Index(
        "ix_od_published_fact_date",
        "dataset_id",
        "business_date",
        "business_key",
    ),
    Index(
        "ix_od_fact_capture_read",
        "dataset_id",
        "instrument_id",
        "capture_id",
        "publication_sequence",
    ),
    mysql_engine="InnoDB",
)
SNAPSHOTS = Table(
    "od_publication_snapshots",
    PUBLICATION_METADATA,
    Column("snapshot_id", String(32), primary_key=True),
    Column("dataset_id", _key_type(MAX_ID_LENGTH), nullable=False),
    Column(
        "publication_id", String(32), ForeignKey("od_publications.publication_id"), nullable=False
    ),
    Column("schema_version", String(64), nullable=False),
    Column("publication_sequence", BigInteger, nullable=False),
    Column("instrument_id", _key_type(MAX_ID_LENGTH), nullable=True),
    Column("capture_id", _key_type(64), nullable=True),
    Column("start_date", Date, nullable=True),
    Column("end_date", Date, nullable=True),
    Column("page_size", Integer, nullable=False),
    Column("access_scope", _key_type(MAX_ID_LENGTH), nullable=False),
    Column("selection_sha256", String(64), nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("record_count", BigInteger, nullable=False),
    Column("as_of_us", BigInteger, nullable=False),
    Column("created_at_us", BigInteger, nullable=False),
    Column("expires_at_us", BigInteger, nullable=False),
    CheckConstraint("page_size > 0", name="ck_od_snapshot_page_size"),
    CheckConstraint("record_count >= 0", name="ck_od_snapshot_record_count"),
    CheckConstraint(
        "(instrument_id IS NULL AND capture_id IS NULL) OR "
        "(instrument_id IS NOT NULL AND capture_id IS NOT NULL)",
        name="ck_od_snapshot_capture_pair",
    ),
    Index("ix_od_snapshot_expiry", "expires_at_us"),
)


@dataclass(frozen=True, slots=True)
class PublishedRecordInput:
    """One complete replacement value for a dataset business key.

    ``business_date`` is a date bucket and cannot be a datetime. The three
    timestamps preserve independent event, source-publication, observation, and
    platform-availability semantics; none is inferred from another. Selector
    metadata is an explicit trusted storage hint and must match the payload.
    Payload fields alone never materialize indexed selector columns.
    """

    business_key: str
    payload: Mapping[str, Any]
    business_date: date | None = None
    observed_at: datetime | None = None
    source_published_at: datetime | None = None
    available_at: datetime | None = None
    event_at: datetime | None = None
    instrument_id: str | None = None
    capture_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """Immutable platform publication metadata and platform publication time."""

    publication_id: str
    dataset_id: str
    source_id: str
    sequence: int
    schema_version: str
    semantic_version: str
    unit: str
    adjustment_basis: str
    quality: Mapping[str, Any]
    provenance: Mapping[str, Any]
    watermark: Mapping[str, Any] | None
    idempotency_key: str
    payload_hash: str
    record_count: int
    published_at: datetime

    @property
    def content_sha256(self) -> str:
        """Expose the canonical full-publication digest in contract terminology."""
        return self.payload_hash


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    """Latest published version for one dataset known to the publication store."""

    dataset_id: str
    publication_id: str
    sequence: int
    published_at: datetime
    schema_version: str
    semantic_version: str
    record_count: int


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    """A fixed publication, optional selector, ordering, scope, and expiry boundary."""

    snapshot_id: str
    dataset_id: str
    publication_id: str
    schema_version: str
    publication_sequence: int
    start_date: date | None
    end_date: date | None
    page_size: int
    access_scope: str
    selection_sha256: str
    content_hash: str
    record_count: int
    created_at: datetime
    expires_at: datetime
    as_of: datetime
    instrument_id: str | None = None
    capture_id: str | None = None

    @property
    def selection_hash(self) -> str:
        """Expose the frozen digest under its short internal name."""
        return self.selection_sha256

    @property
    def content_sha256(self) -> str:
        """Expose the actual scoped row-set digest in contract terminology."""
        return self.content_hash


@dataclass(frozen=True, slots=True)
class PublishedRecord:
    """One visible business-key revision at the snapshot publication boundary."""

    dataset_id: str
    business_key: str
    revision: int
    publication_id: str
    publication_sequence: int
    business_date: date | None
    event_at: datetime | None
    observed_at: datetime | None
    source_published_at: datetime | None
    available_at: datetime | None
    payload: Mapping[str, Any]
    record_hash: str


@dataclass(frozen=True, slots=True)
class CoverageRecordSet:
    """A verified, complete visible row set at one immutable publication pin."""

    dataset_id: str
    source_id: str
    publication_id: str
    publication_sequence: int
    schema_version: str
    semantic_version: str
    unit: str
    adjustment_basis: str
    contract_sha256: str
    algorithm: str
    sha256: str
    record_count: int
    records: tuple[PublishedRecord, ...]


@dataclass(frozen=True, slots=True)
class PublishedPage:
    """Stable page of published records and the next signed cursor, if any."""

    records: tuple[PublishedRecord, ...]
    next_cursor: str | None
    snapshot: SnapshotRecord


@dataclass(frozen=True, slots=True)
class CaptureDiscovery:
    """One capture observed in the exact publication batch."""

    capture_id: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CaptureDiscoveryPage:
    """A bounded page of captures from one exact publication batch."""

    items: tuple[CaptureDiscovery, ...]
    next_cursor: str | None


class PublicationConflictError(ValueError):
    """Raised when an idempotency key is reused with changed publication input."""


class PublicationContractConflictError(ValueError):
    """Raised when a delta changes a dataset's source or data contract."""


class SnapshotError(ValueError):
    """Raised when a snapshot or cursor is malformed or does not match its scope."""


class SnapshotExpiredError(SnapshotError):
    """Raised when a snapshot or cursor has passed its persisted expiry time."""


class PageSizeLimitError(ValueError):
    """Raised instead of returning a partial page that exceeds the response budget."""


class SnapshotSizeLimitError(ValueError):
    """Raised when a fixed snapshot would require hashing too many visible rows."""


class CoverageObservationLimitError(ValueError):
    """Raised instead of returning a truncated publication coverage scan."""


class CoverageRecordIntegrityError(ValueError):
    """Raised when a persisted publication row set fails integrity verification."""


class CoverageSnapshotConsistencyError(ValueError):
    """Raised when the database cannot provide a stable publication read snapshot."""


class CaptureDiscoveryError(ValueError):
    """Raised when a source-native capture discovery request is invalid."""


class CaptureDiscoveryCursorError(CaptureDiscoveryError):
    """Raised when a capture discovery cursor is malformed or out of scope."""


class CaptureDiscoveryLimitError(CaptureDiscoveryError):
    """Raised instead of returning a partial capture set over the row budget."""


class CaptureDiscoveryRecordIntegrityError(CaptureDiscoveryError):
    """Raised when publication capture metadata is missing or inconsistent."""


class CaptureDiscoveryConsistencyError(CaptureDiscoveryError):
    """Raised when the publication and its facts cannot be read atomically."""


def publish_in_connection(
    connection: Connection,
    *,
    dataset_id: str,
    source_id: str,
    schema_version: str,
    semantic_version: str,
    unit: str,
    adjustment_basis: str,
    quality: Mapping[str, Any],
    provenance: Mapping[str, Any],
    idempotency_key: str,
    records: Iterable[PublishedRecordInput],
    watermark: Mapping[str, Any] | None = None,
) -> PublicationRecord:
    """Append a publication, fact revisions, and outbox event atomically.

    Publication input is a delta: supplied business keys are replaced by a new
    revision when their canonical content changed; keys omitted from the batch
    remain unchanged. A new publication's immutable outbox event is inserted in
    the same savepoint. Existing-publication idempotency hits and race replays
    do not create or repair events. The caller's transaction controls
    atomic commit/rollback, allowing a run lease/fence and run success to commit
    with these writes.

    Args:
        connection: Active synchronous SQLAlchemy connection.
        dataset_id: Stable data-domain identifier.
        source_id: Stable source/provider identifier.
        schema_version: Version of the published record schema.
        semantic_version: Version of field meaning and transformations.
        unit: Native or normalized unit declaration.
        adjustment_basis: Explicit adjustment basis, including ``none``.
        quality: Bounded JSON quality summary for this publication.
        provenance: Bounded JSON lineage/source summary without credentials.
        idempotency_key: Caller-owned stable retry key within the dataset.
        records: Complete replacement values for the supplied business keys.
        watermark: Optional bounded watermark advanced atomically with facts.

    Returns:
        The immutable publication metadata. Exact retries return the original.

    Raises:
        PublicationConflictError: A retry key has different canonical input.
        PublicationContractConflictError: Dataset contract metadata changed.
        ValueError: Input is invalid, unbounded, contains credential material, or targets an
            unsupported dialect.
    """
    if not isinstance(connection, Connection):
        raise TypeError("connection must be a synchronous SQLAlchemy Connection")
    _require_supported_connection(connection)
    dataset = _identifier(dataset_id, "dataset_id")
    source = _identifier(source_id, "source_id")
    schema = _label(schema_version, "schema_version", 64)
    semantics = _label(semantic_version, "semantic_version", 64)
    unit_value = _label(unit, "unit", 128)
    adjustment = _label(adjustment_basis, "adjustment_basis", 128)
    request_key = _label(idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH)
    quality_json = _json_object(quality, "quality", MAX_METADATA_BYTES)
    provenance_json = _json_object(provenance, "provenance", MAX_METADATA_BYTES)
    watermark_json = (
        _json_object(watermark, "watermark", MAX_METADATA_BYTES) if watermark is not None else None
    )
    normalized_records = _normalize_records(records)
    payload = {
        "dataset_id": dataset,
        "source_id": source,
        "schema_version": schema,
        "semantic_version": semantics,
        "unit": unit_value,
        "adjustment_basis": adjustment,
        "quality": json.loads(quality_json),
        "provenance": json.loads(provenance_json),
        "watermark": json.loads(watermark_json) if watermark_json is not None else None,
        "records": [record["canonical"] for record in normalized_records],
    }
    payload_hash = _sha256(_canonical_json(payload).encode("utf-8"))
    if connection.dialect.name == "sqlite":
        # pysqlite may defer its physical BEGIN until the first DML statement;
        # a SAVEPOINT issued first can otherwise survive an outer rollback.
        # This no-op DML opens the caller's real transaction before publication.
        connection.exec_driver_sql(
            "UPDATE od_publication_heads SET latest_sequence = latest_sequence WHERE 1 = 0"
        )
    existing = (
        connection.execute(
            select(PUBLICATIONS).where(
                PUBLICATIONS.c.dataset_id == dataset,
                PUBLICATIONS.c.idempotency_key == request_key,
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return _publication_from_row(_ensure_same_payload(existing, payload_hash))

    try:
        # A savepoint lets a uniqueness race on dataset+idempotency_key resolve
        # to the already committed publication without aborting an outer lease
        # transaction. All sequence/fact/head changes roll back with it.
        with connection.begin_nested():
            _ensure_head(connection, dataset)
            # The earlier idempotency lookup may establish a MySQL REPEATABLE READ
            # snapshot. Lock the head as a current read before inspecting its contract.
            head_row = (
                connection.execute(
                    select(PUBLICATION_HEADS)
                    .where(PUBLICATION_HEADS.c.dataset_id == dataset)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            sequence = int(head_row["latest_sequence"])
            latest_publication_id = head_row["latest_publication_id"]
            if latest_publication_id is not None:
                # Keep this a locking/current read too; a plain SELECT can see stale
                # metadata from the transaction's older consistent-read snapshot.
                latest_publication = (
                    connection.execute(
                        select(PUBLICATIONS)
                        .where(PUBLICATIONS.c.publication_id == latest_publication_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one()
                )
                _require_compatible_publication_contract(
                    latest_publication,
                    source_id=source,
                    schema_version=schema,
                    semantic_version=semantics,
                    unit=unit_value,
                    adjustment_basis=adjustment,
                    provenance=json.loads(provenance_json),
                )
            sequence += 1
            now = datetime.now(_UTC)
            now_us = _datetime_to_us(now)
            publication_id = uuid4().hex
            connection.execute(
                update(PUBLICATION_HEADS)
                .where(PUBLICATION_HEADS.c.dataset_id == dataset)
                .values(latest_sequence=sequence, updated_at_us=now_us)
            )
            connection.execute(
                insert(PUBLICATIONS).values(
                    publication_id=publication_id,
                    dataset_id=dataset,
                    source_id=source,
                    sequence=sequence,
                    schema_version=schema,
                    semantic_version=semantics,
                    unit=unit_value,
                    adjustment_basis=adjustment,
                    quality_json=quality_json,
                    provenance_json=provenance_json,
                    watermark_json=watermark_json,
                    idempotency_key=request_key,
                    payload_hash=payload_hash,
                    record_count=len(normalized_records),
                    published_at_us=now_us,
                )
            )
            for record in normalized_records:
                _append_changed_revision(connection, dataset, publication_id, sequence, record)
            connection.execute(
                update(PUBLICATION_HEADS)
                .where(PUBLICATION_HEADS.c.dataset_id == dataset)
                .values(
                    latest_publication_id=publication_id,
                    watermark_json=watermark_json,
                    updated_at_us=now_us,
                )
            )
            persisted_publication = (
                connection.execute(
                    select(PUBLICATIONS)
                    .where(PUBLICATIONS.c.publication_id == publication_id)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            event_candidate = _candidate_from_publication_row(persisted_publication)
            if (
                event_candidate.publication_id != publication_id
                or event_candidate.dataset_id != dataset
                or event_candidate.sequence != sequence
                or event_candidate.record_count != len(normalized_records)
                or event_candidate.payload_sha256 != payload_hash
                or event_candidate.schema_version != schema
                or event_candidate.published_at_us != now_us
            ):
                raise _PublicationOutboxSourceError(
                    "new publication row changed before its outbox event was frozen"
                )
            event = _build_publication_event(event_candidate)
            try:
                with connection.begin_nested():
                    connection.execute(
                        insert(PUBLICATION_OUTBOX_EVENTS).values(**_event_values(event))
                    )
            except IntegrityError:
                raced_event = _find_existing_event(connection, PUBLICATION_OUTBOX_EVENTS, event)
                if raced_event is None:
                    raise
                _require_exact_event_replay(raced_event, event)
    except IntegrityError:
        raced = (
            connection.execute(
                select(PUBLICATIONS)
                .where(
                    PUBLICATIONS.c.dataset_id == dataset,
                    PUBLICATIONS.c.idempotency_key == request_key,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if raced is None:
            raise
        return _publication_from_row(_ensure_same_payload(raced, payload_hash))

    row = (
        connection.execute(
            select(PUBLICATIONS).where(PUBLICATIONS.c.publication_id == publication_id)
        )
        .mappings()
        .one()
    )
    return _publication_from_row(row)


class PublicationStore:
    """Read and publish immutable versions through an explicit SQLAlchemy engine."""

    def __init__(self, engine: Engine, *, cursor_signing_key: bytes) -> None:
        """Bind an engine and mandatory 256-bit-or-stronger cursor key.

        Args:
            engine: Synchronous SQLAlchemy engine whose schema was migrated.
            cursor_signing_key: Secret key supplied by the deployment environment.

        Raises:
            TypeError: The engine or key type is invalid.
            ValueError: The key is shorter than 32 bytes.
        """
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if not isinstance(cursor_signing_key, bytes):
            raise TypeError("cursor_signing_key must be bytes")
        if len(cursor_signing_key) < 32:
            raise ValueError("cursor_signing_key must contain at least 32 bytes")
        self._engine = engine
        self._cursor_signing_key = bytes(cursor_signing_key)

    def publish(
        self,
        *,
        dataset_id: str,
        source_id: str,
        schema_version: str,
        semantic_version: str,
        unit: str,
        adjustment_basis: str,
        quality: Mapping[str, Any],
        provenance: Mapping[str, Any],
        idempotency_key: str,
        records: Iterable[PublishedRecordInput],
        watermark: Mapping[str, Any] | None = None,
    ) -> PublicationRecord:
        """Publish a delta and watermark in one engine-owned transaction."""
        with self._engine.connect() as connection:
            transaction = connection.begin()
            if self._engine.dialect.name == "sqlite":
                # Reserve the SQLite writer before reading the head so separate
                # engine instances cannot both advance one dataset sequence.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                publication = publish_in_connection(
                    connection,
                    dataset_id=dataset_id,
                    source_id=source_id,
                    schema_version=schema_version,
                    semantic_version=semantic_version,
                    unit=unit,
                    adjustment_basis=adjustment_basis,
                    quality=quality,
                    provenance=provenance,
                    idempotency_key=idempotency_key,
                    records=records,
                    watermark=watermark,
                )
            except Exception:
                transaction.rollback()
                raise
            else:
                transaction.commit()
                return publication

    def list_capture_discovery(
        self,
        dataset_id: str,
        publication_id: str,
        *,
        expected_publication: PublicationRecord,
        expected_contract_sha256: str,
        instrument_id: str,
        page_size: int = 100,
        cursor: str | None = None,
        access_scope: str = "default",
    ) -> CaptureDiscoveryPage:
        """List captures written by one exact publication batch for one instrument.

        This deliberately does not search earlier publication sequences and
        must not be interpreted as a latest or full-history capture index. A
        missing instrument in this batch returns an empty page; it does not
        establish that the instrument has never had data. Results are deduped
        by capture ID and ordered by source ``observed_at`` descending, then
        capture ID ascending. Every row for a capture must carry the same
        canonical UTC observation timestamp. This metadata describes retrieval
        time and makes no point-in-time availability claim. The expected
        publication and resolved contract fingerprint are rechecked inside the
        same stable read transaction as the fact query, closing the gap between
        API authorization/contract resolution and these rows.

        The publication and returned-row cap is
        ``MAX_CAPTURE_DISCOVERY_ROWS``; exceeding it fails rather than
        returning a truncated result. The store verifies the exact 0017 index
        shape before querying and forces its five-column equality prefix on
        MySQL. The returned-row cap is not a physical I/O bound: metadata
        checks and a selected index do not bound pages touched, MVCC visibility
        work, or engine-level reads. Treat this as an experimental SQL slice;
        the API remains disabled until a separate MySQL query-plan acceptance
        validates the deployed plan.

        Args:
            dataset_id: Dataset containing the pinned publication.
            publication_id: Exact immutable publication identifier.
            expected_publication: Publication metadata already resolved and
                authorized by the API request, which must still match this
                transaction's publication row exactly.
            expected_contract_sha256: Canonical fingerprint of the reviewed
                contract resolved by the API request and pinned in provenance.
            instrument_id: One source instrument in the publication batch.
            page_size: Number of capture summaries to return, at most
                ``MAX_PAGE_SIZE``.
            cursor: Optional HMAC-authenticated continuation token.
            access_scope: Caller authorization scope bound into the cursor.

        Returns:
            A bounded capture page from the exact revalidated publication.

        Raises:
            CaptureDiscoveryCursorError: If the cursor is malformed, expired,
                or bound to another publication, instrument, page size, or
                access scope.
            CaptureDiscoveryLimitError: If the publication or rows exceed the
                discovery work budget.
            CaptureDiscoveryRecordIntegrityError: If source capture metadata
                is missing, malformed, or inconsistent.
            CaptureDiscoveryConsistencyError: If the database cannot provide
                one stable read transaction for publication metadata and rows.
            PublicationContractConflictError: If publication metadata or its
                pinned contract fingerprint changed after API preflight.
            ValueError: If an input identifier or page bound is invalid.
        """
        dataset = _identifier(dataset_id, "dataset_id")
        publication_key = _hex_identifier(publication_id, "publication_id")
        contract_hash = _sha256_value(expected_contract_sha256, "expected_contract_sha256")
        instrument = _validate_capture_discovery_instrument(instrument_id)
        page = _page_limit(page_size, "page_size", max_value=MAX_PAGE_SIZE)
        scope = _label(access_scope, "access_scope", MAX_ID_LENGTH)
        if not isinstance(expected_publication, PublicationRecord):
            raise TypeError("expected_publication must be a PublicationRecord")
        if (
            expected_publication.dataset_id != dataset
            or expected_publication.publication_id != publication_key
            or expected_publication.provenance.get("contract_sha256") != contract_hash
        ):
            raise PublicationContractConflictError(
                "capture discovery expected publication pin is inconsistent"
            )

        with self._engine.connect() as connection:
            try:
                transaction = _begin_capture_discovery_read_transaction(connection)
            except CaptureDiscoveryConsistencyError:
                raise
            try:
                if connection.dialect.name == "mysql":
                    _require_capture_discovery_transactional_tables(connection)
                _require_capture_discovery_index(connection)
                publication_row = (
                    connection.execute(
                        select(PUBLICATIONS).where(
                            PUBLICATIONS.c.dataset_id == dataset,
                            PUBLICATIONS.c.publication_id == publication_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if publication_row is None:
                    raise PublicationContractConflictError(
                        "capture discovery publication disappeared after preflight"
                    )

                persisted_publication = _publication_from_row(publication_row)
                if (
                    persisted_publication != expected_publication
                    or persisted_publication.provenance.get("contract_sha256") != contract_hash
                ):
                    raise PublicationContractConflictError(
                        "capture discovery publication contract changed after preflight"
                    )

                sequence = persisted_publication.sequence
                publication_count = persisted_publication.record_count
                if type(sequence) is not int or sequence < 1:
                    raise CaptureDiscoveryRecordIntegrityError(
                        "stored publication sequence is invalid"
                    )
                if type(publication_count) is not int or publication_count < 0:
                    raise CaptureDiscoveryRecordIntegrityError(
                        "stored publication row count is invalid"
                    )
                if publication_count > MAX_CAPTURE_DISCOVERY_ROWS:
                    raise CaptureDiscoveryLimitError(
                        "publication exceeds the capture discovery row budget"
                    )

                cursor_position = (
                    self._decode_capture_discovery_cursor(
                        cursor,
                        dataset=dataset,
                        publication_key=publication_key,
                        sequence=sequence,
                        instrument=instrument,
                        page_size=page,
                        access_scope=scope,
                    )
                    if cursor is not None
                    else None
                )
                statement = _capture_discovery_fact_statement(
                    dataset=dataset,
                    publication_key=publication_key,
                    sequence=sequence,
                    instrument=instrument,
                    dialect_name=connection.dialect.name,
                )
                rows = connection.execute(statement).mappings().all()
                if len(rows) > MAX_CAPTURE_DISCOVERY_ROWS:
                    raise CaptureDiscoveryLimitError(
                        "publication capture rows exceed the discovery row budget"
                    )
                if len(rows) > publication_count:
                    raise CaptureDiscoveryRecordIntegrityError(
                        "capture rows exceed the stored publication row count"
                    )

                capture_times: dict[str, datetime] = {}
                for row in rows:
                    capture_id = row["capture_id"]
                    try:
                        selector = validate_selector_pair(
                            instrument_id=instrument,
                            capture_id=capture_id,
                        )
                    except InstrumentCaptureSelectionError as exc:
                        raise CaptureDiscoveryRecordIntegrityError(
                            "stored capture selector is invalid"
                        ) from exc
                    if selector is None:
                        raise CaptureDiscoveryRecordIntegrityError(
                            "stored capture selector is missing"
                        )
                    observed_at = _capture_discovery_observed_at(row["observed_at"])
                    previous = capture_times.get(selector.capture_id)
                    if previous is not None and previous != observed_at:
                        raise CaptureDiscoveryRecordIntegrityError(
                            "one capture has inconsistent observation timestamps"
                        )
                    capture_times[selector.capture_id] = observed_at

                ordered = sorted(
                    (
                        CaptureDiscovery(capture_id=capture_id, observed_at=observed_at)
                        for capture_id, observed_at in capture_times.items()
                    ),
                    key=lambda item: (-_datetime_to_us(item.observed_at), item.capture_id),
                )
                if cursor_position is not None:
                    last_observed_at, last_capture_id = cursor_position
                    ordered = [
                        item
                        for item in ordered
                        if item.observed_at < last_observed_at
                        or (
                            item.observed_at == last_observed_at
                            and item.capture_id > last_capture_id
                        )
                    ]

                has_more = len(ordered) > page
                items = tuple(ordered[:page])
                next_cursor = (
                    self._encode_capture_discovery_cursor(
                        dataset=dataset,
                        publication_key=publication_key,
                        sequence=sequence,
                        instrument=instrument,
                        page_size=page,
                        access_scope=scope,
                        last=items[-1],
                    )
                    if has_more and items
                    else None
                )
            except (
                CaptureDiscoveryCursorError,
                CaptureDiscoveryLimitError,
                CaptureDiscoveryRecordIntegrityError,
                CaptureDiscoveryConsistencyError,
                PublicationContractConflictError,
            ):
                _rollback_coverage_read(transaction)
                raise
            except Exception as exc:
                _rollback_coverage_read(transaction)
                if isinstance(exc, SQLAlchemyError):
                    raise CaptureDiscoveryConsistencyError(
                        "capture publication and rows could not be read consistently"
                    ) from exc
                raise CaptureDiscoveryRecordIntegrityError(
                    "capture publication metadata could not be verified"
                ) from exc
            try:
                transaction.commit()
            except Exception as exc:
                if isinstance(exc, SQLAlchemyError):
                    raise CaptureDiscoveryConsistencyError(
                        "capture publication read could not commit consistently"
                    ) from exc
                raise
        return CaptureDiscoveryPage(items=items, next_cursor=next_cursor)

    def list_datasets(self, *, limit: int = 1_000) -> tuple[DatasetSummary, ...]:
        """Return current published dataset heads, excluding empty heads."""
        _page_limit(limit, "limit", max_value=10_000)
        statement = (
            select(PUBLICATIONS)
            .join(
                PUBLICATION_HEADS,
                and_(
                    PUBLICATION_HEADS.c.dataset_id == PUBLICATIONS.c.dataset_id,
                    PUBLICATION_HEADS.c.latest_publication_id == PUBLICATIONS.c.publication_id,
                ),
            )
            .order_by(PUBLICATIONS.c.dataset_id)
            .limit(limit)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(
            DatasetSummary(
                dataset_id=row["dataset_id"],
                publication_id=row["publication_id"],
                sequence=row["sequence"],
                published_at=_us_to_datetime(row["published_at_us"]),
                schema_version=row["schema_version"],
                semantic_version=row["semantic_version"],
                record_count=row["record_count"],
            )
            for row in rows
        )

    def list_publications(
        self, dataset_id: str, *, limit: int = 100, before_sequence: int | None = None
    ) -> tuple[PublicationRecord, ...]:
        """List immutable publication metadata newest first for one dataset."""
        dataset = _identifier(dataset_id, "dataset_id")
        _page_limit(limit, "limit", max_value=1_000)
        statement = select(PUBLICATIONS).where(PUBLICATIONS.c.dataset_id == dataset)
        if before_sequence is not None:
            if type(before_sequence) is not int or before_sequence < 1:
                raise ValueError("before_sequence must be a positive integer")
            statement = statement.where(PUBLICATIONS.c.sequence < before_sequence)
        statement = statement.order_by(PUBLICATIONS.c.sequence.desc()).limit(limit)
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_publication_from_row(row) for row in rows)

    def get_publication(self, publication_id: str) -> PublicationRecord | None:
        """Return immutable publication metadata by identifier, if it exists."""
        identifier = _hex_identifier(publication_id, "publication_id")
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(PUBLICATIONS).where(PUBLICATIONS.c.publication_id == identifier)
                )
                .mappings()
                .first()
            )
        return _publication_from_row(row) if row is not None else None

    def read_coverage_records(
        self,
        dataset_id: str,
        publication_id: str,
        *,
        limit: int = MAX_COVERAGE_RECORDS,
    ) -> tuple[PublishedRecord, ...] | None:
        """Read every visible fact at an exact publication pin for coverage.

        The query resolves the latest business-key revision at the pinned
        publication sequence, excludes tombstones only after ranking, and
        includes null business dates so callers can reject malformed time
        series. It never creates a persisted or in-memory snapshot and never
        returns a truncated scan.
        """
        dataset = _identifier(dataset_id, "dataset_id")
        publication_key = _hex_identifier(publication_id, "publication_id")
        row_limit = _page_limit(limit, "limit", max_value=MAX_COVERAGE_RECORDS)
        with self._engine.connect() as connection:
            publication_row = (
                connection.execute(
                    select(PUBLICATIONS).where(
                        PUBLICATIONS.c.dataset_id == dataset,
                        PUBLICATIONS.c.publication_id == publication_key,
                    )
                )
                .mappings()
                .first()
            )
            if publication_row is None:
                return None
            ranked = _ranked_visible_facts_for(
                dataset,
                publication_row["sequence"],
            )
            statement = (
                select(ranked)
                .where(ranked.c.row_number == 1, ranked.c.is_deleted.is_(False))
                .order_by(
                    case((ranked.c.business_date.is_(None), 1), else_=0),
                    ranked.c.business_date,
                    ranked.c.business_key,
                )
                .limit(row_limit + 1)
            )
            result = connection.execute(
                statement.execution_options(stream_results=True, yield_per=_READ_YIELD_PER)
            ).mappings()
            rows: list[RowMapping] = []
            total_bytes = 0
            try:
                for item in result:
                    if len(rows) >= row_limit:
                        raise CoverageObservationLimitError(
                            f"coverage scan exceeds the {row_limit:,}-record limit"
                        )
                    total_bytes += len(item["business_key"].encode("utf-8"))
                    total_bytes += len(item["record_json"].encode("utf-8"))
                    if total_bytes > MAX_COVERAGE_SCAN_BYTES:
                        raise CoverageObservationLimitError(
                            "coverage scan exceeds the 64 MiB byte limit"
                        )
                    rows.append(item)
            finally:
                result.close()
        return tuple(_published_record_from_row(item) for item in rows)

    def read_coverage_record_set(
        self,
        dataset_id: str,
        publication_id: str,
        *,
        contract_sha256: str,
        limit: int = MAX_COVERAGE_RECORDS,
    ) -> CoverageRecordSet | None:
        """Verify and read the full visible row set at one stable publication pin.

        The returned digest is a versioned, externally pinnable description of
        the complete visible set. This method never creates or updates a pin:
        callers must compare the result with an independently trusted pin.
        Publication metadata and fact rows are read in one repeatable snapshot;
        each stored row hash is recomputed with the publication normalizer
        before it contributes to the full-set digest.
        """
        dataset = _identifier(dataset_id, "dataset_id")
        publication_key = _hex_identifier(publication_id, "publication_id")
        contract_hash = _sha256_value(contract_sha256, "contract_sha256")
        row_limit = _page_limit(limit, "limit", max_value=MAX_COVERAGE_RECORDS)

        with self._engine.connect() as connection:
            transaction = _begin_coverage_read_transaction(connection)
            try:
                if connection.dialect.name == "mysql":
                    _require_transactional_coverage_tables(connection)
                publication_row = (
                    connection.execute(
                        select(PUBLICATIONS).where(
                            PUBLICATIONS.c.dataset_id == dataset,
                            PUBLICATIONS.c.publication_id == publication_key,
                        )
                    )
                    .mappings()
                    .first()
                )
                if publication_row is None:
                    coverage_set = None
                else:
                    coverage_set = self._read_verified_coverage_set_in_transaction(
                        connection,
                        publication_row,
                        dataset=dataset,
                        publication_key=publication_key,
                        contract_hash=contract_hash,
                        row_limit=row_limit,
                    )
            except (CoverageObservationLimitError, CoverageRecordIntegrityError):
                _rollback_coverage_read(transaction)
                raise
            except CoverageSnapshotConsistencyError:
                _rollback_coverage_read(transaction)
                raise
            except Exception as exc:
                _rollback_coverage_read(transaction)
                if isinstance(exc, SQLAlchemyError):
                    raise CoverageSnapshotConsistencyError(
                        "publication rows could not be read in one stable database snapshot"
                    ) from exc
                raise CoverageRecordIntegrityError(
                    "publication metadata or visible records could not be verified"
                ) from exc
            try:
                transaction.commit()
            except Exception as exc:
                if isinstance(exc, SQLAlchemyError):
                    raise CoverageSnapshotConsistencyError(
                        "publication snapshot could not be committed consistently"
                    ) from exc
                raise
            return coverage_set

    def _read_verified_coverage_set_in_transaction(
        self,
        connection: Connection,
        publication_row: RowMapping,
        *,
        dataset: str,
        publication_key: str,
        contract_hash: str,
        row_limit: int,
    ) -> CoverageRecordSet:
        """Verify the selected publication and visible rows inside its read txn."""
        try:
            for name in ("quality_json", "provenance_json", "watermark_json"):
                value = publication_row[name]
                if value is not None:
                    if not isinstance(value, str) or len(value) > MAX_METADATA_BYTES:
                        raise CoverageRecordIntegrityError(
                            f"stored publication {name} exceeds its metadata bound"
                        )
                    if len(value.encode("utf-8")) > MAX_METADATA_BYTES:
                        raise CoverageRecordIntegrityError(
                            f"stored publication {name} exceeds its metadata byte bound"
                        )
            publication = _publication_from_row(publication_row)
            if (
                publication.dataset_id != dataset
                or publication.publication_id != publication_key
                or type(publication.sequence) is not int
                or publication.sequence < 1
            ):
                raise CoverageRecordIntegrityError("publication identity is inconsistent")
            _identifier(publication.source_id, "publication.source_id")
            _label(publication.schema_version, "publication.schema_version", 64)
            _label(publication.semantic_version, "publication.semantic_version", 64)
            _label(publication.unit, "publication.unit", 128)
            _label(publication.adjustment_basis, "publication.adjustment_basis", 128)
            if not isinstance(publication.provenance, Mapping):
                raise CoverageRecordIntegrityError("publication provenance is not an object")
            if (
                _json_object(
                    publication.provenance,
                    "stored publication provenance",
                    MAX_METADATA_BYTES,
                )
                != publication_row["provenance_json"]
            ):
                raise CoverageRecordIntegrityError(
                    "stored publication provenance is not canonically encoded"
                )
            stored_contract_hash = publication.provenance.get("contract_sha256")
            if (
                not isinstance(stored_contract_hash, str)
                or _sha256_value(stored_contract_hash, "publication.contract_sha256")
                != contract_hash
            ):
                raise CoverageRecordIntegrityError(
                    "publication contract hash does not match the independent pin"
                )

            key_collation = "C" if connection.dialect.name == "postgresql" else None
            ranked = _ranked_visible_facts_for(
                dataset,
                publication.sequence,
                business_key_collation=key_collation,
            )
            fact_publication = PUBLICATIONS.alias("od_coverage_fact_publication")
            statement = (
                select(
                    ranked,
                    fact_publication.c.publication_id.label("fact_publication_id"),
                    fact_publication.c.dataset_id.label("fact_publication_dataset_id"),
                    fact_publication.c.sequence.label("fact_publication_sequence"),
                    fact_publication.c.source_id.label("fact_publication_source_id"),
                    fact_publication.c.schema_version.label("fact_publication_schema_version"),
                    fact_publication.c.semantic_version.label("fact_publication_semantic_version"),
                    fact_publication.c.unit.label("fact_publication_unit"),
                    fact_publication.c.adjustment_basis.label("fact_publication_adjustment_basis"),
                    fact_publication.c.provenance_json.label("fact_publication_provenance_json"),
                )
                .select_from(
                    ranked.outerjoin(
                        fact_publication,
                        fact_publication.c.publication_id == ranked.c.publication_id,
                    )
                )
                .where(ranked.c.row_number == 1, ranked.c.is_deleted.is_(False))
                .order_by(
                    case((ranked.c.business_date.is_(None), 1), else_=0),
                    ranked.c.business_date,
                    ranked.c.business_key,
                )
                .limit(row_limit + 1)
            )
            result = connection.execute(
                statement.execution_options(stream_results=True, yield_per=_READ_YIELD_PER)
            ).mappings()
            records: list[PublishedRecord] = []
            total_bytes = 0
            try:
                for row in result:
                    if len(records) >= row_limit:
                        raise CoverageObservationLimitError(
                            f"coverage scan exceeds the {row_limit:,}-record limit"
                        )
                    row_bytes = _coverage_record_storage_size(row)
                    total_bytes += row_bytes
                    if total_bytes > MAX_COVERAGE_SCAN_BYTES:
                        raise CoverageObservationLimitError(
                            "coverage scan exceeds the 64 MiB byte limit"
                        )
                    records.append(
                        _verified_coverage_record(
                            row,
                            dataset=dataset,
                            max_sequence=publication.sequence,
                            publication=publication,
                        )
                    )
            finally:
                result.close()

            records.sort(key=_coverage_record_sort_key)
            for previous, current in zip(records, records[1:], strict=False):
                if previous.business_key == current.business_key:
                    raise CoverageRecordIntegrityError(
                        "visible publication contains duplicate business keys"
                    )
            row_count = len(records)
            digest = _visible_record_set_sha256(publication, contract_hash, records)
            return CoverageRecordSet(
                dataset_id=publication.dataset_id,
                source_id=publication.source_id,
                publication_id=publication.publication_id,
                publication_sequence=publication.sequence,
                schema_version=publication.schema_version,
                semantic_version=publication.semantic_version,
                unit=publication.unit,
                adjustment_basis=publication.adjustment_basis,
                contract_sha256=contract_hash,
                algorithm=VISIBLE_RECORD_SET_ALGORITHM,
                sha256=digest,
                record_count=row_count,
                records=tuple(records),
            )
        except (CoverageObservationLimitError, CoverageRecordIntegrityError):
            raise
        except SQLAlchemyError:
            raise
        except Exception as exc:
            raise CoverageRecordIntegrityError(
                "publication metadata or a visible record is malformed"
            ) from exc

    def create_snapshot(
        self,
        dataset_id: str,
        *,
        publication_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        page_size: int = 100,
        access_scope: str = "default",
        ttl: timedelta = timedelta(hours=24),
        instrument_id: str | None = None,
        capture_id: str | None = None,
    ) -> SnapshotRecord:
        """Pin a publication and compute a content hash for its scoped subset.

        The date window is half-open: ``start_date <= business_date < end_date``.
        Snapshot content hashing streams ordered key/revision/row-hash tuples,
        so the hash covers the actual subset rather than publication metadata.
        """
        dataset = _identifier(dataset_id, "dataset_id")
        selection = _validated_capture_selection(
            instrument_id, capture_id, error_type=SnapshotError
        )
        start, end = _validate_date_window(start_date, end_date)
        page = _page_limit(page_size, "page_size", max_value=MAX_PAGE_SIZE)
        scope = _label(access_scope, "access_scope", MAX_ID_LENGTH)
        ttl_us = _ttl_to_us(ttl)
        snapshot_id = uuid4().hex
        now_us = _datetime_to_us(datetime.now(_UTC))
        expires_us = now_us + ttl_us
        with self._engine.begin() as connection:
            if publication_id is None:
                row = (
                    connection.execute(
                        select(PUBLICATIONS)
                        .join(
                            PUBLICATION_HEADS,
                            and_(
                                PUBLICATION_HEADS.c.dataset_id == PUBLICATIONS.c.dataset_id,
                                PUBLICATION_HEADS.c.latest_publication_id
                                == PUBLICATIONS.c.publication_id,
                            ),
                        )
                        .where(PUBLICATIONS.c.dataset_id == dataset)
                    )
                    .mappings()
                    .first()
                )
            else:
                publication_key = _hex_identifier(publication_id, "publication_id")
                row = (
                    connection.execute(
                        select(PUBLICATIONS).where(
                            PUBLICATIONS.c.dataset_id == dataset,
                            PUBLICATIONS.c.publication_id == publication_key,
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise SnapshotError("dataset has no matching published version")
            publication = _publication_from_row(row)
            digest, record_count = _snapshot_content_hash(
                connection,
                dataset,
                publication,
                start,
                end,
                scope,
                page,
                instrument_id=selection.instrument_id if selection is not None else None,
                capture_id=selection.capture_id if selection is not None else None,
            )
            selection_hash = _snapshot_selection_hash(
                dataset,
                publication,
                start,
                end,
                scope,
                page,
                instrument_id=selection.instrument_id if selection is not None else None,
                capture_id=selection.capture_id if selection is not None else None,
            )
            connection.execute(
                insert(SNAPSHOTS).values(
                    snapshot_id=snapshot_id,
                    dataset_id=dataset,
                    publication_id=publication.publication_id,
                    schema_version=publication.schema_version,
                    publication_sequence=publication.sequence,
                    instrument_id=selection.instrument_id if selection is not None else None,
                    capture_id=selection.capture_id if selection is not None else None,
                    start_date=start,
                    end_date=end,
                    page_size=page,
                    access_scope=scope,
                    selection_sha256=selection_hash,
                    content_hash=digest,
                    record_count=record_count,
                    as_of_us=_datetime_to_us(publication.published_at),
                    created_at_us=now_us,
                    expires_at_us=expires_us,
                )
            )
            snapshot_row = (
                connection.execute(select(SNAPSHOTS).where(SNAPSHOTS.c.snapshot_id == snapshot_id))
                .mappings()
                .one()
            )
        return _snapshot_from_row(snapshot_row)

    def get_snapshot(
        self, snapshot_id: str, *, access_scope: str = "default"
    ) -> SnapshotRecord | None:
        """Read a snapshot within its access scope, raising when it has expired."""
        snapshot_key = _hex_identifier(snapshot_id, "snapshot_id")
        scope = _label(access_scope, "access_scope", MAX_ID_LENGTH)
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(SNAPSHOTS).where(
                        SNAPSHOTS.c.snapshot_id == snapshot_key,
                        SNAPSHOTS.c.access_scope == scope,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        snapshot = _snapshot_from_row(row)
        _require_unexpired(snapshot)
        return snapshot

    def read_page(
        self,
        snapshot_id: str,
        *,
        cursor: str | None = None,
        access_scope: str = "default",
    ) -> PublishedPage:
        """Read one stable page from published rows at the snapshot boundary."""
        snapshot = self.get_snapshot(snapshot_id, access_scope=access_scope)
        if snapshot is None:
            raise SnapshotError("snapshot does not exist in the requested scope")
        cursor_position: tuple[date | None, str] | None = None
        if cursor is not None:
            cursor_position = self._decode_cursor(cursor, snapshot)
        ranked = _ranked_visible_facts(snapshot)
        statement = select(ranked).where(ranked.c.row_number == 1, ranked.c.is_deleted.is_(False))
        if snapshot.start_date is not None:
            statement = statement.where(ranked.c.business_date >= snapshot.start_date)
        if snapshot.end_date is not None:
            statement = statement.where(ranked.c.business_date < snapshot.end_date)
        if cursor_position is not None:
            last_date, last_key = cursor_position
            if last_date is None:
                after = and_(ranked.c.business_date.is_(None), ranked.c.business_key > last_key)
            else:
                after = or_(
                    ranked.c.business_date > last_date,
                    and_(ranked.c.business_date == last_date, ranked.c.business_key > last_key),
                    ranked.c.business_date.is_(None),
                )
            statement = statement.where(after)
        nulls_last = case((ranked.c.business_date.is_(None), 1), else_=0)
        statement = statement.order_by(
            nulls_last,
            ranked.c.business_date,
            ranked.c.business_key,
        ).limit(snapshot.page_size + 1)
        records_list: list[PublishedRecord] = []
        response_bytes = _PAGE_ENVELOPE_BYTES
        has_more = False
        with self._engine.connect() as connection:
            result = connection.execute(
                statement.execution_options(stream_results=True, yield_per=_READ_YIELD_PER)
            ).mappings()
            try:
                for row in result:
                    if len(records_list) >= snapshot.page_size:
                        has_more = True
                        break
                    response_bytes += _published_record_response_size(row)
                    if response_bytes > MAX_PAGE_BYTES:
                        raise PageSizeLimitError(
                            f"page exceeds the {MAX_PAGE_BYTES:,}-byte response limit"
                        )
                    records_list.append(_published_record_from_row(row))
            finally:
                result.close()
        records = tuple(records_list)
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = self._encode_cursor(snapshot, last.business_date, last.business_key)
        return PublishedPage(records=records, next_cursor=next_cursor, snapshot=snapshot)

    def _encode_capture_discovery_cursor(
        self,
        *,
        dataset: str,
        publication_key: str,
        sequence: int,
        instrument: str,
        page_size: int,
        access_scope: str,
        last: CaptureDiscovery,
    ) -> str:
        """Sign one capture-discovery continuation and all request bindings."""
        expires_at_us = _datetime_to_us(datetime.now(_UTC) + CAPTURE_DISCOVERY_CURSOR_TTL)
        payload = {
            "version": 1,
            "dataset_id": dataset,
            "publication_id": publication_key,
            "publication_sequence": sequence,
            "instrument_id": instrument,
            "page_size": page_size,
            "access_scope": access_scope,
            "observed_at": _timestamp(last.observed_at, "cursor.observed_at"),
            "capture_id": last.capture_id,
            "expires_at_us": expires_at_us,
        }
        encoded = _base64url(_canonical_json(payload).encode("utf-8"))
        signature = _base64url(
            hmac.new(self._cursor_signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        token = f"{encoded}.{signature}"
        if len(token) > MAX_CURSOR_BYTES:
            raise CaptureDiscoveryCursorError("capture cursor exceeds its size limit")
        return token

    def _decode_capture_discovery_cursor(
        self,
        token: str,
        *,
        dataset: str,
        publication_key: str,
        sequence: int,
        instrument: str,
        page_size: int,
        access_scope: str,
    ) -> tuple[datetime, str]:
        """Verify a signed cursor and return its stable keyset position."""
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_BYTES:
            raise CaptureDiscoveryCursorError("capture cursor is malformed or too large")
        pieces = token.split(".")
        if len(pieces) != 2:
            raise CaptureDiscoveryCursorError("capture cursor is malformed")
        encoded, signature = pieces
        base64url_characters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if (
            not encoded
            or len(signature) != 43
            or any(char not in base64url_characters for char in encoded)
            or any(char not in base64url_characters for char in signature)
        ):
            raise CaptureDiscoveryCursorError("capture cursor encoding is invalid")
        expected = _base64url(
            hmac.new(self._cursor_signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise CaptureDiscoveryCursorError("capture cursor signature is invalid")
        try:
            raw = _base64url_decode(encoded)
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CaptureDiscoveryCursorError("capture cursor payload is invalid") from exc
        if not isinstance(payload, dict):
            raise CaptureDiscoveryCursorError("capture cursor payload is invalid")
        expected_binding = {
            "version": 1,
            "dataset_id": dataset,
            "publication_id": publication_key,
            "publication_sequence": sequence,
            "instrument_id": instrument,
            "page_size": page_size,
            "access_scope": access_scope,
        }
        if any(payload.get(key) != value for key, value in expected_binding.items()):
            raise CaptureDiscoveryCursorError("capture cursor belongs to another request scope")
        try:
            capture = validate_selector_pair(
                instrument_id=instrument,
                capture_id=payload.get("capture_id"),
            )
            observed_at = _capture_discovery_observed_at(payload.get("observed_at"))
        except (InstrumentCaptureSelectionError, CaptureDiscoveryRecordIntegrityError) as exc:
            raise CaptureDiscoveryCursorError("capture cursor position is invalid") from exc
        if capture is None:
            raise CaptureDiscoveryCursorError("capture cursor position is invalid")
        expires_at_us = payload.get("expires_at_us")
        if type(expires_at_us) is not int or expires_at_us <= 0:
            raise CaptureDiscoveryCursorError("capture cursor expiry is invalid")
        if _datetime_to_us(datetime.now(_UTC)) >= expires_at_us:
            raise CaptureDiscoveryCursorError("capture cursor has expired")
        return observed_at, capture.capture_id

    def _encode_cursor(
        self, snapshot: SnapshotRecord, last_date: date | None, last_key: str
    ) -> str:
        """Create an authenticated continuation token for one snapshot page."""
        selection = _validated_capture_selection(
            snapshot.instrument_id,
            snapshot.capture_id,
            error_type=SnapshotError,
        )
        payload = {
            "version": 2 if selection is not None else 1,
            "snapshot_id": snapshot.snapshot_id,
            "dataset_id": snapshot.dataset_id,
            "publication_id": snapshot.publication_id,
            "schema_version": snapshot.schema_version,
            "publication_sequence": snapshot.publication_sequence,
            "start_date": snapshot.start_date.isoformat() if snapshot.start_date else None,
            "end_date": snapshot.end_date.isoformat() if snapshot.end_date else None,
            "page_size": snapshot.page_size,
            "access_scope": snapshot.access_scope,
            "selection_sha256": snapshot.selection_sha256,
            "as_of_us": _datetime_to_us(snapshot.as_of),
            "expires_at_us": _datetime_to_us(snapshot.expires_at),
            "sort": "business_date_asc_nulls_last_business_key_asc",
            "last_business_date": last_date.isoformat() if last_date else None,
            "last_business_key": last_key,
        }
        if selection is not None:
            payload["instrument_id"] = selection.instrument_id
            payload["capture_id"] = selection.capture_id
        encoded = _base64url(_canonical_json(payload).encode("utf-8"))
        signature = _base64url(
            hmac.new(self._cursor_signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        token = f"{encoded}.{signature}"
        if len(token) > MAX_CURSOR_BYTES:
            raise SnapshotError("cursor exceeds its size limit")
        return token

    def _decode_cursor(self, token: str, snapshot: SnapshotRecord) -> tuple[date | None, str]:
        """Verify cursor authentication, bindings, expiry, and continuation key."""
        if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_BYTES:
            raise SnapshotError("cursor is malformed or exceeds its size limit")
        pieces = token.split(".")
        if len(pieces) != 2:
            raise SnapshotError("cursor is malformed")
        encoded, signature = pieces
        base64url_characters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if (
            not encoded
            or len(signature) != 43
            or any(char not in base64url_characters for char in encoded)
            or any(char not in base64url_characters for char in signature)
        ):
            raise SnapshotError("cursor encoding is invalid")
        expected = _base64url(
            hmac.new(self._cursor_signing_key, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise SnapshotError("cursor signature is invalid")
        try:
            raw = _base64url_decode(encoded)
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError("cursor payload is invalid") from exc
        if not isinstance(payload, dict):
            raise SnapshotError("cursor payload is invalid")
        selection = _validated_capture_selection(
            snapshot.instrument_id,
            snapshot.capture_id,
            error_type=SnapshotError,
        )
        expected_binding = {
            "version": 2 if selection is not None else 1,
            "snapshot_id": snapshot.snapshot_id,
            "dataset_id": snapshot.dataset_id,
            "publication_id": snapshot.publication_id,
            "schema_version": snapshot.schema_version,
            "publication_sequence": snapshot.publication_sequence,
            "start_date": snapshot.start_date.isoformat() if snapshot.start_date else None,
            "end_date": snapshot.end_date.isoformat() if snapshot.end_date else None,
            "page_size": snapshot.page_size,
            "access_scope": snapshot.access_scope,
            "selection_sha256": snapshot.selection_sha256,
            "as_of_us": _datetime_to_us(snapshot.as_of),
            "expires_at_us": _datetime_to_us(snapshot.expires_at),
            "sort": "business_date_asc_nulls_last_business_key_asc",
        }
        if selection is not None:
            expected_binding["instrument_id"] = selection.instrument_id
            expected_binding["capture_id"] = selection.capture_id
        if any(payload.get(key) != value for key, value in expected_binding.items()):
            raise SnapshotError("cursor belongs to a different snapshot, window, or page contract")
        last_key = payload.get("last_business_key")
        if not isinstance(last_key, str) or not last_key:
            raise SnapshotError("cursor continuation key is invalid")
        raw_date = payload.get("last_business_date")
        try:
            last_date = date.fromisoformat(raw_date) if raw_date is not None else None
        except (TypeError, ValueError) as exc:
            raise SnapshotError("cursor business date is invalid") from exc
        if _datetime_to_us(datetime.now(_UTC)) >= _datetime_to_us(snapshot.expires_at):
            raise SnapshotExpiredError("snapshot cursor has expired")
        return last_date, last_key


def _ranked_visible_facts(snapshot: SnapshotRecord) -> Subquery:
    """Build a latest-revision-as-of query shared by pages and hash snapshots."""
    selection = _validated_capture_selection(
        snapshot.instrument_id,
        snapshot.capture_id,
        error_type=SnapshotError,
    )
    return _ranked_visible_facts_for(
        snapshot.dataset_id,
        snapshot.publication_sequence,
        selection=selection,
    )


def _ranked_visible_facts_for(
    dataset_id: str,
    publication_sequence: int,
    *,
    selection: InstrumentCaptureSelection | None = None,
    business_key_collation: str | None = None,
) -> Subquery:
    """Rank all facts for a dataset at a sequence, optionally within one selector."""
    dataset_partition = (
        PUBLISHED_FACT_VERSIONS.c.dataset_id.collate(business_key_collation)
        if business_key_collation is not None
        else PUBLISHED_FACT_VERSIONS.c.dataset_id
    )
    business_key_partition = (
        PUBLISHED_FACT_VERSIONS.c.business_key.collate(business_key_collation)
        if business_key_collation is not None
        else PUBLISHED_FACT_VERSIONS.c.business_key
    )
    predicates = [
        dataset_partition == dataset_id,
        PUBLISHED_FACT_VERSIONS.c.publication_sequence <= publication_sequence,
    ]
    if selection is not None:
        predicates.extend(
            (
                PUBLISHED_FACT_VERSIONS.c.instrument_id == selection.instrument_id,
                PUBLISHED_FACT_VERSIONS.c.capture_id == selection.capture_id,
            )
        )
    return (
        select(
            PUBLISHED_FACT_VERSIONS.c.dataset_id,
            PUBLISHED_FACT_VERSIONS.c.business_key,
            PUBLISHED_FACT_VERSIONS.c.revision,
            PUBLISHED_FACT_VERSIONS.c.publication_id,
            PUBLISHED_FACT_VERSIONS.c.publication_sequence,
            PUBLISHED_FACT_VERSIONS.c.instrument_id,
            PUBLISHED_FACT_VERSIONS.c.capture_id,
            PUBLISHED_FACT_VERSIONS.c.business_date,
            PUBLISHED_FACT_VERSIONS.c.event_at,
            PUBLISHED_FACT_VERSIONS.c.observed_at,
            PUBLISHED_FACT_VERSIONS.c.source_published_at,
            PUBLISHED_FACT_VERSIONS.c.available_at,
            PUBLISHED_FACT_VERSIONS.c.record_json,
            PUBLISHED_FACT_VERSIONS.c.record_hash,
            PUBLISHED_FACT_VERSIONS.c.is_deleted,
            func.row_number()
            .over(
                partition_by=(
                    dataset_partition,
                    business_key_partition,
                ),
                order_by=(
                    PUBLISHED_FACT_VERSIONS.c.publication_sequence.desc(),
                    PUBLISHED_FACT_VERSIONS.c.revision.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(*predicates)
        .subquery("od_snapshot_latest_revision")
    )


def _begin_coverage_read_transaction(connection: Connection) -> RootTransaction:
    """Start a transaction with repeatable reads or reject the SQL dialect."""
    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        transaction = connection.begin()
        try:
            read_uncommitted = connection.exec_driver_sql("PRAGMA read_uncommitted").scalar_one()
            if read_uncommitted != 0:
                raise CoverageSnapshotConsistencyError(
                    "SQLite read_uncommitted must be disabled for coverage verification"
                )
            # pysqlite's legacy transaction mode does not begin on SELECT. Issue
            # the physical BEGIN before either publication or fact rows are read.
            connection.exec_driver_sql("BEGIN")
            return transaction
        except Exception as exc:
            _rollback_coverage_read(transaction)
            if isinstance(exc, CoverageSnapshotConsistencyError):
                raise
            raise CoverageSnapshotConsistencyError(
                "SQLite could not establish a stable read transaction"
            ) from exc

    if dialect_name in {"mysql", "postgresql"}:
        try:
            connection.execution_options(isolation_level="REPEATABLE READ")
            return connection.begin()
        except Exception as exc:
            raise CoverageSnapshotConsistencyError(
                f"{dialect_name} could not establish REPEATABLE READ isolation"
            ) from exc

    raise CoverageSnapshotConsistencyError(
        f"database dialect {dialect_name!r} does not support verified coverage reads"
    )


def _begin_capture_discovery_read_transaction(connection: Connection) -> RootTransaction:
    """Start the same physical/repeatable snapshot required for discovery reads."""
    try:
        return _begin_coverage_read_transaction(connection)
    except CoverageSnapshotConsistencyError as exc:
        raise CaptureDiscoveryConsistencyError(
            "database cannot provide a stable capture discovery snapshot"
        ) from exc


def _require_capture_discovery_transactional_tables(connection: Connection) -> None:
    """Require transactional MySQL publication and fact tables for discovery."""
    try:
        _require_transactional_coverage_tables(connection)
    except CoverageSnapshotConsistencyError as exc:
        raise CaptureDiscoveryConsistencyError(
            "MySQL capture discovery requires publication and fact tables to use InnoDB"
        ) from exc


def _require_capture_discovery_index(connection: Connection) -> None:
    """Require the visible, full-column 0017 lookup index on a supported dialect."""
    dialect_name = connection.dialect.name
    try:
        if dialect_name == "sqlite":
            _require_sqlite_capture_discovery_index(connection)
            return
        if dialect_name == "mysql":
            _require_mysql_capture_discovery_index(connection)
            return
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires a verified SQLite or MySQL index"
        )
    except CaptureDiscoveryConsistencyError:
        raise
    except Exception as exc:
        raise CaptureDiscoveryConsistencyError(
            "capture discovery index could not be verified"
        ) from exc


def _require_sqlite_capture_discovery_index(connection: Connection) -> None:
    """Require SQLite's actual index key order and effective comparison rules."""
    indexes = (
        connection.exec_driver_sql('PRAGMA index_list("od_published_fact_versions")')
        .mappings()
        .all()
    )
    matches = [row for row in indexes if row.get("name") == _CAPTURE_DISCOVERY_INDEX_NAME]
    if (
        len(matches) != 1
        or matches[0].get("unique") not in (0, False)
        or matches[0].get("origin") != "c"
        or matches[0].get("partial") not in (0, False)
    ):
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires the reviewed SQLite index shape"
        )

    parts = (
        connection.exec_driver_sql(
            'SELECT seqno, name, "desc" AS descending, coll, "key" AS is_key '
            "FROM pragma_index_xinfo(:index_name) ORDER BY seqno",
            {"index_name": _CAPTURE_DISCOVERY_INDEX_NAME},
        )
        .mappings()
        .all()
    )
    key_parts = tuple(row for row in parts if row.get("is_key") == 1)
    if (
        len(key_parts) != len(_CAPTURE_DISCOVERY_INDEX_COLUMNS)
        or tuple(row.get("seqno") for row in key_parts)
        != tuple(range(len(_CAPTURE_DISCOVERY_INDEX_COLUMNS)))
        or tuple(row.get("name") for row in key_parts) != _CAPTURE_DISCOVERY_INDEX_COLUMNS
        or any(row.get("descending") not in (0, False) for row in key_parts)
        or any(str(row.get("coll", "")).casefold() != "binary" for row in key_parts)
    ):
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires the reviewed SQLite index key order and collation"
        )


def _require_mysql_capture_discovery_index(connection: Connection) -> None:
    """Require the visible full index and binary identity column contract on MySQL."""
    rows = (
        connection.execute(
            text(_MYSQL_CAPTURE_DISCOVERY_INDEX_METADATA_SQL),
            {"index_name": _CAPTURE_DISCOVERY_INDEX_NAME},
        )
        .mappings()
        .all()
    )
    if len(rows) != len(_CAPTURE_DISCOVERY_INDEX_COLUMNS):
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires the reviewed MySQL index shape"
        )
    for position, (row, column_name) in enumerate(
        zip(rows, _CAPTURE_DISCOVERY_INDEX_COLUMNS, strict=True),
        start=1,
    ):
        if (
            row.get("INDEX_NAME") != _CAPTURE_DISCOVERY_INDEX_NAME
            or type(row.get("NON_UNIQUE")) is not int
            or row.get("NON_UNIQUE") != 1
            or type(row.get("SEQ_IN_INDEX")) is not int
            or row.get("SEQ_IN_INDEX") != position
            or row.get("COLUMN_NAME") != column_name
            or row.get("SUB_PART") is not None
            or str(row.get("INDEX_TYPE", "")).upper() != "BTREE"
            or str(row.get("IS_VISIBLE", "")).upper() != "YES"
            or str(row.get("COLLATION", "")).upper() != "A"
        ):
            raise CaptureDiscoveryConsistencyError(
                "capture discovery requires the reviewed MySQL index shape"
            )

    identity_columns = (
        connection.exec_driver_sql(
            "SELECT COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'od_published_fact_versions' "
            "AND COLUMN_NAME IN ('dataset_id', 'instrument_id', 'capture_id')",
        )
        .mappings()
        .all()
    )
    if (
        len(identity_columns) != len(_CAPTURE_DISCOVERY_BINARY_COLUMNS)
        or {row.get("COLUMN_NAME") for row in identity_columns}
        != set(_CAPTURE_DISCOVERY_BINARY_COLUMNS)
        or any(
            str(row.get("CHARACTER_SET_NAME", "")).casefold() != "utf8mb4"
            or str(row.get("COLLATION_NAME", "")).casefold() != "utf8mb4_0900_bin"
            for row in identity_columns
        )
    ):
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires binary MySQL identity column collations"
        )


def _capture_discovery_fact_statement(
    *,
    dataset: str,
    publication_key: str,
    sequence: int,
    instrument: str,
    dialect_name: str,
) -> Select[tuple[str | None, str | None]]:
    """Build the exact 0017 equality-prefix query and force it on MySQL."""
    if dialect_name not in {"sqlite", "mysql"}:
        raise CaptureDiscoveryConsistencyError(
            "capture discovery requires a verified SQLite or MySQL query"
        )
    statement = (
        select(
            PUBLISHED_FACT_VERSIONS.c.capture_id,
            PUBLISHED_FACT_VERSIONS.c.observed_at,
        )
        .where(
            PUBLISHED_FACT_VERSIONS.c.dataset_id == dataset,
            PUBLISHED_FACT_VERSIONS.c.publication_id == publication_key,
            PUBLISHED_FACT_VERSIONS.c.publication_sequence == sequence,
            PUBLISHED_FACT_VERSIONS.c.instrument_id == instrument,
            PUBLISHED_FACT_VERSIONS.c.is_deleted.is_(False),
        )
        .limit(MAX_CAPTURE_DISCOVERY_ROWS + 1)
    )
    if dialect_name == "mysql":
        return statement.with_hint(
            PUBLISHED_FACT_VERSIONS,
            f"FORCE INDEX ({_CAPTURE_DISCOVERY_INDEX_NAME})",
            dialect_name="mysql",
        )
    return statement


def _rollback_coverage_read(transaction: RootTransaction) -> None:
    """Roll back a coverage read without hiding its original fail-closed error."""
    with suppress(Exception):
        transaction.rollback()


def _require_transactional_coverage_tables(connection: Connection) -> None:
    """Require InnoDB for both MySQL tables participating in a stable read."""
    rows = connection.exec_driver_sql(
        "SELECT table_name, engine FROM information_schema.tables "
        "WHERE table_schema = DATABASE() "
        "AND table_name IN ('od_publications', 'od_published_fact_versions')"
    ).all()
    engines = {str(row[0]).casefold(): str(row[1] or "").casefold() for row in rows}
    expected = {"od_publications", "od_published_fact_versions"}
    if set(engines) != expected or any(engines[name] != "innodb" for name in expected):
        raise CoverageSnapshotConsistencyError(
            "MySQL coverage reads require od_publications and "
            "od_published_fact_versions to use InnoDB"
        )


def _validate_capture_discovery_instrument(value: object) -> str:
    """Validate one source instrument without echoing an invalid identifier."""
    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_ID_LENGTH
        or _CAPTURE_DISCOVERY_INSTRUMENT_ID.fullmatch(value) is None
    ):
        raise ValueError("instrument_id is invalid")
    return value


def _capture_discovery_observed_at(value: object) -> datetime:
    """Require the exact canonical UTC observation instant persisted by publish."""
    if not isinstance(value, str) or len(value) > 40:
        raise CaptureDiscoveryRecordIntegrityError("capture observation timestamp is missing")
    try:
        parsed = _parse_timestamp(value)
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError("timestamp is not UTC")
        normalized = parsed.astimezone(_UTC)
        if _timestamp(normalized, "observed_at") != value:
            raise ValueError("timestamp is not canonical")
    except (OverflowError, TypeError, ValueError) as exc:
        raise CaptureDiscoveryRecordIntegrityError(
            "capture observation timestamp is invalid"
        ) from exc
    return normalized


def _sha256_value(value: str, name: str) -> str:
    """Validate and return one lowercase SHA-256 value."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    return value


def _coverage_record_storage_size(row: RowMapping) -> int:
    """Bound encoded bytes and metadata before parsing one persisted fact row."""
    business_key = row["business_key"]
    record_json = row["record_json"]
    if not isinstance(record_json, str) or len(record_json) > MAX_RECORD_BYTES:
        raise CoverageRecordIntegrityError("stored record_json exceeds its publication bound")
    fact_provenance = row["fact_publication_provenance_json"]
    if (
        not isinstance(fact_provenance, str)
        or len(fact_provenance) > MAX_METADATA_BYTES
        or len(fact_provenance.encode("utf-8")) > MAX_METADATA_BYTES
    ):
        raise CoverageRecordIntegrityError("stored fact publication provenance exceeds its bound")
    _label(business_key, "stored business_key", MAX_BUSINESS_KEY_LENGTH)
    total = (
        len(business_key.encode("utf-8"))
        + len(record_json.encode("utf-8"))
        + len(fact_provenance.encode("utf-8"))
        + 256
    )
    bounded_text = (
        (row["event_at"], 40, "event_at"),
        (row["observed_at"], 40, "observed_at"),
        (row["source_published_at"], 40, "source_published_at"),
        (row["available_at"], 40, "available_at"),
        (row["instrument_id"], MAX_ID_LENGTH, "instrument_id"),
        (row["capture_id"], 64, "capture_id"),
    )
    for value, maximum, name in bounded_text:
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > maximum:
            raise CoverageRecordIntegrityError(f"stored {name} exceeds its publication bound")
        total += len(value.encode("utf-8"))
    return total


def _verified_coverage_record(
    row: RowMapping,
    *,
    dataset: str,
    max_sequence: int,
    publication: PublicationRecord,
) -> PublishedRecord:
    """Rebuild one row through the publication normalizer and compare its hash."""
    if row["is_deleted"] is not False:
        raise CoverageRecordIntegrityError("visible fact query returned a tombstone")
    if (
        row["dataset_id"] != dataset
        or type(row["revision"]) is not int
        or row["revision"] < 1
        or type(row["publication_sequence"]) is not int
        or not 1 <= row["publication_sequence"] <= max_sequence
    ):
        raise CoverageRecordIntegrityError("visible fact identity or revision is inconsistent")
    try:
        _hex_identifier(row["publication_id"], "stored publication_id")
    except ValueError as exc:
        raise CoverageRecordIntegrityError("visible fact publication id is malformed") from exc
    if (
        row["fact_publication_id"] != row["publication_id"]
        or row["fact_publication_dataset_id"] != dataset
        or row["fact_publication_sequence"] != row["publication_sequence"]
        or row["fact_publication_source_id"] != publication.source_id
        or row["fact_publication_schema_version"] != publication.schema_version
        or row["fact_publication_semantic_version"] != publication.semantic_version
        or row["fact_publication_unit"] != publication.unit
        or row["fact_publication_adjustment_basis"] != publication.adjustment_basis
    ):
        raise CoverageRecordIntegrityError(
            "visible fact does not match its immutable publication contract"
        )
    try:
        fact_provenance = json.loads(row["fact_publication_provenance_json"])
    except Exception as exc:
        raise CoverageRecordIntegrityError(
            "visible fact publication provenance is malformed"
        ) from exc
    if not isinstance(fact_provenance, dict) or fact_provenance.get(
        "contract_sha256"
    ) != publication.provenance.get("contract_sha256"):
        raise CoverageRecordIntegrityError("visible fact publication contract hash is inconsistent")
    if (
        _json_object(
            fact_provenance,
            "stored fact publication provenance",
            MAX_METADATA_BYTES,
        )
        != row["fact_publication_provenance_json"]
    ):
        raise CoverageRecordIntegrityError(
            "visible fact publication provenance is not canonically encoded"
        )
    stored_record_json = row["record_json"]
    try:
        payload = json.loads(stored_record_json)
        candidate = PublishedRecordInput(
            business_key=row["business_key"],
            payload=payload,
            business_date=row["business_date"],
            observed_at=_parse_timestamp(row["observed_at"]),
            source_published_at=_parse_timestamp(row["source_published_at"]),
            available_at=_parse_timestamp(row["available_at"]),
            event_at=_parse_timestamp(row["event_at"]),
            instrument_id=row["instrument_id"],
            capture_id=row["capture_id"],
        )
        normalized = _normalize_records((candidate,))
    except Exception as exc:
        if isinstance(exc, CoverageRecordIntegrityError):
            raise
        raise CoverageRecordIntegrityError("stored visible fact cannot be normalized") from exc
    if len(normalized) != 1:
        raise CoverageRecordIntegrityError("stored visible fact did not normalize to one row")
    item = normalized[0]
    stored_hash = row["record_hash"]
    if (
        not isinstance(stored_hash, str)
        or len(stored_hash) != 64
        or any(character not in "0123456789abcdef" for character in stored_hash)
        or not hmac.compare_digest(item["record_hash"], stored_hash)
    ):
        raise CoverageRecordIntegrityError("stored visible fact record_hash does not match")
    if (
        item["business_key"] != row["business_key"]
        or item["business_date"] != row["business_date"]
        or item["event_at"] != row["event_at"]
        or item["observed_at"] != row["observed_at"]
        or item["source_published_at"] != row["source_published_at"]
        or item["available_at"] != row["available_at"]
        or item["instrument_id"] != row["instrument_id"]
        or item["capture_id"] != row["capture_id"]
        or item["record_json"] != stored_record_json
    ):
        raise CoverageRecordIntegrityError(
            "stored visible fact fields are not in canonical publication form"
        )
    return _published_record_from_row(row)


def _coverage_record_sort_key(record: PublishedRecord) -> tuple[bool, date, bytes]:
    """Sort non-null business dates first, then dates and UTF-8 business keys."""
    return (
        record.business_date is None,
        record.business_date if record.business_date is not None else date.min,
        record.business_key.encode("utf-8"),
    )


def _visible_record_set_sha256(
    publication: PublicationRecord,
    contract_sha256: str,
    records: list[PublishedRecord],
) -> str:
    """Hash one canonical header and ordered canonical row per visible record.

    The preimage is canonical JSON encoded as UTF-8 with one newline after the
    header and each row. Header fields bind all publication/contract metadata,
    algorithm versions, ordering, and visible row count. Each row binds the
    business key, revision, business date, source publication, sequence, and
    recomputed publication record hash.
    """
    header = {
        "adjustment_basis": publication.adjustment_basis,
        "algorithm": VISIBLE_RECORD_SET_ALGORITHM,
        "contract_sha256": contract_sha256,
        "dataset_id": publication.dataset_id,
        "publication_id": publication.publication_id,
        "publication_sequence": publication.sequence,
        "record_count": len(records),
        "row_hash_algorithm": VISIBLE_RECORD_HASH_ALGORITHM,
        "schema_version": publication.schema_version,
        "semantic_version": publication.semantic_version,
        "sort_id": VISIBLE_RECORD_SET_SORT,
        "source_id": publication.source_id,
        "unit": publication.unit,
    }
    digest = hashlib.sha256()
    digest.update(_canonical_json(header).encode("utf-8"))
    digest.update(b"\n")
    for record in records:
        row = {
            "business_date": record.business_date.isoformat()
            if record.business_date is not None
            else None,
            "business_key": record.business_key,
            "publication_id": record.publication_id,
            "publication_sequence": record.publication_sequence,
            "record_hash": record.record_hash,
            "revision": record.revision,
        }
        digest.update(_canonical_json(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _snapshot_content_hash(
    connection: Connection,
    dataset_id: str,
    publication: PublicationRecord,
    start: date | None,
    end: date | None,
    access_scope: str,
    page_size: int,
    *,
    instrument_id: str | None = None,
    capture_id: str | None = None,
) -> tuple[str, int]:
    """Hash every visible key/revision in a scoped half-open window in order."""
    temporary = SnapshotRecord(
        snapshot_id="0" * 32,
        dataset_id=dataset_id,
        publication_id=publication.publication_id,
        schema_version=publication.schema_version,
        publication_sequence=publication.sequence,
        start_date=start,
        end_date=end,
        page_size=page_size,
        access_scope=access_scope,
        selection_sha256=_snapshot_selection_hash(
            dataset_id,
            publication,
            start,
            end,
            access_scope,
            page_size,
            instrument_id=instrument_id,
            capture_id=capture_id,
        ),
        content_hash="0" * 64,
        record_count=0,
        created_at=datetime.now(_UTC),
        expires_at=datetime.now(_UTC) + timedelta(seconds=1),
        as_of=publication.published_at,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )
    ranked = _ranked_visible_facts(temporary)
    statement = select(
        ranked.c.business_key,
        ranked.c.revision,
        ranked.c.business_date,
        ranked.c.record_hash,
    ).where(ranked.c.row_number == 1, ranked.c.is_deleted.is_(False))
    if start is not None:
        statement = statement.where(ranked.c.business_date >= start)
    if end is not None:
        statement = statement.where(ranked.c.business_date < end)
    statement = statement.order_by(
        case((ranked.c.business_date.is_(None), 1), else_=0),
        ranked.c.business_date,
        ranked.c.business_key,
    )
    digest = hashlib.sha256()
    header = {
        "dataset_id": dataset_id,
        "publication_id": publication.publication_id,
        "publication_sequence": publication.sequence,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "access_scope": access_scope,
        "page_size": page_size,
        "sort": "business_date_asc_nulls_last_business_key_asc",
    }
    header["selection_sha256"] = _snapshot_selection_hash(
        dataset_id,
        publication,
        start,
        end,
        access_scope,
        page_size,
        instrument_id=instrument_id,
        capture_id=capture_id,
    )
    digest.update(_canonical_json(header).encode("utf-8"))
    digest.update(b"\n")
    count = 0
    result = connection.execute(
        statement.execution_options(stream_results=True, yield_per=_HASH_YIELD_PER)
    ).mappings()
    try:
        for row in result:
            if count >= MAX_SNAPSHOT_RECORDS:
                raise SnapshotSizeLimitError(
                    f"snapshot exceeds the {MAX_SNAPSHOT_RECORDS:,}-record limit"
                )
            item = {
                "business_key": row["business_key"],
                "revision": row["revision"],
                "business_date": (
                    row["business_date"].isoformat() if row["business_date"] else None
                ),
                "record_hash": row["record_hash"],
            }
            digest.update(_canonical_json(item).encode("utf-8"))
            digest.update(b"\n")
            count += 1
    finally:
        result.close()
    return digest.hexdigest(), count


def _published_record_response_size(row: RowMapping) -> int:
    """Conservatively estimate one serialized API row without decoding its payload."""
    record_json = row["record_json"]
    if not isinstance(record_json, str):
        raise ValueError("stored record_json must be text")
    payload_bytes = _json_text_ascii_size_upper_bound(record_json)
    metadata = {
        "dataset_id": row["dataset_id"],
        "business_key": row["business_key"],
        "revision": row["revision"],
        "publication_id": row["publication_id"],
        "publication_sequence": row["publication_sequence"],
        "business_date": row["business_date"].isoformat() if row["business_date"] else None,
        "event_at": _timestamp_output(row["event_at"]),
        "observed_at": _timestamp_output(row["observed_at"]),
        "source_published_at": _timestamp_output(row["source_published_at"]),
        "available_at": _timestamp_output(row["available_at"]),
        "payload": None,
        "record_hash": row["record_hash"],
    }
    metadata_json = _canonical_json(metadata)
    placeholder = '"payload":null'
    if placeholder not in metadata_json:
        raise ValueError("could not estimate serialized record metadata")
    metadata_bytes = (
        _json_text_ascii_size_upper_bound(metadata_json) - len(placeholder) + len('"payload":')
    )
    return payload_bytes + metadata_bytes + _PAGE_ROW_OVERHEAD_BYTES


def _json_text_ascii_size_upper_bound(value: str) -> int:
    """Bound compact JSON bytes even if an API encoder escapes non-ASCII text."""
    return sum(1 if ord(char) < 128 else 6 if ord(char) <= 0xFFFF else 12 for char in value)


def _timestamp_output(value: str | None) -> str | None:
    """Match the public datetime ISO representation used by JSON encoders."""
    parsed = _parse_timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _snapshot_selection_hash(
    dataset_id: str,
    publication: PublicationRecord,
    start: date | None,
    end: date | None,
    access_scope: str,
    page_size: int,
    *,
    instrument_id: str | None = None,
    capture_id: str | None = None,
) -> str:
    """Hash the immutable selection semantics, including source/schema identity."""
    selection = {
        "dataset_id": dataset_id,
        "source_id": publication.source_id,
        "publication_id": publication.publication_id,
        "publication_sequence": publication.sequence,
        "schema_version": publication.schema_version,
        "semantic_version": publication.semantic_version,
        "as_of": publication.published_at.isoformat(),
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "access_scope": access_scope,
        "page_size": page_size,
        "sort": "business_date_asc_nulls_last_business_key_asc",
    }
    selector = _validated_capture_selection(
        instrument_id,
        capture_id,
        error_type=SnapshotError,
    )
    if selector is not None:
        selection.update(
            {
                "selection_version": 2,
                "instrument_id": selector.instrument_id,
                "capture_id": selector.capture_id,
            }
        )
    return _sha256(_canonical_json(selection).encode("utf-8"))


def _ensure_head(connection: Connection, dataset_id: str) -> None:
    """Create an empty sequence head safely within a nested savepoint."""
    found = connection.execute(
        select(PUBLICATION_HEADS.c.dataset_id).where(PUBLICATION_HEADS.c.dataset_id == dataset_id)
    ).scalar_one_or_none()
    if found is not None:
        return
    try:
        with connection.begin_nested():
            connection.execute(
                insert(PUBLICATION_HEADS).values(
                    dataset_id=dataset_id,
                    latest_sequence=0,
                    latest_publication_id=None,
                    watermark_json=None,
                    updated_at_us=_datetime_to_us(datetime.now(_UTC)),
                )
            )
    except IntegrityError:
        # A concurrent publisher created the same head; continue under its row.
        return


def _append_changed_revision(
    connection: Connection,
    dataset_id: str,
    publication_id: str,
    sequence: int,
    record: dict[str, Any],
) -> None:
    """Append one business-key revision only when canonical content changed."""
    existing = (
        connection.execute(
            select(PUBLISHED_FACT_VERSIONS)
            .where(
                PUBLISHED_FACT_VERSIONS.c.dataset_id == dataset_id,
                PUBLISHED_FACT_VERSIONS.c.business_key == record["business_key"],
            )
            .order_by(
                PUBLISHED_FACT_VERSIONS.c.publication_sequence.desc(),
                PUBLISHED_FACT_VERSIONS.c.revision.desc(),
            )
            .limit(1)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if existing is not None and (
        existing["instrument_id"],
        existing["capture_id"],
    ) != (record["instrument_id"], record["capture_id"]):
        raise PublicationContractConflictError(
            "fact selector metadata is immutable for each business key"
        )
    if existing is not None and existing["record_hash"] == record["record_hash"]:
        return
    revision = int(existing["revision"]) + 1 if existing is not None else 1
    connection.execute(
        insert(PUBLISHED_FACT_VERSIONS).values(
            dataset_id=dataset_id,
            business_key=record["business_key"],
            revision=revision,
            publication_id=publication_id,
            publication_sequence=sequence,
            instrument_id=record["instrument_id"],
            capture_id=record["capture_id"],
            business_date=record["business_date"],
            event_at=record["event_at"],
            observed_at=record["observed_at"],
            source_published_at=record["source_published_at"],
            available_at=record["available_at"],
            record_json=record["record_json"],
            record_hash=record["record_hash"],
            is_deleted=False,
        )
    )


def _validated_capture_selection(
    instrument_id: object | None,
    capture_id: object | None,
    *,
    error_type: type[ValueError] = ValueError,
) -> InstrumentCaptureSelection | None:
    """Validate one paired storage selector with a caller-safe exception type."""
    try:
        return validate_selector_pair(instrument_id=instrument_id, capture_id=capture_id)
    except InstrumentCaptureSelectionError as exc:
        raise error_type(str(exc)) from None


def _normalize_records(records: Iterable[PublishedRecordInput]) -> list[dict[str, Any]]:
    """Validate, canonicalize, and bound one replacement-record batch."""
    if isinstance(records, (str, bytes, Mapping)):
        raise ValueError("records must be an iterable of PublishedRecordInput values")
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise ValueError("records must be iterable") from exc
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for index, item in enumerate(iterator):
        if index >= MAX_PUBLICATION_RECORDS:
            raise ValueError(f"publication exceeds {MAX_PUBLICATION_RECORDS:,} records")
        if not isinstance(item, PublishedRecordInput):
            raise ValueError(f"records[{index}] must be PublishedRecordInput")
        business_key = _label(
            item.business_key, f"records[{index}].business_key", MAX_BUSINESS_KEY_LENGTH
        )
        if business_key in seen:
            raise ValueError(f"duplicate business_key in publication: {business_key}")
        seen.add(business_key)
        if item.business_date is not None and (
            isinstance(item.business_date, datetime) or not isinstance(item.business_date, date)
        ):
            raise ValueError(f"records[{index}].business_date must be datetime.date")
        selection = _validated_capture_selection(item.instrument_id, item.capture_id)
        payload_json = _json_object(item.payload, f"records[{index}].payload", MAX_RECORD_BYTES)
        payload = json.loads(payload_json)
        if selection is not None and (
            payload.get("instrument_id") != selection.instrument_id
            or payload.get("capture_id") != selection.capture_id
        ):
            raise ValueError(f"records[{index}] selector metadata does not match its payload")
        observed_at = _timestamp(item.observed_at, f"records[{index}].observed_at")
        source_published_at = _timestamp(
            item.source_published_at, f"records[{index}].source_published_at"
        )
        available_at = _timestamp(item.available_at, f"records[{index}].available_at")
        event_at = _timestamp(item.event_at, f"records[{index}].event_at")
        canonical = {
            "business_key": business_key,
            "business_date": item.business_date.isoformat() if item.business_date else None,
            "event_at": event_at,
            "observed_at": observed_at,
            "source_published_at": source_published_at,
            "available_at": available_at,
            "payload": payload,
        }
        if selection is not None:
            canonical["selection"] = {
                "instrument_id": selection.instrument_id,
                "capture_id": selection.capture_id,
            }
        record_hash = _sha256(_canonical_json(canonical).encode("utf-8"))
        size = len(payload_json.encode("utf-8")) + len(business_key.encode("utf-8")) + 256
        total_bytes += size
        if size > MAX_RECORD_BYTES:
            raise ValueError(f"records[{index}] exceeds the {MAX_RECORD_BYTES:,}-byte limit")
        if total_bytes > MAX_BATCH_BYTES:
            raise ValueError(f"publication exceeds the {MAX_BATCH_BYTES:,}-byte batch limit")
        normalized.append(
            {
                "business_key": business_key,
                "business_date": item.business_date,
                "event_at": event_at,
                "observed_at": observed_at,
                "source_published_at": source_published_at,
                "available_at": available_at,
                "instrument_id": selection.instrument_id if selection is not None else None,
                "capture_id": selection.capture_id if selection is not None else None,
                "record_json": payload_json,
                "record_hash": record_hash,
                "canonical": canonical,
            }
        )
    normalized.sort(key=lambda record: record["business_key"])
    return normalized


def _json_object(value: Mapping[str, Any], name: str, maximum_bytes: int) -> str:
    """Validate and canonically serialize bounded credential-free JSON object."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    copied = _validate_json(value, name, depth=0, node_count=[0], active=set())
    encoded = _canonical_json(copied)
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} exceeds the {maximum_bytes:,}-byte limit")
    return encoded


def _validate_json(
    value: object,
    path: str,
    *,
    depth: int,
    node_count: list[int],
    active: set[int],
) -> object:
    """Copy only bounded JSON values, rejecting non-finite and secret-like data."""
    node_count[0] += 1
    if node_count[0] > 200_000:
        raise ValueError("JSON input exceeds the 200,000-node limit")
    if depth > 16:
        raise ValueError(f"JSON input at {path} exceeds the maximum depth")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"JSON number at {path} must be finite")
        return value
    if isinstance(value, str):
        if len(value) > MAX_METADATA_BYTES or any(ord(char) < 32 for char in value):
            raise ValueError(f"JSON string at {path} is too long or contains control characters")
        if _BEARER_VALUE.match(value) or _COMMON_SECRET_VALUE.fullmatch(value):
            raise ValueError(f"secret-like value is not allowed at {path}")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"cyclic JSON object at {path}")
        if len(value) > 10_000:
            raise ValueError(f"JSON object at {path} exceeds the 10,000-item limit")
        active.add(identity)
        output: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError(f"JSON object key at {path} must be a bounded nonempty string")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                raise ValueError(f"credential-like field is not allowed at {path}.{key}")
            output[key] = _validate_json(
                child,
                f"{path}.{key}",
                depth=depth + 1,
                node_count=node_count,
                active=active,
            )
        active.remove(identity)
        return output
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"cyclic JSON array at {path}")
        if len(value) > 10_000:
            raise ValueError(f"JSON array at {path} exceeds the 10,000-item limit")
        active.add(identity)
        array_output = [
            _validate_json(
                child,
                f"{path}[{index}]",
                depth=depth + 1,
                node_count=node_count,
                active=active,
            )
            for index, child in enumerate(value)
        ]
        active.remove(identity)
        return array_output
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def _publication_from_row(row: RowMapping) -> PublicationRecord:
    """Parse one database publication row into its public immutable model."""
    return PublicationRecord(
        publication_id=row["publication_id"],
        dataset_id=row["dataset_id"],
        source_id=row["source_id"],
        sequence=row["sequence"],
        schema_version=row["schema_version"],
        semantic_version=row["semantic_version"],
        unit=row["unit"],
        adjustment_basis=row["adjustment_basis"],
        quality=json.loads(row["quality_json"]),
        provenance=json.loads(row["provenance_json"]),
        watermark=json.loads(row["watermark_json"]) if row["watermark_json"] is not None else None,
        idempotency_key=row["idempotency_key"],
        payload_hash=row["payload_hash"],
        record_count=row["record_count"],
        published_at=_us_to_datetime(row["published_at_us"]),
    )


def _snapshot_from_row(row: RowMapping) -> SnapshotRecord:
    """Parse one persisted snapshot row into its public immutable model."""
    selection = _validated_capture_selection(
        row["instrument_id"],
        row["capture_id"],
        error_type=SnapshotError,
    )
    return SnapshotRecord(
        snapshot_id=row["snapshot_id"],
        dataset_id=row["dataset_id"],
        publication_id=row["publication_id"],
        schema_version=row["schema_version"],
        publication_sequence=row["publication_sequence"],
        start_date=row["start_date"],
        end_date=row["end_date"],
        page_size=row["page_size"],
        access_scope=row["access_scope"],
        selection_sha256=row["selection_sha256"],
        content_hash=row["content_hash"],
        record_count=row["record_count"],
        created_at=_us_to_datetime(row["created_at_us"]),
        expires_at=_us_to_datetime(row["expires_at_us"]),
        as_of=_us_to_datetime(row["as_of_us"]),
        instrument_id=selection.instrument_id if selection is not None else None,
        capture_id=selection.capture_id if selection is not None else None,
    )


def _published_record_from_row(row: RowMapping) -> PublishedRecord:
    """Parse the selected row version without conflating its time dimensions."""
    return PublishedRecord(
        dataset_id=row["dataset_id"],
        business_key=row["business_key"],
        revision=row["revision"],
        publication_id=row["publication_id"],
        publication_sequence=row["publication_sequence"],
        business_date=row["business_date"],
        event_at=_parse_timestamp(row["event_at"]),
        observed_at=_parse_timestamp(row["observed_at"]),
        source_published_at=_parse_timestamp(row["source_published_at"]),
        available_at=_parse_timestamp(row["available_at"]),
        payload=json.loads(row["record_json"]),
        record_hash=row["record_hash"],
    )


def _ensure_same_payload(row: RowMapping, payload_hash: str) -> RowMapping:
    """Reject changed content under an existing idempotency key."""
    if row["payload_hash"] != payload_hash:
        raise PublicationConflictError(
            "idempotency_key already exists with a different publication payload"
        )
    return row


def _require_compatible_publication_contract(
    previous: RowMapping,
    *,
    source_id: str,
    schema_version: str,
    semantic_version: str,
    unit: str,
    adjustment_basis: str,
    provenance: Mapping[str, Any],
) -> None:
    """Reject metadata changes that would relabel retained delta facts."""
    incoming = {
        "source_id": source_id,
        "schema_version": schema_version,
        "semantic_version": semantic_version,
        "unit": unit,
        "adjustment_basis": adjustment_basis,
    }
    changed = [name for name, value in incoming.items() if previous[name] != value]
    previous_provenance = json.loads(previous["provenance_json"])
    if not isinstance(previous_provenance, dict):
        raise RuntimeError("stored publication provenance must be a JSON object")
    prior_hash = (
        "contract_sha256" in previous_provenance,
        previous_provenance.get("contract_sha256"),
    )
    incoming_hash = ("contract_sha256" in provenance, provenance.get("contract_sha256"))
    if prior_hash != incoming_hash:
        changed.append("provenance.contract_sha256")
    if changed:
        changed_fields = ", ".join(changed)
        raise PublicationContractConflictError(
            "dataset publication contract changed: " + changed_fields
        )


def _identifier(value: str, name: str) -> str:
    """Validate a bounded identifier used in a persistent key."""
    return _label(value, name, MAX_ID_LENGTH)


def _hex_identifier(value: str, name: str) -> str:
    """Validate a UUID-shaped lowercase hex identifier."""
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{name} must be a lowercase 32-character hex identifier")
    return value


def _label(value: str, name: str, maximum: int) -> str:
    """Validate a single-line nonempty string label."""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a nonempty string of at most {maximum} characters")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} must not have surrounding whitespace or control characters")
    return value


def _timestamp(value: datetime | None, name: str) -> str | None:
    """Normalize an aware instant to ISO-8601 UTC without inventing time."""
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a previously validated UTC timestamp string."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_date_window(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    """Require date-only, nonempty half-open bounds when both are supplied."""
    for value, name in ((start, "start_date"), (end, "end_date")):
        if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
            raise ValueError(f"{name} must be datetime.date, not timestamp or string")
    if start is not None and end is not None and start >= end:
        raise ValueError("date window must be nonempty and half-open: start_date < end_date")
    return start, end


def _page_limit(value: int, name: str, *, max_value: int) -> int:
    """Validate an exact integer page bound."""
    if type(value) is not int or value < 1 or value > max_value:
        raise ValueError(f"{name} must be an integer between 1 and {max_value}")
    return value


def _ttl_to_us(ttl: timedelta) -> int:
    """Validate and convert a bounded snapshot TTL to exact microseconds."""
    if not isinstance(ttl, timedelta) or ttl < MIN_SNAPSHOT_TTL or ttl > MAX_SNAPSHOT_TTL:
        raise ValueError("ttl must be between one second and seven days")
    value = ttl.days * 86_400_000_000 + ttl.seconds * 1_000_000 + ttl.microseconds
    if value <= 0:
        raise ValueError("ttl must be positive")
    return value


def _require_unexpired(snapshot: SnapshotRecord) -> None:
    """Raise the API-mappable error for an expired persistent snapshot."""
    if datetime.now(_UTC) >= snapshot.expires_at:
        raise SnapshotExpiredError("snapshot has expired")


def _datetime_to_us(value: datetime) -> int:
    """Convert an aware instant to epoch microseconds without float rounding."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    utc_value = value.astimezone(_UTC)
    epoch = datetime(1970, 1, 1, tzinfo=_UTC)
    delta = utc_value - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _us_to_datetime(value: int) -> datetime:
    """Convert epoch microseconds to an aware UTC datetime."""
    epoch = datetime(1970, 1, 1, tzinfo=_UTC)
    return epoch + timedelta(microseconds=value)


def _canonical_json(value: object) -> str:
    """Encode JSON deterministically and reject NaN or Infinity."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: bytes) -> str:
    """Return the lowercase SHA-256 hex digest."""
    return hashlib.sha256(value).hexdigest()


def _base64url(value: bytes) -> str:
    """Encode URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    """Decode canonical URL-safe base64 without accepting arbitrary characters."""
    if not value or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for char in value
    ):
        raise ValueError("invalid base64url value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON object keys in signed cursor payloads."""
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON object key")
        output[key] = value
    return output
