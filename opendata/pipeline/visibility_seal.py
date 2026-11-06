"""Post-commit publication visibility seal with a persistent monotonic sequence.

A publication's in-transaction ``published_at`` / ``published_at_us`` value is
written by the publisher host clock while its transaction is still open. It
proves nothing about commit visibility: the transaction can still roll back, the
warehouse fact partition can still be incomplete, and the host clock can be
wrong. This module keeps that field exactly as it is and labels it
non-probative; the seal below is a separate, strictly post-commit fact.

Seal protocol (iteration 204-A04-04):

1. The caller commits the publishing transaction (facts, publication pointer,
   outbox event) and the cross-database metadata receiver transaction.
2. ``seal_after_commit`` opens fresh connections, so it observes only durably
   committed rows. It verifies the publication row, the advanced dataset
   publication pointer, the complete warehouse fact partition, and the
   cross-database receipt copy, and re-verifies the frozen receipt digest.
3. Inside the seal transaction, after the sequence ledger row is locked, it reads
   the authoritative UTC clock and fails closed on an untrusted, implausible,
   naive, or rewound clock, writing nothing. Reading the clock under the ledger
   lock is what keeps a concurrent writer's later seal instant from making an
   honest clock look like a rewind.
4. Only then does it allocate the next value of the persistent monotonic
   visibility sequence and insert the seal row plus the sequence-state advance
   in one transaction, so a crash cannot leave a half-sealed publication and a
   rolled-back seal never burns a sequence value.
5. The written seal is read back on a new connection (read-after-commit
   verification) before it is returned.

Concurrency: the sequence advances by compare-and-set on the single state row
plus a unique index on ``visibility_sequence``, so duplicate or backwards
sequence values are refused by the database even when two writers read the same
state. Exact retries replay the original seal and never renumber it.

The surface is default-off. ``VisibilitySealStore`` refuses to write any seal
until a deployment sets ``VISIBILITY_SEAL_ENABLED=true``, which the iteration
ledger permits only after the clock and commit-visibility boundary are verified
in the target environment. The research cutoff query HTTP surface stays closed:
this module exposes store-level reads only, and an unsealed publication can
never appear in them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError

from opendata.data.metadata_receipt_schema import METADATA_PUBLICATION_RECEIPTS
from opendata.pipeline import publication_outbox as _outbox
from opendata.pipeline.publication_store import (
    PUBLICATION_HEADS,
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
)
from opendata.pipeline.run_ledger import _database_clock_us

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy.engine import Connection

MAX_SEAL_PAGE_SIZE = 1_000
_SEAL_VERSION = 1
_MAX_TIMESTAMP_US = 253_402_300_799_999_999
_MAX_SEQUENCE = 9_223_372_036_854_775_807
_MAX_CLOCK_EVIDENCE_BYTES = 4_096
_MAX_PRODUCER_IDENTITY_BYTES = 128
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECONDS_PER_SECOND = 1_000_000
_FACT_SET_HASH_DOMAIN = b"opendata.publication-visibility.fact-set.v1\n"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PUBLICATION_ID = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z", re.ASCII)
_RETRYABLE_LOCK = re.compile(r"locked|deadlock", re.IGNORECASE)
# Database clocks can be coarser than the stored microsecond value (SQLite reports
# milliseconds), so a seal instant may legitimately equal or slightly lead the
# in-transaction publication stamp it follows. Anything clearly earlier is a rewind.
_COMMIT_INSTANT_TOLERANCE_US = 1_000_000
_STATE_ID = 1


def _binary_key(length: int) -> String:
    """Use exact, case-sensitive, no-pad MySQL identity comparisons and SQLite defaults."""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _json_storage() -> Text:
    """Allow the bounded canonical evidence JSON to exceed MySQL's 64 KiB TEXT cap."""
    return Text().with_variant(MEDIUMTEXT(), "mysql")


def make_visibility_seal_tables(metadata: MetaData) -> tuple[Table, Table]:
    """Register seal storage and its monotonic ledger in the caller's metadata registry.

    A seal carries a RESTRICT foreign key to ``od_publications``, so it must be declared
    in the same registry as the publication tables it observes; warehouse revision 0022
    creates it there too. The call is idempotent, so a fixture and the runtime always
    share one table definition.

    Args:
        metadata: Registry containing, or about to contain, ``od_publications``.

    Returns:
        The seal table and the single-row visibility sequence ledger table.

    Raises:
        ValueError: One table name is registered with a different definition.
    """
    seals = metadata.tables.get("od_publication_visibility_seals")
    state = metadata.tables.get("od_visibility_sequence_state")
    if seals is not None and state is not None:
        return seals, state
    if seals is not None or state is not None:
        raise ValueError("visibility seal storage is only partly registered")
    seals = Table(
        "od_publication_visibility_seals",
        metadata,
        Column(
            "publication_id",
            String(32),
            ForeignKey(
                "od_publications.publication_id",
                name="fk_od_visibility_seal_publication",
                ondelete="RESTRICT",
            ),
            primary_key=True,
            nullable=False,
        ),
        Column("dataset_id", _binary_key(128), nullable=False),
        Column("publication_sequence", BigInteger, nullable=False),
        Column("record_count", BigInteger, nullable=False),
        Column("schema_version", String(64), nullable=False),
        Column("producer_identity", _binary_key(_MAX_PRODUCER_IDENTITY_BYTES), nullable=False),
        Column("payload_sha256", String(64), nullable=False),
        Column("fact_set_sha256", String(64), nullable=False),
        Column("receipt_event_sha256", String(64), nullable=False),
        Column("visibility_sequence", BigInteger, nullable=False),
        Column("sealed_at_us", BigInteger, nullable=False),
        Column("clock_evidence_json", _json_storage(), nullable=False),
        Column("seal_version", Integer, nullable=False),
        CheckConstraint("publication_sequence > 0", name="ck_od_visibility_seal_pub_sequence"),
        CheckConstraint("record_count >= 0", name="ck_od_visibility_seal_record_count"),
        CheckConstraint(
            "length(schema_version) BETWEEN 1 AND 64", name="ck_od_visibility_seal_schema"
        ),
        CheckConstraint(
            "length(producer_identity) BETWEEN 1 AND 128",
            name="ck_od_visibility_seal_producer_len",
        ),
        CheckConstraint("length(payload_sha256) = 64", name="ck_od_visibility_seal_payload_sha"),
        CheckConstraint("length(fact_set_sha256) = 64", name="ck_od_visibility_seal_fact_sha"),
        CheckConstraint(
            "length(receipt_event_sha256) = 64",
            name="ck_od_visibility_seal_receipt_sha",
        ),
        CheckConstraint(
            f"visibility_sequence BETWEEN 1 AND {_MAX_SEQUENCE}",
            name="ck_od_visibility_seal_sequence",
        ),
        CheckConstraint(
            f"sealed_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
            name="ck_od_visibility_seal_sealed_at",
        ),
        CheckConstraint(
            f"length(clock_evidence_json) BETWEEN 2 AND {_MAX_CLOCK_EVIDENCE_BYTES}",
            name="ck_od_visibility_seal_clock_evidence_size",
        ),
        CheckConstraint("seal_version = 1", name="ck_od_visibility_seal_version"),
        Index("uq_od_visibility_seal_sequence", "visibility_sequence", unique=True),
        Index(
            "uq_od_visibility_seal_dataset_sequence",
            "dataset_id",
            "publication_sequence",
            unique=True,
        ),
        Index("ix_od_visibility_seal_dataset_order", "dataset_id", "visibility_sequence"),
        Index("ix_od_visibility_seal_sealed_at", "sealed_at_us"),
        mysql_engine="InnoDB",
    )
    state = Table(
        "od_visibility_sequence_state",
        metadata,
        Column("state_id", Integer, primary_key=True, nullable=False),
        Column("last_visibility_sequence", BigInteger, nullable=False),
        Column("last_sealed_at_us", BigInteger, nullable=False),
        Column("seal_count", BigInteger, nullable=False),
        Column("updated_at_us", BigInteger, nullable=False),
        CheckConstraint("state_id = 1", name="ck_od_visibility_state_single_row"),
        CheckConstraint(
            f"last_visibility_sequence BETWEEN 0 AND {_MAX_SEQUENCE}",
            name="ck_od_visibility_state_last_sequence",
        ),
        CheckConstraint(
            f"last_sealed_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
            name="ck_od_visibility_state_last_sealed_at",
        ),
        CheckConstraint("seal_count >= 0", name="ck_od_visibility_state_seal_count"),
        CheckConstraint(
            f"updated_at_us BETWEEN 0 AND {_MAX_TIMESTAMP_US}",
            name="ck_od_visibility_state_updated_at",
        ),
        CheckConstraint(
            "seal_count <= last_visibility_sequence",
            name="ck_od_visibility_state_count_bounded",
        ),
        mysql_engine="InnoDB",
    )
    return seals, state


#: The seal tables are declared inside the publication registry so their publication
#: foreign key resolves; this name keeps the seal surface importable on its own.
VISIBILITY_SEAL_METADATA = PUBLICATION_METADATA
VISIBILITY_SEALS, VISIBILITY_SEQUENCE_STATE = make_visibility_seal_tables(VISIBILITY_SEAL_METADATA)


class VisibilitySealError(ValueError):
    """Base error for a refused or inconsistent publication visibility seal."""


class VisibilitySealDisabledError(VisibilitySealError):
    """Raised when the default-off seal surface is used without deployment enablement."""


class VisibilityClockUntrustedError(VisibilitySealError):
    """Raised when the authoritative UTC clock is implausible or disagrees with the host."""


class VisibilityClockRewindError(VisibilityClockUntrustedError):
    """Raised when a seal instant would move the persisted visibility clock backwards."""


class VisibilityCommitVisibilityError(VisibilitySealError):
    """Raised when a fact partition, publication pointer, or receipt is not committed."""


class VisibilitySealConflictError(VisibilitySealError):
    """Raised when an existing seal no longer matches the committed publication evidence."""


class VisibilitySequenceContentionError(VisibilitySealError):
    """Raised when the monotonic sequence stayed contended beyond the bounded retries."""


@dataclass(frozen=True, slots=True)
class VisibilityClockPolicy:
    """Bounds for the trusted-UTC clock gate applied before any seal is written.

    Attributes:
        min_plausible_utc: Oldest seal instant a deployment will accept.
        max_plausible_utc: Newest seal instant a deployment will accept.
        max_host_database_skew_us: Largest tolerated host-clock disagreement with the
            authoritative warehouse clock, in microseconds.
        sequence_attempts: Bounded compare-and-set retries for one seal request.
    """

    min_plausible_utc: datetime = datetime(2020, 1, 1, tzinfo=timezone.utc)
    max_plausible_utc: datetime = datetime(2100, 1, 1, tzinfo=timezone.utc)
    max_host_database_skew_us: int = 2_000_000
    sequence_attempts: int = 8

    def __post_init__(self) -> None:
        """Reject a policy that could not fail closed."""
        for name in ("min_plausible_utc", "max_plausible_utc"):
            value: object = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")
            if value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must use UTC")
        if self.min_plausible_utc >= self.max_plausible_utc:
            raise ValueError("min_plausible_utc must precede max_plausible_utc")
        if (
            type(self.max_host_database_skew_us) is not int
            or not 0 <= (self.max_host_database_skew_us) <= 600_000_000
        ):
            raise ValueError("max_host_database_skew_us must be between 0 and 600 seconds")
        if type(self.sequence_attempts) is not int or not 1 <= self.sequence_attempts <= 32:
            raise ValueError("sequence_attempts must be between 1 and 32")


DEFAULT_VISIBILITY_CLOCK_POLICY = VisibilityClockPolicy()


@dataclass(frozen=True, slots=True)
class VisibilitySealRecord:
    """One durable post-commit visibility seal for one immutable publication."""

    publication_id: str
    dataset_id: str
    publication_sequence: int
    record_count: int
    schema_version: str
    producer_identity: str
    payload_sha256: str
    fact_set_sha256: str
    receipt_event_sha256: str
    visibility_sequence: int
    sealed_at: datetime
    clock_evidence: Mapping[str, Any]
    seal_version: int

    @property
    def sealed_at_rfc3339(self) -> str:
        """Return the seal instant as one explicit RFC 3339 UTC instant."""
        return _rfc3339_utc(self.sealed_at)


@dataclass(frozen=True, slots=True)
class VisibilitySealResult:
    """Outcome of one seal request.

    Attributes:
        record: The durable seal.
        replayed: Whether an already committed seal was returned unchanged.
    """

    record: VisibilitySealRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class VisibilitySequenceState:
    """Persisted monotonic visibility ledger and its clock high-water mark."""

    initialised: bool
    last_visibility_sequence: int
    last_sealed_at: datetime | None
    seal_count: int
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class _CommittedEvidence:
    """Values frozen into a seal after read-after-commit verification."""

    publication_id: str
    dataset_id: str
    publication_sequence: int
    record_count: int
    schema_version: str
    producer_identity: str
    payload_sha256: str
    fact_set_sha256: str
    receipt_event_sha256: str
    published_at_us: int


class _SequenceContentionError(Exception):
    """Internal signal that another writer advanced the visibility sequence."""


class VisibilitySealStore:
    """Seal committed publications into one persistent monotonic visibility ledger."""

    def __init__(
        self,
        warehouse_engine: Engine,
        metadata_engine: Engine,
        *,
        enabled: bool = False,
        clock_policy: VisibilityClockPolicy = DEFAULT_VISIBILITY_CLOCK_POLICY,
        host_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the warehouse and the separate metadata receiver database.

        Args:
            warehouse_engine: Engine whose schema holds ``od_publications`` and the seal
                tables created by warehouse revision 0022.
            metadata_engine: Engine of the control database holding cross-database
                publication receipt copies.
            enabled: Must be ``True`` explicitly; the default-off surface writes nothing.
            clock_policy: Trusted-UTC clock bounds and bounded compare-and-set retries.
            host_clock: Optional deployment host clock, used only as a trust signal.

        Raises:
            TypeError: An engine is not a synchronous SQLAlchemy Engine.
            ValueError: The clock policy is invalid.
        """
        if not isinstance(warehouse_engine, Engine):
            raise TypeError("warehouse_engine must be a synchronous SQLAlchemy Engine")
        if not isinstance(metadata_engine, Engine):
            raise TypeError("metadata_engine must be a synchronous SQLAlchemy Engine")
        self._warehouse = warehouse_engine
        self._metadata = metadata_engine
        self._enabled = enabled is True
        self._policy = clock_policy
        self._host_clock = host_clock

    @property
    def enabled(self) -> bool:
        """Whether this deployment may write seals at all."""
        return self._enabled

    def seal_after_commit(
        self, *, publication_id: str, producer_identity: str
    ) -> VisibilitySealResult:
        """Seal one publication whose facts, pointer, and receipt are all committed.

        The caller must already have committed the publishing transaction; this method
        reads on new connections and therefore cannot observe its own uncommitted writes.
        Nothing is written unless every gate passes, and a refusal never consumes a
        visibility sequence value.

        Args:
            publication_id: Committed publication identity.
            producer_identity: Receipt producer pin used in the metadata database.

        Returns:
            The durable seal and whether an identical seal already existed.

        Raises:
            VisibilitySealDisabledError: The default-off surface is not enabled.
            VisibilityCommitVisibilityError: Facts, pointer, or receipt are not committed.
            VisibilityClockUntrustedError: The clock is implausible, naive, or skewed.
            VisibilityClockRewindError: The clock moved backwards against persisted evidence.
            VisibilitySealConflictError: An existing seal no longer matches the evidence.
            VisibilitySequenceContentionError: The sequence stayed busy beyond the bound.
            ValueError: An argument is malformed.
            SQLAlchemyError: The database rejected a statement or transaction.
        """
        if not self._enabled:
            raise VisibilitySealDisabledError(
                "visibility sealing is disabled; enable VISIBILITY_SEAL_ENABLED only after the "
                "clock and commit-visibility boundary are verified in the target environment"
            )
        identifier = _publication_identifier(publication_id)
        producer = _producer_identity(producer_identity)
        with self._warehouse.connect() as connection:
            evidence = self._verify_committed_publication(
                connection, publication_id=identifier, producer_identity=producer
            )
        replay = self._replay_if_sealed(evidence)
        if replay is not None:
            return replay
        result = self._seal_with_bounded_retries(evidence)
        if result is None:
            raise VisibilitySequenceContentionError(
                "the visibility sequence stayed contended beyond the bounded retries; "
                "no seal was written"
            )
        return result

    def get_seal(self, publication_id: str) -> VisibilitySealRecord | None:
        """Return the durable seal for one publication, or ``None`` when unsealed.

        Args:
            publication_id: Publication identity.

        Returns:
            The seal row, or ``None`` when the publication has no visibility seal.
        """
        identifier = _publication_identifier(publication_id)
        with self._warehouse.connect() as connection:
            row = (
                connection.execute(
                    select(VISIBILITY_SEALS).where(VISIBILITY_SEALS.c.publication_id == identifier)
                )
                .mappings()
                .first()
            )
        return _seal_from_row(row) if row is not None else None

    def list_seals(
        self,
        dataset_id: str,
        *,
        through_visibility_sequence: int | None = None,
        limit: int = MAX_SEAL_PAGE_SIZE,
    ) -> tuple[VisibilitySealRecord, ...]:
        """Return sealed publications in ascending visibility-sequence order.

        Only sealed publications can appear, so an unsealed publication is never
        research-visible through this read. The HTTP cutoff query surface stays
        closed until the target environment verifies clock and commit visibility.

        Args:
            dataset_id: Dataset whose seals are listed.
            through_visibility_sequence: Optional inclusive upper sequence bound.
            limit: Bounded page size between 1 and 1000.

        Returns:
            Seals ordered by increasing visibility sequence.
        """
        dataset = _dataset_identifier(dataset_id)
        statement = (
            select(VISIBILITY_SEALS)
            .where(VISIBILITY_SEALS.c.dataset_id == dataset)
            .order_by(VISIBILITY_SEALS.c.visibility_sequence.asc())
            .limit(_page_limit(limit))
        )
        if through_visibility_sequence is not None:
            statement = statement.where(
                VISIBILITY_SEALS.c.visibility_sequence
                <= _sequence_value(through_visibility_sequence, "through_visibility_sequence")
            )
        with self._warehouse.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_seal_from_row(row) for row in rows)

    def sequence_state(self) -> VisibilitySequenceState:
        """Return the persisted monotonic ledger and verify it against seal storage.

        Returns:
            The state row, or an uninitialised zero ledger before the first seal.

        Raises:
            VisibilitySealError: The persisted ledger disagrees with persisted seals.
        """
        with self._warehouse.connect() as connection:
            row = (
                connection.execute(
                    select(VISIBILITY_SEQUENCE_STATE).where(
                        VISIBILITY_SEQUENCE_STATE.c.state_id == _STATE_ID
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return VisibilitySequenceState(
                    initialised=False,
                    last_visibility_sequence=0,
                    last_sealed_at=None,
                    seal_count=0,
                    updated_at=None,
                )
            state = _state_from_row(row)
            stored = connection.execute(
                select(
                    VISIBILITY_SEALS.c.visibility_sequence,
                    VISIBILITY_SEALS.c.sealed_at_us,
                )
            ).all()
        if len(stored) != state.seal_count or any(
            int(value[0]) > state.last_visibility_sequence for value in stored
        ):
            raise VisibilitySealError(
                "visibility sequence state does not match persisted seals; refusing to report it"
            )
        return state

    def _replay_if_sealed(self, evidence: _CommittedEvidence) -> VisibilitySealResult | None:
        """Return an exact committed replay, or ``None`` when the publication is unsealed."""
        stored = self.get_seal(evidence.publication_id)
        if stored is None:
            return None
        return VisibilitySealResult(record=_require_exact_replay(stored, evidence), replayed=True)

    def _seal_with_bounded_retries(
        self, evidence: _CommittedEvidence
    ) -> VisibilitySealResult | None:
        """Retry one seal request within the policy bound; ``None`` means still contended."""
        for _attempt in range(self._policy.sequence_attempts):
            result = self._attempt_seal(evidence)
            if result is not None:
                return result
        return None

    def _attempt_seal(self, evidence: _CommittedEvidence) -> VisibilitySealResult | None:
        """Write and read back one seal; ``None`` asks the caller to retry the race."""
        try:
            clock_us, clock_evidence = self._write_seal(evidence)
        except IntegrityError as error:
            if not _is_duplicate_key_error(error):
                raise
            replay = self._replay_if_sealed(evidence)
            if replay is not None:
                return replay
            # The duplicate was the unique visibility sequence, not this publication;
            # another writer holds the number, so re-read the ledger and try again.
            return None
        except _SequenceContentionError:
            # Another writer won the compare-and-set; re-read the ledger and retry.
            return None
        except OperationalError as error:
            if not _is_retryable_lock_error(error):
                raise
            return None
        sealed = self.get_seal(evidence.publication_id)
        if sealed is None:
            raise VisibilitySealError(
                "the committed seal could not be read back; refusing to report a visible "
                "publication"
            )
        if sealed != _expected_record(
            evidence, clock_us, clock_evidence, sealed.visibility_sequence
        ):
            raise VisibilitySealConflictError(
                "read-back seal does not match the values written by its own transaction"
            )
        return VisibilitySealResult(record=sealed, replayed=False)

    def _verify_committed_publication(
        self, connection: Connection, *, publication_id: str, producer_identity: str
    ) -> _CommittedEvidence:
        """Verify the committed publication, pointer, fact partition, and receipt copy."""
        publication = _load_committed_publication(connection, publication_id)
        dataset = str(publication["dataset_id"])
        sequence = _sequence_value(publication["sequence"], "publication sequence")
        record_count = _non_negative_int(publication["record_count"], "record_count")
        payload_sha256 = _digest(publication["payload_hash"], "payload_hash")
        published_at_us = _non_negative_int(publication["published_at_us"], "published_at_us")
        _verify_publication_pointer(connection, publication)
        fact_set_sha256 = _verify_fact_partition(
            connection,
            dataset=dataset,
            sequence=sequence,
            record_count=record_count,
            publication=publication,
        )
        receipt_event_sha256 = self._verify_cross_database_receipt(
            producer_identity,
            dataset=dataset,
            sequence=sequence,
            record_count=record_count,
            payload_sha256=payload_sha256,
            published_at_us=published_at_us,
            publication=publication,
        )
        return _CommittedEvidence(
            publication_id=publication_id,
            dataset_id=dataset,
            publication_sequence=sequence,
            record_count=record_count,
            schema_version=str(publication["schema_version"]),
            producer_identity=producer_identity,
            payload_sha256=payload_sha256,
            fact_set_sha256=fact_set_sha256,
            receipt_event_sha256=receipt_event_sha256,
            published_at_us=published_at_us,
        )

    def _verify_cross_database_receipt(
        self,
        producer_identity: str,
        *,
        dataset: str,
        sequence: int,
        record_count: int,
        payload_sha256: str,
        published_at_us: int,
        publication: RowMapping,
    ) -> str:
        """Require exactly one committed receipt copy whose frozen bytes still verify."""
        with self._metadata.connect() as connection:
            rows = (
                connection.execute(
                    select(METADATA_PUBLICATION_RECEIPTS).where(
                        METADATA_PUBLICATION_RECEIPTS.c.producer_identity == producer_identity,
                        METADATA_PUBLICATION_RECEIPTS.c.publication_id
                        == publication["publication_id"],
                    )
                )
                .mappings()
                .all()
            )
        if len(rows) != 1:
            raise VisibilityCommitVisibilityError(
                "the cross-database publication receipt is not committed for this producer"
            )
        row = rows[0]
        event_sha256 = _digest(row["event_sha256"], "receipt event_sha256")
        event_bytes = _receipt_event_bytes(row["event_json_utf8"])
        if hashlib.sha256(_outbox._EVENT_HASH_DOMAIN + event_bytes).hexdigest() != event_sha256:
            raise VisibilityCommitVisibilityError(
                "receipt event bytes no longer match their frozen domain-separated digest"
            )
        _verify_receipt_event_body(
            event_bytes,
            publication_id=str(publication["publication_id"]),
            dataset=dataset,
            sequence=sequence,
            record_count=record_count,
            payload_sha256=payload_sha256,
            published_at_us=published_at_us,
        )
        if _sequence_value(row["publication_sequence"], "receipt sequence") != sequence:
            raise VisibilityCommitVisibilityError(
                "receipt row sequence does not match the committed publication"
            )
        return event_sha256

    def _read_trusted_clock(self, connection: Connection) -> tuple[int, dict[str, Any]]:
        """Read the authoritative UTC clock and fail closed on an untrusted reading."""
        try:
            database_us = _database_clock_us(connection)
        except (RuntimeError, ValueError, TypeError) as error:
            raise VisibilityClockUntrustedError(
                f"the authoritative UTC clock is unavailable: {error}"
            ) from error
        host_us: int | None = None
        agreement = "not_supplied"
        if self._host_clock is not None:
            host_us = _host_clock_us(self._host_clock)
            if abs(host_us - database_us) > self._policy.max_host_database_skew_us:
                raise VisibilityClockUntrustedError(
                    "the host clock disagrees with the authoritative warehouse clock beyond "
                    f"{self._policy.max_host_database_skew_us} microseconds; refusing to seal"
                )
            agreement = "agreed"
        if not (
            _datetime_to_us(self._policy.min_plausible_utc)
            <= database_us
            <= _datetime_to_us(self._policy.max_plausible_utc)
        ):
            raise VisibilityClockUntrustedError(
                "the authoritative clock is outside the deployment plausible UTC window; "
                "refusing to seal"
            )
        evidence: dict[str, Any] = {
            "authoritative_clock": "warehouse_database_utc_clock",
            "clock_evidence_kind": "database_utc_clock_with_host_agreement_check",
            "database_utc_us": database_us,
            "host_agreement": agreement,
            "host_utc_us": host_us,
            "policy_version": _SEAL_VERSION,
            "skew_tolerance_us": self._policy.max_host_database_skew_us,
        }
        if len(_canonical_json(evidence).encode("utf-8")) > _MAX_CLOCK_EVIDENCE_BYTES:
            raise VisibilityClockUntrustedError("the clock evidence exceeds its bound")
        return database_us, evidence

    def _write_seal(self, evidence: _CommittedEvidence) -> tuple[int, dict[str, Any]]:
        """Insert the seal and advance the sequence ledger in one transaction.

        The authoritative clock is read inside the transaction, after the ledger row
        is locked, so a concurrent writer that committed a later seal instant cannot
        make an honest clock reading look like a rewind. Every refusal in this path
        rolls the transaction back, so a rejected seal never burns a sequence value.

        Args:
            evidence: Read-after-commit values frozen into the seal.

        Returns:
            The accepted seal instant and its bounded clock evidence.

        Raises:
            VisibilityClockUntrustedError: The clock reading cannot be trusted.
            VisibilityClockRewindError: The clock moved backwards against the ledger.
            _SequenceContention: Another writer advanced the ledger first.
        """
        with self._warehouse.begin() as connection:
            if connection.dialect.name == "sqlite":
                # pysqlite defers its physical BEGIN until the first DML, so a SAVEPOINT
                # issued first can survive an outer rollback. This no-op DML opens the
                # real transaction before the ledger row is created.
                connection.exec_driver_sql(
                    "UPDATE od_visibility_sequence_state "
                    "SET last_visibility_sequence = last_visibility_sequence WHERE 1 = 0"
                )
            _ensure_sequence_state(connection)
            last_sequence, last_sealed_at_us, seal_count = _locked_state(connection)
            clock_us, clock_evidence = self._read_trusted_clock(connection)
            _require_forward_clock(clock_us, last_sealed_at_us, evidence.published_at_us)
            canonical = _canonical_json(clock_evidence)
            if last_sequence >= _MAX_SEQUENCE:
                raise VisibilitySealError("the visibility sequence is exhausted")
            next_sequence = last_sequence + 1
            connection.execute(
                insert(VISIBILITY_SEALS).values(
                    publication_id=evidence.publication_id,
                    dataset_id=evidence.dataset_id,
                    publication_sequence=evidence.publication_sequence,
                    record_count=evidence.record_count,
                    schema_version=evidence.schema_version,
                    producer_identity=evidence.producer_identity,
                    payload_sha256=evidence.payload_sha256,
                    fact_set_sha256=evidence.fact_set_sha256,
                    receipt_event_sha256=evidence.receipt_event_sha256,
                    visibility_sequence=next_sequence,
                    sealed_at_us=clock_us,
                    clock_evidence_json=canonical,
                    seal_version=_SEAL_VERSION,
                )
            )
            advanced = connection.execute(
                update(VISIBILITY_SEQUENCE_STATE)
                .where(
                    VISIBILITY_SEQUENCE_STATE.c.state_id == _STATE_ID,
                    VISIBILITY_SEQUENCE_STATE.c.last_visibility_sequence == last_sequence,
                )
                .values(
                    last_visibility_sequence=next_sequence,
                    last_sealed_at_us=clock_us,
                    seal_count=seal_count + 1,
                    updated_at_us=clock_us,
                )
            )
            if advanced.rowcount != 1:
                raise _SequenceContentionError(
                    "another writer advanced the visibility sequence; this seal rolled back"
                )
        return clock_us, clock_evidence


def visibility_seal_store_from_settings(
    settings: object,
    *,
    warehouse_engine: Engine,
    metadata_engine: Engine,
    host_clock: Callable[[], datetime] | None = None,
) -> VisibilitySealStore:
    """Build the seal store from server settings; the flag is default-off.

    Args:
        settings: Server settings object; only a literal ``True`` enables sealing.
        warehouse_engine: Publication and seal storage engine.
        metadata_engine: Control database engine holding receipt copies.
        host_clock: Optional host clock used purely as a trust signal.

    Returns:
        A store that refuses to write seals unless ``VISIBILITY_SEAL_ENABLED`` is ``true``.
    """
    return VisibilitySealStore(
        warehouse_engine,
        metadata_engine,
        enabled=getattr(settings, "visibility_seal_enabled", False) is True,
        host_clock=host_clock,
    )


def _load_committed_publication(connection: Connection, publication_id: str) -> RowMapping:
    """Read the publication row from a fresh connection, proving it is committed."""
    publication = (
        connection.execute(
            select(PUBLICATIONS).where(PUBLICATIONS.c.publication_id == publication_id)
        )
        .mappings()
        .first()
    )
    if publication is None:
        raise VisibilityCommitVisibilityError(
            "the publication is not committed; a visibility seal may only follow a commit"
        )
    schema_version = publication["schema_version"]
    if not isinstance(schema_version, str) or not 1 <= len(schema_version) <= 64:
        raise VisibilityCommitVisibilityError("the committed publication schema version is invalid")
    if _non_negative_int(publication["published_at_us"], "published_at_us") > _MAX_TIMESTAMP_US:
        raise VisibilityCommitVisibilityError(
            "the committed publication timestamp is out of the supported range"
        )
    return publication


def _verify_publication_pointer(connection: Connection, publication: RowMapping) -> None:
    """Require the dataset publication pointer to be committed and at least this far advanced."""
    dataset = str(publication["dataset_id"])
    sequence = _sequence_value(publication["sequence"], "publication sequence")
    head = (
        connection.execute(
            select(PUBLICATION_HEADS).where(PUBLICATION_HEADS.c.dataset_id == dataset)
        )
        .mappings()
        .first()
    )
    if head is None:
        raise VisibilityCommitVisibilityError(
            "the dataset publication pointer is not committed for this publication"
        )
    latest_sequence = _sequence_value(head["latest_sequence"], "head latest_sequence")
    if latest_sequence < sequence:
        raise VisibilityCommitVisibilityError(
            "the publication pointer has not advanced to this publication"
        )
    if head["latest_publication_id"] == publication["publication_id"]:
        return
    _verify_pointer_successor(
        connection,
        dataset=dataset,
        sequence=sequence,
        latest_sequence=latest_sequence,
        latest_publication_id=head["latest_publication_id"],
    )


def _verify_pointer_successor(
    connection: Connection,
    *,
    dataset: str,
    sequence: int,
    latest_sequence: int,
    latest_publication_id: object,
) -> None:
    """Accept a pointer past this publication only with an exactly bound successor row."""
    if not isinstance(latest_publication_id, str) or not _PUBLICATION_ID.fullmatch(
        latest_publication_id
    ):
        raise VisibilityCommitVisibilityError(
            "the publication pointer names no committed successor publication"
        )
    successor = (
        connection.execute(
            select(PUBLICATIONS).where(PUBLICATIONS.c.publication_id == latest_publication_id)
        )
        .mappings()
        .first()
    )
    if (
        successor is None
        or successor["dataset_id"] != dataset
        or latest_sequence <= sequence
        or _sequence_value(successor["sequence"], "successor sequence") != latest_sequence
    ):
        raise VisibilityCommitVisibilityError(
            "the publication pointer successor does not match its dataset and sequence"
        )


def _verify_fact_partition(
    connection: Connection,
    *,
    dataset: str,
    sequence: int,
    record_count: int,
    publication: RowMapping,
) -> str:
    """Verify the committed fact rows and digest the exact appended partition.

    A publication is a delta: a submitted business key whose canonical content is
    unchanged appends no new revision, so a later publication may legitimately bind
    fewer rows than it declares. That is accepted only when the dataset already has
    a committed revision visible at or before this sequence; the dataset's first
    publication must append every record it declares.
    """
    rows = (
        connection.execute(
            select(
                PUBLISHED_FACT_VERSIONS.c.business_key,
                PUBLISHED_FACT_VERSIONS.c.revision,
                PUBLISHED_FACT_VERSIONS.c.record_hash,
                PUBLISHED_FACT_VERSIONS.c.dataset_id,
                PUBLISHED_FACT_VERSIONS.c.publication_sequence,
            )
            .where(PUBLISHED_FACT_VERSIONS.c.publication_id == publication["publication_id"])
            .order_by(
                PUBLISHED_FACT_VERSIONS.c.business_key.asc(),
                PUBLISHED_FACT_VERSIONS.c.revision.asc(),
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > record_count:
        raise VisibilityCommitVisibilityError(
            "the committed fact partition carries more revisions than its publication declared"
        )
    if len(rows) != record_count and not _predecessor_is_visible(
        connection, dataset=dataset, sequence=sequence
    ):
        raise VisibilityCommitVisibilityError(
            "the warehouse fact partition is incomplete for the committed publication"
        )
    seen: set[tuple[str, int]] = set()
    facts: list[list[object]] = []
    for row in rows:
        _require_fact_binding(row, dataset=dataset, sequence=sequence)
        business_key = row["business_key"]
        revision = _sequence_value(row["revision"], "fact revision")
        if not isinstance(business_key, str) or not business_key:
            raise VisibilityCommitVisibilityError("a fact partition row has no business key")
        if (business_key, revision) in seen:
            raise VisibilityCommitVisibilityError(
                "the fact partition repeats the same business key revision"
            )
        seen.add((business_key, revision))
        facts.append([business_key, revision, _digest(row["record_hash"], "record_hash")])
    return hashlib.sha256(
        _FACT_SET_HASH_DOMAIN + _canonical_json(facts).encode("utf-8")
    ).hexdigest()


def _predecessor_is_visible(connection: Connection, *, dataset: str, sequence: int) -> bool:
    """Whether the dataset already holds a committed revision at or before this sequence."""
    if sequence <= 1:
        return False
    found = connection.execute(
        select(PUBLISHED_FACT_VERSIONS.c.business_key)
        .where(
            PUBLISHED_FACT_VERSIONS.c.dataset_id == dataset,
            PUBLISHED_FACT_VERSIONS.c.publication_sequence < sequence,
        )
        .limit(1)
    ).first()
    return found is not None


def _require_fact_binding(row: RowMapping, *, dataset: str, sequence: int) -> None:
    """Require one fact row to be bound to this dataset and publication sequence."""
    if (
        row["dataset_id"] != dataset
        or _sequence_value(row["publication_sequence"], "fact publication sequence") != sequence
    ):
        raise VisibilityCommitVisibilityError(
            "a fact partition row is bound to a different dataset or publication sequence"
        )


def _receipt_event_bytes(value: object) -> bytes:
    """Return receipt event bytes, accepting only a binary or text column."""
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise VisibilityCommitVisibilityError("the receipt event payload is not binary")


def _verify_receipt_event_body(
    event_bytes: bytes,
    *,
    publication_id: str,
    dataset: str,
    sequence: int,
    record_count: int,
    payload_sha256: str,
    published_at_us: int,
) -> None:
    """Require the receipt's frozen event body to still equal the committed publication."""
    try:
        decoded: Any = json.loads(event_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VisibilityCommitVisibilityError(
            "the receipt event payload is not canonical JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise VisibilityCommitVisibilityError("the receipt event payload is not an object")
    if (
        decoded.get("publication_id") != publication_id
        or decoded.get("dataset_id") != dataset
        or decoded.get("publication_sequence") != sequence
        or decoded.get("record_count") != record_count
        or decoded.get("payload_sha256") != payload_sha256
        or decoded.get("published_at_us") != published_at_us
    ):
        raise VisibilityCommitVisibilityError(
            "the receipt event content does not match the committed publication"
        )


def _host_clock_us(host_clock: Callable[[], datetime]) -> int:
    """Read and validate the deployment host clock as UTC microseconds."""
    try:
        value = host_clock()
    except Exception as error:  # an unreadable host clock is untrusted, never assumed
        raise VisibilityClockUntrustedError(
            f"the host clock is unreadable: {type(error).__name__}"
        ) from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise VisibilityClockUntrustedError(
            "the host clock must return a timezone-aware UTC datetime; refusing to seal"
        )
    return _datetime_to_us(value)


def _require_forward_clock(clock_us: int, last_sealed_at_us: int, published_at_us: int) -> None:
    """Require a seal instant that moves the visibility clock forward, never backwards.

    A seal instant equal to the persisted high-water mark is allowed because some
    database clocks have coarser resolution than the two microsecond values being
    compared; any lower value is a rewind and is refused. The publication's own
    in-transaction ``published_at_us`` is non-probative, so only a clear reversal
    beyond the clock resolution tolerance rejects the seal.

    Args:
        clock_us: Authoritative UTC seal instant in epoch microseconds.
        last_sealed_at_us: Persisted visibility clock high-water mark.
        published_at_us: In-transaction publication stamp of the sealing target.

    Raises:
        VisibilityClockRewindError: Either persisted clock relation goes backwards.
    """
    if clock_us < last_sealed_at_us:
        raise VisibilityClockRewindError(
            "the authoritative clock is behind the persisted visibility high-water mark; "
            "refusing to seal"
        )
    if clock_us + _COMMIT_INSTANT_TOLERANCE_US < published_at_us:
        raise VisibilityClockRewindError(
            "the visibility seal instant precedes its own publication commit instant; "
            "refusing to seal"
        )


def _ensure_sequence_state(connection: Connection) -> None:
    """Create the single sequence ledger row idempotently within the seal transaction.

    The existence probe is a locking read, so concurrent first-seal writers serialise on
    the ledger instead of both racing to create it; a loser of the creation race still
    reads the winner's row under the same lock.
    """
    found = connection.execute(
        select(VISIBILITY_SEQUENCE_STATE.c.state_id)
        .where(VISIBILITY_SEQUENCE_STATE.c.state_id == _STATE_ID)
        .with_for_update()
    ).scalar_one_or_none()
    if found is not None:
        return
    try:
        with connection.begin_nested():
            connection.execute(
                insert(VISIBILITY_SEQUENCE_STATE).values(
                    state_id=_STATE_ID,
                    last_visibility_sequence=0,
                    last_sealed_at_us=0,
                    seal_count=0,
                    updated_at_us=0,
                )
            )
    except IntegrityError:
        # A concurrent writer created the ledger first; its row is the authority.
        return


def _locked_state(connection: Connection) -> tuple[int, int, int]:
    """Read the sequence ledger as a locking read where the dialect supports one."""
    row = (
        connection.execute(
            select(VISIBILITY_SEQUENCE_STATE)
            .where(VISIBILITY_SEQUENCE_STATE.c.state_id == _STATE_ID)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    return (
        _non_negative_int(row["last_visibility_sequence"], "last_visibility_sequence"),
        _non_negative_int(row["last_sealed_at_us"], "last_sealed_at_us"),
        _non_negative_int(row["seal_count"], "seal_count"),
    )


def _seal_from_row(row: RowMapping) -> VisibilitySealRecord:
    """Parse one persisted seal row into its immutable model."""
    evidence: Any = json.loads(str(row["clock_evidence_json"]))
    if not isinstance(evidence, dict):
        raise VisibilitySealError("a persisted seal clock evidence value is not a JSON object")
    return VisibilitySealRecord(
        publication_id=str(row["publication_id"]),
        dataset_id=str(row["dataset_id"]),
        publication_sequence=_sequence_value(row["publication_sequence"], "publication_sequence"),
        record_count=_non_negative_int(row["record_count"], "record_count"),
        schema_version=str(row["schema_version"]),
        producer_identity=str(row["producer_identity"]),
        payload_sha256=_digest(row["payload_sha256"], "payload_sha256"),
        fact_set_sha256=_digest(row["fact_set_sha256"], "fact_set_sha256"),
        receipt_event_sha256=_digest(row["receipt_event_sha256"], "receipt_event_sha256"),
        visibility_sequence=_monotonic_sequence(row["visibility_sequence"], "visibility_sequence"),
        sealed_at=_us_to_datetime(_non_negative_int(row["sealed_at_us"], "sealed_at_us")),
        clock_evidence=cast("Mapping[str, Any]", evidence),
        seal_version=_non_negative_int(row["seal_version"], "seal_version"),
    )


def _state_from_row(row: RowMapping) -> VisibilitySequenceState:
    """Parse the persisted sequence ledger row."""
    updated_at_us = _non_negative_int(row["updated_at_us"], "updated_at_us")
    last_sealed_at_us = _non_negative_int(row["last_sealed_at_us"], "last_sealed_at_us")
    return VisibilitySequenceState(
        initialised=True,
        last_visibility_sequence=_non_negative_int(
            row["last_visibility_sequence"], "last_visibility_sequence"
        ),
        last_sealed_at=_us_to_datetime(last_sealed_at_us) if last_sealed_at_us else None,
        seal_count=_non_negative_int(row["seal_count"], "seal_count"),
        updated_at=_us_to_datetime(updated_at_us) if updated_at_us else None,
    )


def _expected_record(
    evidence: _CommittedEvidence,
    clock_us: int,
    clock_evidence: Mapping[str, Any],
    visibility_sequence: int,
) -> VisibilitySealRecord:
    """Build the record the seal transaction must have written."""
    return VisibilitySealRecord(
        publication_id=evidence.publication_id,
        dataset_id=evidence.dataset_id,
        publication_sequence=evidence.publication_sequence,
        record_count=evidence.record_count,
        schema_version=evidence.schema_version,
        producer_identity=evidence.producer_identity,
        payload_sha256=evidence.payload_sha256,
        fact_set_sha256=evidence.fact_set_sha256,
        receipt_event_sha256=evidence.receipt_event_sha256,
        visibility_sequence=visibility_sequence,
        sealed_at=_us_to_datetime(clock_us),
        clock_evidence=clock_evidence,
        seal_version=_SEAL_VERSION,
    )


def _monotonic_sequence(value: object, name: str) -> int:
    """Require one strictly positive persisted visibility sequence value."""
    sequence = _sequence_value(value, name)
    if sequence < 1:
        raise VisibilitySealError(f"persisted seal storage has non-positive {name}")
    return sequence


def _require_exact_replay(
    stored: VisibilitySealRecord, evidence: _CommittedEvidence
) -> VisibilitySealRecord:
    """Accept only a seal whose frozen evidence still matches the committed publication."""
    if (
        stored.publication_id != evidence.publication_id
        or stored.dataset_id != evidence.dataset_id
        or stored.publication_sequence != evidence.publication_sequence
        or stored.record_count != evidence.record_count
        or stored.schema_version != evidence.schema_version
        or stored.producer_identity != evidence.producer_identity
        or stored.payload_sha256 != evidence.payload_sha256
        or stored.fact_set_sha256 != evidence.fact_set_sha256
        or stored.receipt_event_sha256 != evidence.receipt_event_sha256
    ):
        raise VisibilitySealConflictError(
            "a visibility seal already exists for this publication identity and no longer "
            "matches the committed fact partition or receipt"
        )
    return stored


def _publication_identifier(value: str) -> str:
    """Require a lowercase 32-character hex publication identity."""
    if not isinstance(value, str) or not _PUBLICATION_ID.fullmatch(value):
        raise ValueError("publication_id must be a lowercase 32-character hex identifier")
    return value


def _producer_identity(value: str) -> str:
    """Require a bounded single-line producer pin matching the receipt contract."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf-8")) > _MAX_PRODUCER_IDENTITY_BYTES
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("producer_identity must be a bounded, single-line identifier")
    return value


def _dataset_identifier(value: str) -> str:
    """Require a bounded single-line dataset identity."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("dataset_id must be a bounded, single-line identifier")
    return value


def _page_limit(value: int) -> int:
    """Bound one seal listing page."""
    if type(value) is not int or not 1 <= value <= MAX_SEAL_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_SEAL_PAGE_SIZE}")
    return value


def _sequence_value(value: object, name: str) -> int:
    """Require a non-negative signed 64-bit integer read from the database."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisibilityCommitVisibilityError(f"{name} must be an integer")
    if not 0 <= value <= _MAX_SEQUENCE:
        raise VisibilityCommitVisibilityError(f"{name} is outside the supported range")
    return value


def _non_negative_int(value: object, name: str) -> int:
    """Require a non-negative integer database value."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise VisibilitySealError(f"persisted seal storage has invalid {name}")
    if value < 0:
        raise VisibilitySealError(f"persisted seal storage has negative {name}")
    return value


def _digest(value: object, name: str) -> str:
    """Require a lowercase SHA-256 digest read from persisted storage."""
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise VisibilityCommitVisibilityError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _is_duplicate_key_error(error: IntegrityError) -> bool:
    """Classify only unique or primary-key duplicates as seal contention."""
    text = str(error.orig).casefold()
    return "unique" in text or "duplicate" in text or "primary key" in text


def _is_retryable_lock_error(error: OperationalError) -> bool:
    """Classify a SQLite busy or MySQL deadlock response as retryable contention."""
    return _RETRYABLE_LOCK.search(str(error.orig)) is not None


def _canonical_json(value: object) -> str:
    """Encode deterministic compact UTF-8 JSON and reject non-finite numbers."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _datetime_to_us(value: datetime) -> int:
    """Encode an aware UTC datetime as epoch microseconds."""
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _us_to_datetime(value: int) -> datetime:
    """Decode epoch microseconds into an aware UTC datetime."""
    if not 0 <= value <= _MAX_TIMESTAMP_US:
        raise VisibilitySealError("a timestamp is outside the supported UTC range")
    return _EPOCH + timedelta(microseconds=value)


def _rfc3339_utc(value: datetime) -> str:
    """Render an aware UTC datetime as a microsecond-precision RFC 3339 instant."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise VisibilitySealError("seal timestamps must be UTC")
    rendered = f"{value.strftime('%Y-%m-%dT%H:%M:%S')}.{value.microsecond:06d}Z"
    if _RFC3339_UTC.fullmatch(rendered) is None:
        raise VisibilitySealError("a seal timestamp is not valid RFC 3339 UTC")
    return rendered


__all__ = [
    "DEFAULT_VISIBILITY_CLOCK_POLICY",
    "VISIBILITY_SEAL_METADATA",
    "VISIBILITY_SEALS",
    "VISIBILITY_SEQUENCE_STATE",
    "VisibilityClockPolicy",
    "VisibilityClockRewindError",
    "VisibilityClockUntrustedError",
    "VisibilityCommitVisibilityError",
    "VisibilitySealConflictError",
    "VisibilitySealDisabledError",
    "VisibilitySealError",
    "VisibilitySealRecord",
    "VisibilitySealResult",
    "VisibilitySealStore",
    "VisibilitySequenceContentionError",
    "VisibilitySequenceState",
    "visibility_seal_store_from_settings",
]
