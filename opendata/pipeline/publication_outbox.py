"""Private transactional publication-event schema and canonicalization helpers.

The publisher owns event insertion so only the new-publication branch can append
an event. This module deliberately exposes no append or repair API; historical
publications are never backfilled. A caller-supplied contract digest is frozen
as unverified evidence and is never synthesized when absent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from sqlalchemy.engine import Connection, RowMapping

__all__: list[str] = []

_EVENT_VERSION = 1
_EVENT_TYPE = "opendata.publication.published"
_MAX_EVENT_BYTES = 8_192
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PUBLICATION_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_EVENT_HASH_DOMAIN = b"opendata.publication-outbox.event.v1\n"
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_RECORD_COUNT = 2_147_483_647
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_EVIDENCE_ABSENT = "absent"
_EVIDENCE_CALLER_SUPPLIED = "caller_supplied_unverified"
_MYSQL_TRANSACTIONAL_TABLES = (
    "od_publication_heads",
    "od_publications",
    "od_published_fact_versions",
    "od_publication_outbox",
)
_MYSQL_TRANSACTIONAL_TABLES_QUERY = text(
    "SELECT TABLE_NAME AS table_name, ENGINE AS engine "
    "FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA = DATABASE() "
    "AND TABLE_NAME IN (:publication_heads, :publications, :fact_versions, :outbox)"
)


class _PublicationOutboxError(ValueError):
    """Base error for unsupported targets or invalid frozen event data."""


class _PublicationOutboxConflictError(_PublicationOutboxError):
    """Raised when an outbox identity is bound to different event content."""


class _PublicationOutboxSourceError(_PublicationOutboxError):
    """Raised when a just-inserted publication row cannot be frozen safely."""


@dataclass(frozen=True, slots=True)
class _PublicationOutboxCandidate:
    """Bounded immutable summary read from the publication row just inserted."""

    publication_id: str
    dataset_id: str
    sequence: int
    record_count: int
    payload_sha256: str
    contract_sha256: str | None
    contract_evidence_kind: str
    provenance_sha256: str
    schema_version: str
    published_at_us: int


@dataclass(frozen=True, slots=True)
class _PublicationOutboxEvent:
    """One canonical immutable publication event prepared for insertion."""

    publication_id: str
    dataset_id: str
    sequence: int
    record_count: int
    payload_sha256: str
    contract_sha256: str | None
    contract_evidence_kind: str
    provenance_sha256: str
    schema_version: str
    event_version: int
    event_json: str
    event_sha256: str
    published_at_us: int


def _make_publication_outbox_table(metadata: MetaData) -> Table:
    """Register the runtime outbox table in the publication metadata registry.

    Args:
        metadata: Metadata already containing ``od_publications``.

    Returns:
        The table model used by the publisher and isolated metadata fixtures.
    """
    return Table(
        "od_publication_outbox",
        metadata,
        Column(
            "publication_id",
            String(32),
            ForeignKey(
                "od_publications.publication_id",
                name="fk_od_pub_outbox_publication",
                ondelete="RESTRICT",
            ),
            primary_key=True,
            nullable=False,
        ),
        Column(
            "dataset_id",
            String(128).with_variant(VARCHAR(128, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=False,
        ),
        Column("publication_sequence", BigInteger, nullable=False),
        Column("record_count", BigInteger, nullable=False),
        Column("payload_sha256", String(64), nullable=False),
        Column("contract_sha256", String(64), nullable=True),
        Column("contract_evidence_kind", String(32), nullable=False),
        Column("provenance_sha256", String(64), nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("event_version", Integer, nullable=False),
        Column(
            "event_json",
            Text().with_variant(MEDIUMTEXT(), "mysql"),
            nullable=False,
        ),
        Column("event_sha256", String(64), nullable=False),
        Column("published_at_us", BigInteger, nullable=False),
        CheckConstraint("publication_sequence > 0", name="ck_od_pub_outbox_sequence"),
        CheckConstraint("record_count >= 0", name="ck_od_pub_outbox_record_count"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_od_pub_outbox_payload_sha"),
        CheckConstraint(
            "contract_sha256 IS NULL OR length(contract_sha256) = 64",
            name="ck_od_pub_outbox_contract_sha",
        ),
        CheckConstraint(
            "(contract_evidence_kind = 'absent' AND contract_sha256 IS NULL) OR "
            "(contract_evidence_kind = 'caller_supplied_unverified' "
            "AND contract_sha256 IS NOT NULL)",
            name="ck_od_pub_outbox_contract_evidence",
        ),
        CheckConstraint("length(provenance_sha256) = 64", name="ck_od_pub_outbox_provenance_sha"),
        CheckConstraint("length(schema_version) > 0", name="ck_od_pub_outbox_schema"),
        CheckConstraint("event_version = 1", name="ck_od_pub_outbox_event_version"),
        CheckConstraint(
            f"length(event_json) BETWEEN 2 AND {_MAX_EVENT_BYTES}",
            name="ck_od_pub_outbox_event_size",
        ),
        CheckConstraint("length(event_sha256) = 64", name="ck_od_pub_outbox_event_sha"),
        CheckConstraint("published_at_us >= 0", name="ck_od_pub_outbox_published_at"),
        UniqueConstraint(
            "dataset_id",
            "publication_sequence",
            name="uq_od_pub_outbox_dataset_sequence",
        ),
        mysql_engine="InnoDB",
    )


def _candidate_from_publication_row(row: RowMapping) -> _PublicationOutboxCandidate:
    """Freeze the persisted publication fields and provenance evidence.

    A caller-provided ``contract_sha256`` is not authenticated by this generic
    publisher. The event records its source explicitly and separately hashes
    the exact canonical provenance JSON. No contract digest is invented.

    Args:
        row: Persisted publication row selected after its new INSERT.

    Returns:
        A validated bounded event candidate.

    Raises:
        _PublicationOutboxSourceError: The provenance is malformed or unbounded.
        ValueError: A persisted value has an invalid shape.
    """
    provenance_json = row["provenance_json"]
    if not isinstance(provenance_json, str):
        raise _PublicationOutboxSourceError("publication provenance must be canonical JSON text")
    provenance_bytes = provenance_json.encode("utf-8")
    if len(provenance_bytes) > 262_144:
        raise _PublicationOutboxSourceError("publication provenance exceeds its bound")
    try:
        provenance: Any = json.loads(provenance_json)
        if not isinstance(provenance, dict) or _canonical_json(provenance) != provenance_json:
            raise ValueError("provenance is not a canonical JSON object")
        contract = provenance.get("contract_sha256")
        if "contract_sha256" in provenance:
            if not isinstance(contract, str) or not _SHA256.fullmatch(contract):
                raise ValueError("contract_sha256 is not a lowercase SHA-256 digest")
            evidence_kind = _EVIDENCE_CALLER_SUPPLIED
        else:
            contract = None
            evidence_kind = _EVIDENCE_ABSENT
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _PublicationOutboxSourceError("publication provenance is invalid") from exc

    candidate = _PublicationOutboxCandidate(
        publication_id=row["publication_id"],
        dataset_id=row["dataset_id"],
        sequence=row["sequence"],
        record_count=row["record_count"],
        payload_sha256=row["payload_hash"],
        contract_sha256=contract,
        contract_evidence_kind=evidence_kind,
        provenance_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        schema_version=row["schema_version"],
        published_at_us=row["published_at_us"],
    )
    return _normalize_candidate(candidate)


def _build_publication_event(
    candidate: _PublicationOutboxCandidate,
) -> _PublicationOutboxEvent:
    """Build the versioned canonical event for a new publication.

    Args:
        candidate: Frozen values from a publication row inserted by the caller.

    Returns:
        The canonical event with a domain-separated SHA-256 digest.

    Raises:
        ValueError: The candidate or its canonical envelope exceeds bounds.
    """
    candidate = _normalize_candidate(candidate)
    event_json = _canonical_json(_event_payload(candidate))
    event_bytes = event_json.encode("utf-8")
    if len(event_bytes) > _MAX_EVENT_BYTES:
        raise ValueError("canonical publication event exceeds its byte limit")
    event_sha256 = hashlib.sha256(_EVENT_HASH_DOMAIN + event_bytes).hexdigest()
    return _PublicationOutboxEvent(
        publication_id=candidate.publication_id,
        dataset_id=candidate.dataset_id,
        sequence=candidate.sequence,
        record_count=candidate.record_count,
        payload_sha256=candidate.payload_sha256,
        contract_sha256=candidate.contract_sha256,
        contract_evidence_kind=candidate.contract_evidence_kind,
        provenance_sha256=candidate.provenance_sha256,
        schema_version=candidate.schema_version,
        event_version=_EVENT_VERSION,
        event_json=event_json,
        event_sha256=event_sha256,
        published_at_us=candidate.published_at_us,
    )


def _find_existing_event(
    connection: Connection,
    table: Table,
    expected: _PublicationOutboxEvent,
) -> RowMapping | None:
    """Read either unique event identity using a locking/current read.

    Args:
        connection: The active publisher connection.
        table: The outbox table bound to the publisher metadata.
        expected: Event identity to find after a uniqueness race.

    Returns:
        The single matching event row, or ``None`` when the failed insert had
        another cause.

    Raises:
        _PublicationOutboxConflictError: The two unique identities resolve to
            different event rows.
    """
    rows = (
        connection.execute(
            select(table)
            .where(
                or_(
                    table.c.publication_id == expected.publication_id,
                    (table.c.dataset_id == expected.dataset_id)
                    & (table.c.publication_sequence == expected.sequence),
                )
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    if len(rows) > 1:
        raise _PublicationOutboxConflictError(
            "publication ID and dataset sequence are bound to different outbox events"
        )
    return rows[0] if rows else None


def _require_exact_event_replay(
    row: Mapping[str, Any],
    expected: _PublicationOutboxEvent,
) -> None:
    """Accept an exact uniqueness-race replay and reject any identity conflict.

    Args:
        row: Existing database row found by publication ID or dataset sequence.
        expected: Event prepared from the publication just inserted.

    Raises:
        _PublicationOutboxConflictError: Stored event fields or digest differ.
    """
    try:
        candidate = _normalize_candidate(
            _PublicationOutboxCandidate(
                publication_id=row["publication_id"],
                dataset_id=row["dataset_id"],
                sequence=row["publication_sequence"],
                record_count=row["record_count"],
                payload_sha256=row["payload_sha256"],
                contract_sha256=row["contract_sha256"],
                contract_evidence_kind=row["contract_evidence_kind"],
                provenance_sha256=row["provenance_sha256"],
                schema_version=row["schema_version"],
                published_at_us=row["published_at_us"],
            )
        )
        event_json = row["event_json"]
        event_sha256 = row["event_sha256"]
        if not isinstance(event_json, str) or not isinstance(event_sha256, str):
            raise ValueError("event content columns must be text")
        event_bytes = event_json.encode("utf-8")
        if (
            type(row["event_version"]) is not int
            or row["event_version"] != _EVENT_VERSION
            or len(event_bytes) > _MAX_EVENT_BYTES
            or event_json != _canonical_json(_event_payload(candidate))
            or event_sha256 != hashlib.sha256(_EVENT_HASH_DOMAIN + event_bytes).hexdigest()
        ):
            raise ValueError("event canonical data does not match the stored columns")
        actual = _PublicationOutboxEvent(
            publication_id=candidate.publication_id,
            dataset_id=candidate.dataset_id,
            sequence=candidate.sequence,
            record_count=candidate.record_count,
            payload_sha256=candidate.payload_sha256,
            contract_sha256=candidate.contract_sha256,
            contract_evidence_kind=candidate.contract_evidence_kind,
            provenance_sha256=candidate.provenance_sha256,
            schema_version=candidate.schema_version,
            event_version=row["event_version"],
            event_json=event_json,
            event_sha256=event_sha256,
            published_at_us=candidate.published_at_us,
        )
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise _PublicationOutboxConflictError(
            "persisted publication outbox event failed canonical integrity checks"
        ) from exc
    if actual != expected:
        raise _PublicationOutboxConflictError(
            "outbox identity already exists with different frozen publication content"
        )


def _require_supported_connection(connection: Connection) -> None:
    """Accept only SQLite or MySQL 8.0.16+ for publication plus outbox writes.

    Args:
        connection: SQLAlchemy connection whose dialect will persist the event.

    Raises:
        _PublicationOutboxError: The database dialect/version lacks reviewed DDL semantics.
    """
    dialect = connection.dialect
    if dialect.name == "sqlite":
        return
    if dialect.name != "mysql" or getattr(dialect, "is_mariadb", False):
        raise _PublicationOutboxError("publication outbox supports SQLite and MySQL 8.0.16+ only")
    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise _PublicationOutboxError(
            "publication outbox requires MySQL 8.0.16 or later within MySQL 8"
        )
    parameters = dict(
        zip(
            ("publication_heads", "publications", "fact_versions", "outbox"),
            _MYSQL_TRANSACTIONAL_TABLES,
            strict=True,
        )
    )
    try:
        rows = connection.execute(_MYSQL_TRANSACTIONAL_TABLES_QUERY, parameters).mappings().all()
        engines: dict[str, object] = {}
        for row in rows:
            table_name = row["table_name"]
            if not isinstance(table_name, str) or table_name in engines:
                raise _PublicationOutboxError(
                    "publication outbox found duplicate or invalid MySQL table metadata"
                )
            engines[table_name] = row["engine"]
        if set(engines) != set(_MYSQL_TRANSACTIONAL_TABLES):
            raise _PublicationOutboxError(
                "publication outbox could not verify all four MySQL transaction tables"
            )
        for table_name in _MYSQL_TRANSACTIONAL_TABLES:
            engine = engines[table_name]
            if not isinstance(engine, str) or engine.casefold() != "innodb":
                raise _PublicationOutboxError(
                    f"publication outbox requires {table_name} to use InnoDB"
                )
    except _PublicationOutboxError:
        raise
    except Exception as exc:
        raise _PublicationOutboxError(
            "publication outbox could not verify MySQL table engines; refusing writes"
        ) from exc


def _normalize_candidate(candidate: _PublicationOutboxCandidate) -> _PublicationOutboxCandidate:
    """Validate all values frozen into a publication event.

    Args:
        candidate: Candidate produced from one persisted publication row.

    Returns:
        The same immutable candidate after validation.

    Raises:
        TypeError: The candidate or an integer field has the wrong type.
        ValueError: An identity, digest, evidence-kind, or bounded value is invalid.
    """
    if not isinstance(candidate, _PublicationOutboxCandidate):
        raise TypeError("candidate must be a _PublicationOutboxCandidate")
    if not isinstance(candidate.publication_id, str) or not _PUBLICATION_ID.fullmatch(
        candidate.publication_id
    ):
        raise ValueError("publication_id must be a lowercase 32-character hex identifier")
    if (
        not isinstance(candidate.dataset_id, str)
        or not candidate.dataset_id.strip()
        or len(candidate.dataset_id) > 128
        or candidate.dataset_id != candidate.dataset_id.strip()
        or any(ord(char) < 32 for char in candidate.dataset_id)
    ):
        raise ValueError("dataset_id must be a bounded, single-line identifier")
    if type(candidate.sequence) is not int or not 1 <= candidate.sequence <= _MAX_SEQUENCE:
        raise ValueError("sequence must be a positive signed 64-bit integer")
    if (
        type(candidate.record_count) is not int
        or not 0 <= candidate.record_count <= _MAX_RECORD_COUNT
    ):
        raise ValueError("record_count must be a non-negative signed 32-bit integer")
    for name, value in (
        ("payload_sha256", candidate.payload_sha256),
        ("provenance_sha256", candidate.provenance_sha256),
    ):
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(f"{name} must be a lowercase 64-character SHA-256 digest")
    if candidate.contract_sha256 is not None and (
        not isinstance(candidate.contract_sha256, str)
        or not _SHA256.fullmatch(candidate.contract_sha256)
    ):
        raise ValueError("contract_sha256 must be absent or a lowercase SHA-256 digest")
    expected_kind = (
        _EVIDENCE_CALLER_SUPPLIED if candidate.contract_sha256 is not None else _EVIDENCE_ABSENT
    )
    if candidate.contract_evidence_kind != expected_kind:
        raise ValueError("contract evidence kind does not match the frozen contract digest")
    if (
        not isinstance(candidate.schema_version, str)
        or not candidate.schema_version.strip()
        or len(candidate.schema_version) > 64
        or candidate.schema_version != candidate.schema_version.strip()
        or any(ord(char) < 32 for char in candidate.schema_version)
    ):
        raise ValueError("schema_version must be a bounded, single-line label")
    if (
        type(candidate.published_at_us) is not int
        or not 0 <= candidate.published_at_us <= _MAX_TIMESTAMP_US
    ):
        raise ValueError("published_at_us must be a valid non-negative UTC epoch timestamp")
    return candidate


def _event_payload(candidate: _PublicationOutboxCandidate) -> dict[str, object]:
    """Build the stable, versioned event envelope.

    Args:
        candidate: Validated frozen publication summary.

    Returns:
        JSON-compatible event body with explicit contract evidence semantics.
    """
    return {
        "contract_evidence_kind": candidate.contract_evidence_kind,
        "contract_sha256": candidate.contract_sha256,
        "dataset_id": candidate.dataset_id,
        "event_type": _EVENT_TYPE,
        "event_version": _EVENT_VERSION,
        "payload_sha256": candidate.payload_sha256,
        "provenance_sha256": candidate.provenance_sha256,
        "publication_id": candidate.publication_id,
        "publication_sequence": candidate.sequence,
        "published_at_us": candidate.published_at_us,
        "record_count": candidate.record_count,
        "schema_version": candidate.schema_version,
    }


def _event_values(event: _PublicationOutboxEvent) -> dict[str, object]:
    """Return the database columns for one prepared immutable event.

    Args:
        event: Canonical event built from a newly inserted publication row.

    Returns:
        A fresh mapping suitable for a parameterized SQLAlchemy insert.
    """
    return {
        "publication_id": event.publication_id,
        "dataset_id": event.dataset_id,
        "publication_sequence": event.sequence,
        "record_count": event.record_count,
        "payload_sha256": event.payload_sha256,
        "contract_sha256": event.contract_sha256,
        "contract_evidence_kind": event.contract_evidence_kind,
        "provenance_sha256": event.provenance_sha256,
        "schema_version": event.schema_version,
        "event_version": event.event_version,
        "event_json": event.event_json,
        "event_sha256": event.event_sha256,
        "published_at_us": event.published_at_us,
    }


def _canonical_json(value: object) -> str:
    """Encode deterministic compact UTF-8 JSON and reject non-finite numbers.

    Args:
        value: JSON-compatible value to encode.

    Returns:
        Canonical JSON text using sorted keys and compact separators.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
