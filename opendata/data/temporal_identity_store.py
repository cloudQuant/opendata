"""Append-only persistence for canonical temporal identity snapshots.

The store uses only an explicitly injected, already migrated SQLite or MySQL
engine. It never creates schema, loads application settings, reads credentials,
or selects a snapshot implicitly. The caller supplies a digest obtained through
an independent trusted channel on both writes and reads.

The outer SHA-256 covers every canonical codec byte, including the codec's
embedded ``content_sha256``. It is separate from that embedded digest. Neither
digest authenticates ``authority_id`` or ``registry_id`` or establishes source
authenticity, authorization, universe completeness, or historical coverage;
callers must maintain a trusted mapping for those claims.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from opendata.data.temporal_instrument_identity import TemporalIdentityRegistry
from opendata.data.temporal_instrument_identity_snapshot import (
    MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES,
    TemporalIdentitySnapshotError,
    decode_temporal_identity_snapshot,
    encode_temporal_identity_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_SNAPSHOT_CHUNK_BYTES = 262_144
MAX_SNAPSHOT_CHUNKS = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)

_SELECT_SNAPSHOT = text(
    """
    SELECT s.authority_id, s.registry_id, s.reference_version,
           s.total_bytes, s.chunk_count, s.snapshot_sha256, s.created_at_us,
           c.chunk_no,
           CASE WHEN LENGTH(c.chunk_bytes) <= 262144
                THEN c.chunk_bytes ELSE NULL END AS chunk_bytes,
           LENGTH(c.chunk_bytes) AS chunk_length, c.chunk_sha256
      FROM od_temporal_identity_snapshots AS s
      LEFT JOIN od_temporal_identity_snapshot_chunks AS c
        ON c.authority_id = s.authority_id
       AND c.registry_id = s.registry_id
       AND c.reference_version = s.reference_version
     WHERE s.authority_id = :authority_id
       AND s.registry_id = :registry_id
       AND s.reference_version = :reference_version
     ORDER BY c.chunk_no
     LIMIT 257
    """
)
_INSERT_SNAPSHOT = text(
    """
    INSERT INTO od_temporal_identity_snapshots
      (authority_id, registry_id, reference_version, total_bytes,
       chunk_count, snapshot_sha256, created_at_us)
    VALUES
      (:authority_id, :registry_id, :reference_version, :total_bytes,
       :chunk_count, :snapshot_sha256, :created_at_us)
    """
)
_INSERT_CHUNK = text(
    """
    INSERT INTO od_temporal_identity_snapshot_chunks
      (authority_id, registry_id, reference_version, chunk_no,
       chunk_bytes, chunk_sha256)
    VALUES
      (:authority_id, :registry_id, :reference_version, :chunk_no,
       :chunk_bytes, :chunk_sha256)
    """
)


class TemporalIdentitySnapshotStoreError(RuntimeError):
    """Base error for persistence failures with fixed, non-sensitive messages."""


class TemporalIdentitySnapshotValidationError(ValueError):
    """Raised when a caller-supplied key, registry, or digest is invalid."""


class TemporalIdentitySnapshotConflictError(TemporalIdentitySnapshotStoreError):
    """Raised when an immutable version key already contains different bytes."""


class TemporalIdentitySnapshotNotFoundError(TemporalIdentitySnapshotStoreError):
    """Raised when an explicitly requested version has no stored snapshot."""


class TemporalIdentitySnapshotCorruptError(TemporalIdentitySnapshotStoreError):
    """Raised when persisted header, chunks, digest, or codec data disagree."""


class TemporalIdentitySnapshotDigestMismatchError(TemporalIdentitySnapshotStoreError):
    """Raised when stored bytes do not match the separately supplied digest."""


@dataclass(frozen=True, slots=True)
class TemporalIdentitySnapshotReceipt:
    """Metadata for one successfully committed immutable snapshot version."""

    authority_id: str
    registry_id: str
    reference_version: str
    snapshot_sha256: str
    total_bytes: int
    chunk_count: int
    created_at_us: int
    already_existed: bool


@dataclass(frozen=True, slots=True)
class _StoredSnapshot:
    """Validated complete bytes and metadata read from one database snapshot."""

    raw: bytes
    registry: TemporalIdentityRegistry
    snapshot_sha256: bytes
    total_bytes: int
    chunk_count: int
    created_at_us: int


class TemporalIdentitySnapshotStore:
    """Persist full canonical registry versions in pre-created data tables.

    SQLite writes acquire ``BEGIN IMMEDIATE`` before checking a version key.
    MySQL relies on InnoDB transactions and the schema's composite primary key
    to arbitrate concurrent inserts. Existing versions are immutable: identical
    canonical bytes are idempotent, and different bytes conflict. There is no
    update, delete, or latest-version operation. A MySQL engine must already
    target the schema admitted by migration 0021 (MySQL 8.0.16+ with InnoDB);
    construction deliberately performs no connection or server-version probe.
    """

    def __init__(self, engine: Engine) -> None:
        """Bind an explicit synchronous SQLite or MySQL engine without I/O."""
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if engine.dialect.name not in {"sqlite", "mysql"} or getattr(
            engine.dialect, "is_mariadb", False
        ):
            raise ValueError(
                "temporal identity storage requires SQLite or migration-approved MySQL"
            )
        self._engine = engine

    def put(
        self,
        *,
        authority_id: str,
        registry_id: str,
        registry: TemporalIdentityRegistry,
        expected_sha256: str,
    ) -> TemporalIdentitySnapshotReceipt:
        """Atomically insert one complete registry snapshot or return an exact replay.

        The expected digest must come from a caller-controlled channel separate
        from this database. It is compared to the SHA-256 of the complete codec
        bytes, not to the codec's embedded ``content_sha256``.

        Args:
            authority_id: Stable ASCII namespace key, 1 to 128 characters.
            registry_id: Stable ASCII registry key, 1 to 128 characters.
            registry: Explicitly supplied immutable domain registry.
            expected_sha256: Independently trusted lowercase SHA-256 hex digest.

        Returns:
            The immutable version receipt; ``already_existed`` marks exact replay.

        Raises:
            TemporalIdentitySnapshotValidationError: If input or digest is invalid.
            TemporalIdentitySnapshotConflictError: If the version key has other bytes.
            TemporalIdentitySnapshotStoreError: If persistence fails.
        """
        authority = _validate_identifier(authority_id, "authority_id")
        registry_key = _validate_identifier(registry_id, "registry_id")
        expected = _validate_expected_sha256(expected_sha256)
        if type(registry) is not TemporalIdentityRegistry:
            raise TemporalIdentitySnapshotValidationError(
                "registry must be a TemporalIdentityRegistry"
            )
        try:
            raw = encode_temporal_identity_snapshot(registry)
            decoded = decode_temporal_identity_snapshot(raw)
        except TemporalIdentitySnapshotError:
            raise TemporalIdentitySnapshotValidationError(
                "registry cannot be encoded as a canonical temporal identity snapshot"
            ) from None
        if decoded != registry:
            raise TemporalIdentitySnapshotValidationError(
                "registry does not round-trip through its canonical snapshot codec"
            )
        version = _validate_identifier(registry.reference_version, "reference_version")
        digest = hashlib.sha256(raw).digest()
        if not hmac.compare_digest(digest.hex(), expected):
            raise TemporalIdentitySnapshotValidationError(
                "expected digest does not match the complete canonical snapshot bytes"
            )
        if not raw or len(raw) > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
            raise TemporalIdentitySnapshotValidationError(
                "snapshot size is outside its fixed bound"
            )
        chunk_count = (len(raw) + MAX_SNAPSHOT_CHUNK_BYTES - 1) // MAX_SNAPSHOT_CHUNK_BYTES
        if not 1 <= chunk_count <= MAX_SNAPSHOT_CHUNKS:
            raise TemporalIdentitySnapshotValidationError("snapshot exceeds the fixed chunk limit")

        key = (authority, registry_key, version)
        try:
            with self._transaction(write=True) as connection:
                existing = self._load(connection, key)
                if existing is not None:
                    if hmac.compare_digest(existing.raw, raw):
                        return _receipt(key, existing, already_existed=True)
                    raise TemporalIdentitySnapshotConflictError(
                        "immutable snapshot version already contains different bytes"
                    )
                created_at_us = time.time_ns() // 1_000
                if created_at_us < 0:
                    raise TemporalIdentitySnapshotStoreError("snapshot clock is invalid")
                connection.execute(
                    _INSERT_SNAPSHOT,
                    {
                        "authority_id": authority,
                        "registry_id": registry_key,
                        "reference_version": version,
                        "total_bytes": len(raw),
                        "chunk_count": chunk_count,
                        "snapshot_sha256": digest,
                        "created_at_us": created_at_us,
                    },
                )
                chunk_rows = _chunk_rows(key, raw)
                connection.execute(_INSERT_CHUNK, chunk_rows)
                stored = self._load(connection, key)
                if stored is None or not hmac.compare_digest(stored.raw, raw):
                    raise TemporalIdentitySnapshotCorruptError(
                        "snapshot write did not read back as the committed canonical bytes"
                    )
                return _receipt(key, stored, already_existed=False)
        except IntegrityError:
            # A concurrent MySQL insert may win after this transaction's initial
            # read. Re-read after rollback and accept only a fully valid exact replay.
            existing = self._load_in_new_transaction(key)
            if existing is not None and hmac.compare_digest(existing.raw, raw):
                return _receipt(key, existing, already_existed=True)
            if existing is not None:
                raise TemporalIdentitySnapshotConflictError(
                    "immutable snapshot version already contains different bytes"
                ) from None
            raise TemporalIdentitySnapshotStoreError("snapshot insert failed") from None
        except SQLAlchemyError:
            raise TemporalIdentitySnapshotStoreError("snapshot storage operation failed") from None

    def get(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> TemporalIdentityRegistry:
        """Read and validate one explicitly pinned snapshot without returning partial data.

        The expected digest is mandatory and must be supplied independently of
        the stored header. A single consistent SQL statement reads the header
        and ordered chunks inside a database transaction before any registry is
        returned.

        Args:
            authority_id: Exact stable authority key.
            registry_id: Exact stable registry key.
            reference_version: Explicit immutable version; no latest lookup exists.
            expected_sha256: Independently trusted lowercase full-byte SHA-256.

        Returns:
            A fully decoded and domain-validated registry.

        Raises:
            TemporalIdentitySnapshotNotFoundError: If the exact version is absent.
            TemporalIdentitySnapshotDigestMismatchError: If bytes differ from the pin.
            TemporalIdentitySnapshotCorruptError: If persisted content is inconsistent.
            TemporalIdentitySnapshotStoreError: If database access fails.
        """
        return self._get_validated_snapshot(
            authority_id=authority_id,
            registry_id=registry_id,
            reference_version=reference_version,
            expected_sha256=expected_sha256,
        ).registry

    def get_bytes(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> bytes:
        """Return complete canonical bytes after full header, chunk, codec and pin checks.

        This read has the same independent full-byte digest requirement and
        corruption checks as :meth:`get`. It does not return partial chunks or
        trust the digest stored in the database as its own expected value.

        Args:
            authority_id: Exact stable authority key.
            registry_id: Exact stable registry key.
            reference_version: Explicit immutable version; no latest lookup exists.
            expected_sha256: Independently trusted lowercase full-byte SHA-256.

        Returns:
            The exact complete canonical snapshot bytes validated from storage.

        Raises:
            TemporalIdentitySnapshotNotFoundError: If the exact version is absent.
            TemporalIdentitySnapshotDigestMismatchError: If bytes differ from the pin.
            TemporalIdentitySnapshotCorruptError: If persisted content is inconsistent.
            TemporalIdentitySnapshotStoreError: If database access fails.
        """
        return self._get_validated_snapshot(
            authority_id=authority_id,
            registry_id=registry_id,
            reference_version=reference_version,
            expected_sha256=expected_sha256,
        ).raw

    def _get_validated_snapshot(
        self,
        *,
        authority_id: str,
        registry_id: str,
        reference_version: str,
        expected_sha256: str,
    ) -> _StoredSnapshot:
        """Read one explicit version and validate all bytes against its external pin."""
        key = (
            _validate_identifier(authority_id, "authority_id"),
            _validate_identifier(registry_id, "registry_id"),
            _validate_identifier(reference_version, "reference_version"),
        )
        expected = _validate_expected_sha256(expected_sha256)
        try:
            with self._transaction(write=False) as connection:
                stored = self._load(connection, key)
        except SQLAlchemyError:
            raise TemporalIdentitySnapshotStoreError("snapshot storage operation failed") from None
        if stored is None:
            raise TemporalIdentitySnapshotNotFoundError("requested snapshot version was not found")
        if not hmac.compare_digest(stored.snapshot_sha256.hex(), expected):
            raise TemporalIdentitySnapshotDigestMismatchError(
                "stored snapshot does not match the independently supplied digest"
            )
        return stored

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[Connection]:
        """Open an explicit snapshot transaction appropriate to the selected dialect."""
        if self._engine.dialect.name == "sqlite":
            with self._engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
                try:
                    yield connection
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            return
        with self._engine.begin() as connection:
            yield connection

    def _load_in_new_transaction(self, key: tuple[str, str, str]) -> _StoredSnapshot | None:
        """Read a candidate row after a failed insert transaction has rolled back."""
        try:
            with self._transaction(write=False) as connection:
                return self._load(connection, key)
        except SQLAlchemyError:
            raise TemporalIdentitySnapshotStoreError("snapshot storage operation failed") from None

    @staticmethod
    def _load(connection: Connection, key: tuple[str, str, str]) -> _StoredSnapshot | None:
        """Read and validate a complete stored version from one SQL statement."""
        authority_id, registry_id, reference_version = key
        result = connection.execute(
            _SELECT_SNAPSHOT,
            {
                "authority_id": authority_id,
                "registry_id": registry_id,
                "reference_version": reference_version,
            },
        )
        rows = result.fetchmany(MAX_SNAPSHOT_CHUNKS + 1)
        if not rows:
            return None
        if len(rows) > MAX_SNAPSHOT_CHUNKS:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot has too many chunks")
        header = rows[0]._mapping
        _validate_header(header, key)
        total_bytes = header["total_bytes"]
        expected_count = header["chunk_count"]
        header_digest = bytes(header["snapshot_sha256"])
        created_at_us = header["created_at_us"]
        if not 1 <= total_bytes <= MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot byte count is invalid")
        if not 1 <= expected_count <= MAX_SNAPSHOT_CHUNKS:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot chunk count is invalid")
        if len(header_digest) != 32:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot digest is malformed")
        if type(created_at_us) is not int or created_at_us < 0:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot timestamp is invalid")

        chunks: list[bytes] = []
        total = 0
        for expected_no, row in enumerate(rows):
            values = row._mapping
            _validate_repeated_header(values, header, key)
            chunk_no = values["chunk_no"]
            chunk_bytes = values["chunk_bytes"]
            chunk_length = values["chunk_length"]
            chunk_digest = values["chunk_sha256"]
            if type(chunk_no) is not int or chunk_no != expected_no:
                raise TemporalIdentitySnapshotCorruptError(
                    "stored snapshot chunks are not consecutively ordered"
                )
            if type(chunk_length) is not int or not 1 <= chunk_length <= MAX_SNAPSHOT_CHUNK_BYTES:
                raise TemporalIdentitySnapshotCorruptError(
                    "stored snapshot chunk length is invalid"
                )
            if type(chunk_bytes) is not bytes or len(chunk_bytes) != chunk_length:
                raise TemporalIdentitySnapshotCorruptError(
                    "stored snapshot chunk length is invalid"
                )
            if type(chunk_digest) is not bytes or len(chunk_digest) != 32:
                raise TemporalIdentitySnapshotCorruptError(
                    "stored snapshot chunk digest is malformed"
                )
            computed_chunk_digest = hashlib.sha256(chunk_bytes).digest()
            if not hmac.compare_digest(computed_chunk_digest, chunk_digest):
                raise TemporalIdentitySnapshotCorruptError(
                    "stored snapshot chunk digest does not match"
                )
            total += len(chunk_bytes)
            if total > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
                raise TemporalIdentitySnapshotCorruptError("stored snapshot exceeds its byte limit")
            chunks.append(chunk_bytes)
        if len(chunks) != expected_count:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot chunk count does not match")
        if total != total_bytes:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot byte count does not match")
        raw = b"".join(chunks)
        computed_snapshot_digest = hashlib.sha256(raw).digest()
        if not hmac.compare_digest(computed_snapshot_digest, header_digest):
            raise TemporalIdentitySnapshotCorruptError("stored snapshot digest does not match")
        try:
            registry = decode_temporal_identity_snapshot(raw)
        except TemporalIdentitySnapshotError:
            raise TemporalIdentitySnapshotCorruptError(
                "stored bytes are not a valid canonical temporal identity snapshot"
            ) from None
        if registry.reference_version != reference_version:
            raise TemporalIdentitySnapshotCorruptError(
                "stored codec version does not match its database key"
            )
        return _StoredSnapshot(
            raw=raw,
            registry=registry,
            snapshot_sha256=header_digest,
            total_bytes=total_bytes,
            chunk_count=expected_count,
            created_at_us=created_at_us,
        )


def _validate_identifier(value: str, name: str) -> str:
    """Validate one bounded ASCII database key without normalization."""
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TemporalIdentitySnapshotValidationError(
            f"{name} must be a 1-128 character ASCII identifier"
        )
    return value


def _validate_expected_sha256(value: str) -> str:
    """Require an independently supplied lowercase hexadecimal SHA-256 value."""
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TemporalIdentitySnapshotValidationError(
            "expected_sha256 must be 64 lowercase hexadecimal characters"
        )
    return value


def _validate_header(header: RowMapping, key: tuple[str, str, str]) -> None:
    """Check stored identifiers and scalar header types before using limits."""
    if (header["authority_id"], header["registry_id"], header["reference_version"]) != key:
        raise TemporalIdentitySnapshotCorruptError("stored snapshot key does not match request")
    for column in ("total_bytes", "chunk_count", "created_at_us"):
        if type(header[column]) is not int:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot header is malformed")
    if not isinstance(header["snapshot_sha256"], (bytes, bytearray, memoryview)):
        raise TemporalIdentitySnapshotCorruptError("stored snapshot digest is malformed")


def _validate_repeated_header(
    row: RowMapping, header: RowMapping, key: tuple[str, str, str]
) -> None:
    """Ensure joined rows belong to the requested, unchanged header tuple."""
    if (row["authority_id"], row["registry_id"], row["reference_version"]) != key:
        raise TemporalIdentitySnapshotCorruptError("stored snapshot key does not match request")
    for column in (
        "total_bytes",
        "chunk_count",
        "snapshot_sha256",
        "created_at_us",
    ):
        if row[column] != header[column]:
            raise TemporalIdentitySnapshotCorruptError("stored snapshot header rows disagree")


def _chunk_rows(key: tuple[str, str, str], raw: bytes) -> list[dict[str, object]]:
    """Build a fixed, bounded list of 256 KiB-or-smaller chunk insert mappings."""
    authority_id, registry_id, reference_version = key
    rows: list[dict[str, object]] = []
    for chunk_no, offset in enumerate(range(0, len(raw), MAX_SNAPSHOT_CHUNK_BYTES)):
        chunk = raw[offset : offset + MAX_SNAPSHOT_CHUNK_BYTES]
        rows.append(
            {
                "authority_id": authority_id,
                "registry_id": registry_id,
                "reference_version": reference_version,
                "chunk_no": chunk_no,
                "chunk_bytes": chunk,
                "chunk_sha256": hashlib.sha256(chunk).digest(),
            }
        )
    return rows


def _receipt(
    key: tuple[str, str, str],
    stored: _StoredSnapshot,
    *,
    already_existed: bool,
) -> TemporalIdentitySnapshotReceipt:
    """Construct a public metadata receipt without exposing stored bytes."""
    return TemporalIdentitySnapshotReceipt(
        authority_id=key[0],
        registry_id=key[1],
        reference_version=key[2],
        snapshot_sha256=stored.snapshot_sha256.hex(),
        total_bytes=stored.total_bytes,
        chunk_count=stored.chunk_count,
        created_at_us=stored.created_at_us,
        already_existed=already_existed,
    )


__all__ = [
    "MAX_SNAPSHOT_CHUNK_BYTES",
    "MAX_SNAPSHOT_CHUNKS",
    "TemporalIdentitySnapshotConflictError",
    "TemporalIdentitySnapshotCorruptError",
    "TemporalIdentitySnapshotDigestMismatchError",
    "TemporalIdentitySnapshotNotFoundError",
    "TemporalIdentitySnapshotReceipt",
    "TemporalIdentitySnapshotStore",
    "TemporalIdentitySnapshotStoreError",
    "TemporalIdentitySnapshotValidationError",
]
