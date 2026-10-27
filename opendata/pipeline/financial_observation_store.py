"""Prepare and persist immutable financial observations in two phases.

``prepare_financial_observation_capture`` runs outside a database transaction:
it opens real raw artifacts through ``RawResponseArtifactReader``, validates the
manifest/body and caller-declared scope, decodes and checks every row/pointer,
then returns a deeply frozen ``PreparedCapture`` of SQL scalar values.
``persist_prepared_financial_observation_capture`` runs inside a caller-owned
transaction (for example, a fenced ``RunLedger.commit_result`` callback) and
performs bounded SQL only. It accepts no reader, path, body, decoder object, or
engine and never commits the caller's transaction.

The expected source, source ID, instrument, statement, cadence, and request
digest are trusted scope declarations; they do not prove upstream source
authenticity, actual outbound request parameters, data rights, or unit semantics.
The DTO prevents accidental drift, not deliberate same-process construction or
mutation through Python internals. The caller must preserve the raw files, then
revalidate and hold a valid RunLedger lease/fencing state immediately before and
throughout the SQL commit.

The response observation time is sampled after the HTTP response arrives, and
the raw writer timestamps capture only after it stores the response object.
Accordingly, a persisted capture must never precede its observation time.

Raw bodies remain in the verified raw-evidence object store. SQL rows retain
only content addresses, manifests, source pointers, and exact decimal text.
Filesystem publication and SQL commit are not atomic; if an artifact is removed
after preparation, its database reference may be dangling. Backups, WORM
retention, and cleanup policy are not implemented here. Each capture is bounded
to 64 response rows and at most 16 mapped item codes per row. This is an
input/transaction limit, not evidence of full-history coverage or support for
silently truncated source responses. Larger historical backfills need an
explicit recoverable paging/batching contract and source-capability validation.
The current worst-case SQL command ceiling is 12,680 per capture, including
savepoint and uniqueness-conflict verification statements; this is a finite
upper bound, not a callback latency guarantee.

Operator-declared units and source scope are declarations: this store does not
prove their correctness, data rights, source authenticity, PIT history, or
coverage completeness. A process with the same UID can still alter private raw
files.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal, TypeAlias, TypeGuard, cast
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
)
from sqlalchemy.dialects.mysql import CHAR as MYSQL_CHAR
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import MEDIUMTEXT, VARCHAR
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from opendata.data.financial_observation import FinancialStatementObservation
from opendata.data.providers.ths.endpoints import FINANCIAL_PERIOD_END, financial_statement_items
from opendata.data.providers.ths.models.financial_observation_raw import (
    FinancialObservationDecodeResult,
    FinancialObservationSourceReference,
    QuarantinedFinancialObservationRow,
)
from opendata.pipeline.raw_evidence import RawResponseManifest, RawResponseSource
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceReadError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.sql.type_api import TypeEngine

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_INSTRUMENT_ID_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_POINTER_ESCAPE_PATTERN = re.compile(r"~(?![01])")
_MAX_RAW_BODY_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024
_MAX_FINANCIAL_ROWS_PER_CAPTURE = 64
_MAX_PREPARED_TEXT_CHARS = 65_536
# Current source mappings use at most 13 statement item fields. Leave a small
# reviewed margin; a larger mapping needs an intentional SQL-budget adjustment.
_MAX_ITEM_CODES_PER_ROW = 16
_MAX_OBSERVATION_REFERENCES = _MAX_FINANCIAL_ROWS_PER_CAPTURE * _MAX_ITEM_CODES_PER_ROW
_MAX_QUARANTINED_ROWS = _MAX_FINANCIAL_ROWS_PER_CAPTURE
_MAX_SQL_STATEMENTS_PER_INSERT_OR_VERIFY = 6
_MAX_SQL_STATEMENTS_PER_CAPTURE = 2 + _MAX_SQL_STATEMENTS_PER_INSERT_OR_VERIFY * (
    1 + (2 * _MAX_OBSERVATION_REFERENCES) + _MAX_QUARANTINED_ROWS
)
_EXPECTED_SOURCE_ID = "ths_fuyao"
_OPERATOR_DECLARED = "operator_declared"
_OBSERVATION_POLICY = "current_as_observed_only"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_METADATA = MetaData()

_SQLValue: TypeAlias = str | int | date | datetime | None
_FrozenSqlRow: TypeAlias = tuple[tuple[str, _SQLValue], ...]
_FrozenObservationRows: TypeAlias = tuple[tuple[str, _FrozenSqlRow], ...]
_FrozenLinkGroups: TypeAlias = tuple[tuple[str, tuple[_FrozenSqlRow, ...]], ...]
_FrozenQuarantineRows: TypeAlias = tuple[_FrozenSqlRow, ...]


class FinancialObservationStoreError(ValueError):
    """Raised when a capture or decoded source observation is inconsistent."""


class FinancialObservationStoreConflictError(FinancialObservationStoreError):
    """Raised when an immutable identity is reused for different content."""


class FinancialObservationStoreTransactionError(RuntimeError):
    """Raised when the caller does not supply an active SQLAlchemy transaction."""


@dataclass(frozen=True, slots=True)
class FinancialObservationCaptureContext:
    """Trusted request scope supplied by the application for one response.

    ``raw_source`` and the request-summary digest are caller-supplied expected
    identities. The ``ths_fuyao`` source ID is fixed by the decoder contract.
    These scope declarations do not prove source authenticity, unit semantics,
    data rights, or that a caller-provided digest was computed from the actual
    outbound request parameters.
    """

    source_id: str
    instrument_id: str
    statement_type: Literal["income", "balance", "cashflow"]
    periodicity: Literal["annual", "quarterly"]
    observed_at_utc: datetime
    raw_source: RawResponseSource
    request_summary_sha256: str


@dataclass(frozen=True, slots=True)
class FinancialObservationStoreReceipt:
    """Counts of inserted versus already-identical immutable records."""

    capture_id: str
    inserted_capture: bool
    inserted_observations: int
    inserted_links: int
    inserted_quarantine_rows: int


@dataclass(frozen=True, slots=True)
class PreparedCapture:
    """Deeply frozen SQL values produced outside the caller's DB transaction.

    This DTO prevents accidental mutation between preparation and persistence;
    Python callers in the same process remain trusted code and can deliberately
    construct objects. Persisting it does not re-read or authenticate raw files.
    """

    _capture_id: str = field(repr=False)
    _capture_values: _FrozenSqlRow = field(repr=False)
    _observation_values: _FrozenObservationRows = field(repr=False)
    _links_by_observation: _FrozenLinkGroups = field(repr=False)
    _quarantine_values: _FrozenQuarantineRows = field(repr=False)
    _sql_statement_ceiling: int = field(repr=False)


def _key_type(length: int) -> TypeEngine[str]:
    """Use case-sensitive exact string keys on the supported MySQL dialect."""
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_0900_bin"), "mysql")


def _hash_type() -> TypeEngine[str]:
    """Use fixed-width hash fields with exact MySQL binary comparison."""
    return String(64).with_variant(MYSQL_CHAR(64, collation="utf8mb4_0900_bin"), "mysql")


def _capture_id_type() -> TypeEngine[str]:
    """Use a fixed-width opaque capture id with exact MySQL comparison."""
    return String(32).with_variant(MYSQL_CHAR(32, collation="utf8mb4_0900_bin"), "mysql")


def _utc_datetime_type() -> TypeEngine[datetime]:
    """Keep six microsecond digits in MySQL while SQLite uses DateTime."""
    return DateTime(timezone=False).with_variant(MYSQL_DATETIME(fsp=6), "mysql")


CAPTURES = Table(
    "od_financial_captures",
    _METADATA,
    Column("capture_id", _capture_id_type(), primary_key=True),
    Column("provider_id", _key_type(128), nullable=False),
    Column("dataset_id", _key_type(128), nullable=False),
    Column("operation_id", _key_type(128), nullable=False),
    Column("source_id", _key_type(64), nullable=False),
    Column("instrument_id", _key_type(10), nullable=False),
    Column("statement_type", _key_type(16), nullable=False),
    Column("periodicity", _key_type(16), nullable=False),
    Column("observed_at_utc", _utc_datetime_type(), nullable=False),
    Column("captured_at_utc", _utc_datetime_type(), nullable=False),
    Column("source_request_id", _key_type(256), nullable=False),
    Column("http_status", SmallInteger, nullable=False),
    Column("request_summary_sha256", _hash_type(), nullable=False),
    Column("raw_object_sha256", _hash_type(), nullable=False),
    Column("raw_object_size_bytes", BigInteger, nullable=False),
    Column("raw_object_key", _key_type(512), nullable=False),
    Column("raw_manifest_sha256", _hash_type(), nullable=False),
    Column("raw_manifest_size_bytes", BigInteger, nullable=False),
    Column("raw_manifest_key", _key_type(512), nullable=False),
    Column("retention_class", _key_type(24), nullable=False),
    Column("source_history_limit", _key_type(32), nullable=False),
    Column("native_unit_mapping_version_declared", _key_type(128), nullable=False),
    Column("native_unit_mapping_sha256_declared", _hash_type(), nullable=False),
    Column("unit_mapping_declaration_status", _key_type(24), nullable=False),
    Column("decode_status", _key_type(24), nullable=False),
    Index(
        "ix_od_fin_capture_scope_observed",
        "source_id",
        "instrument_id",
        "statement_type",
        "periodicity",
        "observed_at_utc",
    ),
)

OBSERVATIONS = Table(
    "od_financial_observations",
    _METADATA,
    Column("observation_id", _hash_type(), primary_key=True),
    Column("natural_key_sha256", _hash_type(), nullable=False),
    Column("content_sha256", _hash_type(), nullable=False),
    Column("source_id", _key_type(64), nullable=False),
    Column("instrument_id", _key_type(10), nullable=False),
    Column("statement_type", _key_type(16), nullable=False),
    Column("periodicity", _key_type(16), nullable=False),
    Column("report_period", Date, nullable=False),
    Column("source_disclosure_date", Date, nullable=False),
    Column("observed_at_utc", _utc_datetime_type(), nullable=False),
    Column("item_code", _key_type(256), nullable=False),
    Column("value_decimal_text", _key_type(128), nullable=True),
    Column("native_unit", _key_type(128), nullable=False),
    Column(
        "source_metadata_json",
        Text().with_variant(MEDIUMTEXT(), "mysql"),
        nullable=False,
    ),
    Column("observation_policy", _key_type(40), nullable=False),
    UniqueConstraint(
        "natural_key_sha256",
        "content_sha256",
        name="uq_od_fin_observation_natural_content",
    ),
    Index(
        "ix_od_financial_observations_scope",
        "source_id",
        "instrument_id",
        "statement_type",
        "periodicity",
        "report_period",
    ),
)

CAPTURE_OBSERVATION_LINKS = Table(
    "od_financial_capture_observation_links",
    _METADATA,
    Column("link_id", _hash_type(), primary_key=True),
    Column(
        "capture_id",
        _capture_id_type(),
        ForeignKey("od_financial_captures.capture_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("value_pointer", _key_type(512), nullable=False),
    Column(
        "observation_id",
        _hash_type(),
        ForeignKey("od_financial_observations.observation_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("row_pointer", _key_type(512), nullable=False),
    UniqueConstraint(
        "capture_id",
        "value_pointer",
        name="uq_od_fin_link_capture_value_pointer",
    ),
    Index("ix_od_fin_link_observation", "observation_id"),
)

QUARANTINE = Table(
    "od_financial_observation_quarantine",
    _METADATA,
    Column("quarantine_id", _hash_type(), primary_key=True),
    Column(
        "capture_id",
        _capture_id_type(),
        ForeignKey("od_financial_captures.capture_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("row_pointer", _key_type(512), nullable=False),
    Column("disclosure_pointer", _key_type(512), nullable=True),
    Column("reason", _key_type(48), nullable=False),
    UniqueConstraint(
        "capture_id",
        "row_pointer",
        name="uq_od_fin_quarantine_capture_row",
    ),
    Index("ix_od_fin_quarantine_capture", "capture_id"),
)


def prepare_financial_observation_capture(
    *,
    context: FinancialObservationCaptureContext,
    reader: RawResponseArtifactReader,
    manifest_sha256: str,
    expected_http_status: int,
    result: FinancialObservationDecodeResult,
) -> PreparedCapture:
    """Read and validate raw evidence outside the warehouse transaction.

    This performs no database access. It reopens actual evidence through the
    exact reader, parses and validates the body, checks complete row and pointer
    coverage, and freezes only bounded SQL scalar values into a DTO.

    Args:
        context: Caller-declared expected source and request scope.
        reader: Exact reader rooted at the private raw evidence store.
        manifest_sha256: Lowercase SHA-256 address of the canonical manifest.
        expected_http_status: HTTP status independently observed by the caller.
            It is passed to the raw reader and compared with the verified manifest.
        result: Decoder output tied to the archived response digest and request ID.

    Returns:
        A deeply frozen, bounded DTO suitable for a later SQL-only callback.

    Raises:
        FinancialObservationStoreError: Any scope, digest, row, pointer, or
            raw-evidence mismatch is rejected before a transaction is opened.
    """
    checked_context = _validate_context(context)
    _validate_reader_arguments(reader, manifest_sha256, expected_http_status, result)
    try:
        evidence = reader.read(
            manifest_sha256,
            expected_source=checked_context.raw_source,
            expected_request_summary_sha256=checked_context.request_summary_sha256,
            captured_at_not_after=datetime.now(timezone.utc),
            expected_http_status=expected_http_status,
        )
    except RawEvidenceReadError:
        raise FinancialObservationStoreError(
            "raw evidence reader rejected the manifest or object"
        ) from None
    rows = _prepare_rows(
        context=checked_context,
        evidence=evidence,
        expected_http_status=expected_http_status,
        result=result,
    )
    return _freeze_prepared_rows(rows)


def persist_prepared_financial_observation_capture(
    connection: Connection,
    *,
    prepared: PreparedCapture,
) -> FinancialObservationStoreReceipt:
    """Persist a prepared DTO using only caller-owned SQL transaction resources.

    This callback performs no filesystem access, clock reads, JSON decoding,
    provider lookups, network access, engine creation, or commit. It verifies the
    DTO's immutable scalar shape and statement-count ceiling, then uses bounded
    SQL and nested savepoints on the supplied connection. The caller owns the
    RunLedger lease/fencing state and must preserve referenced raw files until
    the SQL transaction commits.

    Args:
        connection: Active synchronous SQLAlchemy transaction. On SQLite the
            caller must already have opened the physical DBAPI transaction,
            normally using ``BEGIN IMMEDIATE``.
        prepared: DTO returned by :func:`prepare_financial_observation_capture`.

    Returns:
        Counts describing newly inserted capture, observation, link and
        quarantine rows. Replayed identical rows count as zero insertions.

    Raises:
        FinancialObservationStoreError: An invalid DTO or immutable-key
            conflict is rejected without upsert or overwrite.
        FinancialObservationStoreTransactionError: No caller-owned physical
            transaction is active.
    """
    _require_connection(connection)
    rows = _materialize_prepared_capture(prepared)

    inserted_observations = 0
    inserted_links = 0
    inserted_quarantine_rows = 0
    try:
        with connection.begin_nested():
            inserted_capture = _insert_or_verify(
                connection,
                CAPTURES,
                rows.capture,
                lookup_keys=(("capture_id",),),
            )
            for observation_id, values in rows.observations:
                if _insert_or_verify(
                    connection,
                    OBSERVATIONS,
                    values,
                    lookup_keys=(("observation_id",), ("natural_key_sha256", "content_sha256")),
                    ignored_existing_fields=frozenset({"observed_at_utc"}),
                ):
                    inserted_observations += 1
                for link_values in rows.links_by_observation[observation_id]:
                    if _insert_or_verify(
                        connection,
                        CAPTURE_OBSERVATION_LINKS,
                        link_values,
                        lookup_keys=(("link_id",), ("capture_id", "value_pointer")),
                    ):
                        inserted_links += 1
            for values in rows.quarantine:
                if _insert_or_verify(
                    connection,
                    QUARANTINE,
                    values,
                    lookup_keys=(("quarantine_id",), ("capture_id", "row_pointer")),
                ):
                    inserted_quarantine_rows += 1
    except FinancialObservationStoreError:
        raise
    except SQLAlchemyError:
        raise FinancialObservationStoreError("financial archive database write failed") from None

    return FinancialObservationStoreReceipt(
        capture_id=rows.capture_id,
        inserted_capture=inserted_capture,
        inserted_observations=inserted_observations,
        inserted_links=inserted_links,
        inserted_quarantine_rows=inserted_quarantine_rows,
    )


@dataclass(frozen=True, slots=True)
class _PreparedRows:
    """Fully validated SQL values prepared before the first write."""

    capture: dict[str, object]
    capture_id: str
    observations: tuple[tuple[str, dict[str, object]], ...]
    links_by_observation: dict[str, tuple[dict[str, object], ...]]
    quarantine: tuple[dict[str, object], ...]


def _is_sql_value(value: object) -> TypeGuard[_SQLValue]:
    """Accept only immutable scalar values supported by the prepared archive rows."""
    return value is None or type(value) in (str, int, date, datetime)


def _freeze_sql_row(values: dict[str, object], table: Table) -> _FrozenSqlRow:
    """Convert one complete validated row to a sorted immutable scalar tuple."""
    if set(values) != set(table.columns.keys()):
        raise FinancialObservationStoreError("prepared SQL row has an unexpected column set")
    frozen: list[tuple[str, _SQLValue]] = []
    for name in sorted(values):
        value = values[name]
        if type(name) is not str or not _is_sql_value(value):
            raise FinancialObservationStoreError("prepared SQL row contains a mutable value")
        frozen.append((name, value))
    return tuple(frozen)


def _thaw_sql_row(values: object, table: Table) -> dict[str, object]:
    """Revalidate an immutable DTO row and materialize it for SQLAlchemy."""
    if type(values) is not tuple:
        raise FinancialObservationStoreError("prepared SQL row is not an immutable tuple")
    row: dict[str, object] = {}
    for entry in values:
        if type(entry) is not tuple or len(entry) != 2:
            raise FinancialObservationStoreError("prepared SQL row entry is invalid")
        name, value = entry
        if type(name) is not str or not _is_sql_value(value) or name in row:
            raise FinancialObservationStoreError("prepared SQL row entry is invalid")
        column = table.c.get(name)
        if column is None:
            raise FinancialObservationStoreError("prepared SQL row has an unexpected column")
        _validate_prepared_column_value(table, column, value)
        row[name] = value
    if set(row) != set(table.columns.keys()):
        raise FinancialObservationStoreError("prepared SQL row has an unexpected column set")
    return row


def _validate_prepared_column_value(table: Table, column: Column[object], value: _SQLValue) -> None:
    """Bound scalar size and enforce the SQL column's expected Python type."""
    if value is None:
        if not column.nullable:
            raise FinancialObservationStoreError("prepared SQL row contains a null required value")
        return
    if isinstance(column.type, Text):
        if type(value) is not str:
            raise FinancialObservationStoreError("prepared SQL text value has an invalid type")
        if len(value) > _MAX_PREPARED_TEXT_CHARS:
            raise FinancialObservationStoreError("prepared SQL text exceeds its character limit")
        try:
            encoded_size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError:
            raise FinancialObservationStoreError("prepared SQL text is not valid UTF-8") from None
        if (
            table.name == OBSERVATIONS.name
            and column.name == "source_metadata_json"
            and not 2 <= encoded_size <= 65_536
        ):
            raise FinancialObservationStoreError(
                "prepared source metadata exceeds its finite byte limit"
            )
        return
    if isinstance(column.type, String):
        if type(value) is not str:
            raise FinancialObservationStoreError("prepared SQL text value has an invalid type")
        string_type = cast("String", column.type)
        if string_type.length is not None and len(value) > string_type.length:
            raise FinancialObservationStoreError("prepared SQL text exceeds its column limit")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise FinancialObservationStoreError("prepared SQL text is not valid UTF-8") from None
        return
    if isinstance(column.type, DateTime):
        if type(value) is not datetime or value.tzinfo is not None:
            raise FinancialObservationStoreError("prepared SQL datetime must be a naive UTC value")
        return
    if isinstance(column.type, Date):
        if type(value) is not date:
            raise FinancialObservationStoreError("prepared SQL date value has an invalid type")
        return
    if isinstance(column.type, (BigInteger, SmallInteger)):
        if type(value) is not int:
            raise FinancialObservationStoreError("prepared SQL integer value has an invalid type")
        if isinstance(column.type, BigInteger):
            lower_bound, upper_bound = -(2**63), (2**63) - 1
        else:
            lower_bound, upper_bound = -(2**15), (2**15) - 1
        if not lower_bound <= value <= upper_bound:
            raise FinancialObservationStoreError("prepared SQL integer exceeds its column range")
        return
    raise FinancialObservationStoreError("prepared SQL column uses an unsupported scalar type")


def _sql_statement_ceiling(
    *, observation_count: int, link_count: int, quarantine_count: int
) -> int:
    """Bound SQL commands including savepoints and unique-conflict readbacks."""
    insert_or_verify_calls = 1 + observation_count + link_count + quarantine_count
    return 2 + _MAX_SQL_STATEMENTS_PER_INSERT_OR_VERIFY * insert_or_verify_calls


def _freeze_prepared_rows(rows: _PreparedRows) -> PreparedCapture:
    """Deep-freeze prepared values and enforce per-capture statement limits."""
    if len(rows.observations) > _MAX_OBSERVATION_REFERENCES:
        raise FinancialObservationStoreError("capture exceeds the observation SQL budget")
    frozen_capture = _freeze_sql_row(rows.capture, CAPTURES)
    frozen_observations = tuple(
        (observation_id, _freeze_sql_row(values, OBSERVATIONS))
        for observation_id, values in rows.observations
    )
    frozen_links = tuple(
        (
            observation_id,
            tuple(_freeze_sql_row(values, CAPTURE_OBSERVATION_LINKS) for values in link_values),
        )
        for observation_id, link_values in sorted(rows.links_by_observation.items())
    )
    frozen_quarantine = tuple(
        _freeze_sql_row(values, QUARANTINE)
        for values in sorted(rows.quarantine, key=lambda row: str(row["row_pointer"]))
    )
    link_count = sum(len(group) for _, group in frozen_links)
    if link_count > _MAX_OBSERVATION_REFERENCES or len(frozen_quarantine) > _MAX_QUARANTINED_ROWS:
        raise FinancialObservationStoreError("capture exceeds its SQL row budget")
    statement_ceiling = _sql_statement_ceiling(
        observation_count=len(frozen_observations),
        link_count=link_count,
        quarantine_count=len(frozen_quarantine),
    )
    if statement_ceiling > _MAX_SQL_STATEMENTS_PER_CAPTURE:
        raise FinancialObservationStoreError("capture exceeds its SQL statement budget")
    return PreparedCapture(
        _capture_id=rows.capture_id,
        _capture_values=frozen_capture,
        _observation_values=frozen_observations,
        _links_by_observation=frozen_links,
        _quarantine_values=frozen_quarantine,
        _sql_statement_ceiling=statement_ceiling,
    )


def _materialize_prepared_capture(prepared: PreparedCapture) -> _PreparedRows:
    """Check and materialize the DTO without filesystem, clock, or provider work."""
    if type(prepared) is not PreparedCapture:
        raise FinancialObservationStoreError("prepared capture has an invalid type")
    if (
        type(prepared._capture_id) is not str
        or _CAPTURE_ID_PATTERN.fullmatch(prepared._capture_id) is None
    ):
        raise FinancialObservationStoreError("prepared capture identity is invalid")
    capture = _thaw_sql_row(prepared._capture_values, CAPTURES)
    if capture["capture_id"] != prepared._capture_id:
        raise FinancialObservationStoreError("prepared capture identity does not match its row")
    if (
        type(prepared._observation_values) is not tuple
        or len(prepared._observation_values) > _MAX_OBSERVATION_REFERENCES
    ):
        raise FinancialObservationStoreError("prepared observations exceed their SQL budget")
    observations: list[tuple[str, dict[str, object]]] = []
    observation_ids: set[str] = set()
    for observation_entry in prepared._observation_values:
        if type(observation_entry) is not tuple or len(observation_entry) != 2:
            raise FinancialObservationStoreError("prepared observation entry is invalid")
        observation_id, immutable_values = observation_entry
        if (
            type(observation_id) is not str
            or _SHA256_PATTERN.fullmatch(observation_id) is None
            or observation_id in observation_ids
        ):
            raise FinancialObservationStoreError("prepared observation identity is invalid")
        values = _thaw_sql_row(immutable_values, OBSERVATIONS)
        if values["observation_id"] != observation_id:
            raise FinancialObservationStoreError("prepared observation row identity is invalid")
        observation_ids.add(observation_id)
        observations.append((observation_id, values))

    if type(prepared._links_by_observation) is not tuple:
        raise FinancialObservationStoreError("prepared link groups are invalid")
    links: dict[str, tuple[dict[str, object], ...]] = {}
    seen_pointers: set[str] = set()
    link_count = 0
    for link_group_entry in prepared._links_by_observation:
        if type(link_group_entry) is not tuple or len(link_group_entry) != 2:
            raise FinancialObservationStoreError("prepared link group is invalid")
        observation_id, immutable_group = link_group_entry
        if (
            type(observation_id) is not str
            or observation_id not in observation_ids
            or observation_id in links
            or type(immutable_group) is not tuple
            or not immutable_group
        ):
            raise FinancialObservationStoreError("prepared link group is invalid")
        group: list[dict[str, object]] = []
        for immutable_link_values in immutable_group:
            values = _thaw_sql_row(immutable_link_values, CAPTURE_OBSERVATION_LINKS)
            pointer = values["value_pointer"]
            if (
                values["capture_id"] != prepared._capture_id
                or values["observation_id"] != observation_id
                or type(pointer) is not str
                or pointer in seen_pointers
            ):
                raise FinancialObservationStoreError("prepared link reference is inconsistent")
            seen_pointers.add(pointer)
            group.append(values)
            link_count += 1
        links[observation_id] = tuple(group)
    if set(links) != observation_ids or link_count > _MAX_OBSERVATION_REFERENCES:
        raise FinancialObservationStoreError("prepared links do not cover observations")

    if (
        type(prepared._quarantine_values) is not tuple
        or len(prepared._quarantine_values) > _MAX_QUARANTINED_ROWS
    ):
        raise FinancialObservationStoreError("prepared quarantine rows exceed their SQL budget")
    quarantine: list[dict[str, object]] = []
    quarantine_pointers: set[str] = set()
    for immutable_values in prepared._quarantine_values:
        values = _thaw_sql_row(immutable_values, QUARANTINE)
        pointer = values["row_pointer"]
        if (
            values["capture_id"] != prepared._capture_id
            or type(pointer) is not str
            or pointer in quarantine_pointers
        ):
            raise FinancialObservationStoreError("prepared quarantine reference is inconsistent")
        quarantine_pointers.add(pointer)
        quarantine.append(values)

    statement_ceiling = _sql_statement_ceiling(
        observation_count=len(observations),
        link_count=link_count,
        quarantine_count=len(quarantine),
    )
    if (
        type(prepared._sql_statement_ceiling) is not int
        or prepared._sql_statement_ceiling != statement_ceiling
        or statement_ceiling > _MAX_SQL_STATEMENTS_PER_CAPTURE
    ):
        raise FinancialObservationStoreError("prepared capture SQL statement ceiling is invalid")
    return _PreparedRows(
        capture=capture,
        capture_id=prepared._capture_id,
        observations=tuple(observations),
        links_by_observation=links,
        quarantine=tuple(quarantine),
    )


def _validate_reader_arguments(
    reader: RawResponseArtifactReader,
    manifest_sha256: str,
    expected_http_status: int,
    result: FinancialObservationDecodeResult,
) -> None:
    """Require exact reader, content address, explicit HTTP status, and decoder types."""
    if type(reader) is not RawResponseArtifactReader:
        raise FinancialObservationStoreError("raw evidence reader is not the reviewed exact type")
    if type(manifest_sha256) is not str or _SHA256_PATTERN.fullmatch(manifest_sha256) is None:
        raise FinancialObservationStoreError("raw manifest address is invalid")
    if type(expected_http_status) is not int or expected_http_status not in {200, 201}:
        raise FinancialObservationStoreError("expected HTTP status is not a successful response")
    if type(result) is not FinancialObservationDecodeResult:
        raise FinancialObservationStoreError("decoder result is not the reviewed exact type")


def _prepare_rows(
    *,
    context: FinancialObservationCaptureContext,
    evidence: RawEvidenceReadResult,
    expected_http_status: int,
    result: FinancialObservationDecodeResult,
) -> _PreparedRows:
    """Validate complete scope, hashes, and pointers without touching SQL."""
    checked_context = _validate_context(context)
    checked_evidence = _validate_evidence(evidence, checked_context, expected_http_status)
    checked_result = _validate_decode_result(result, checked_evidence)
    body_payload = _parse_json_body(checked_evidence.body)
    _validate_request_id(body_payload, checked_result.source_request_id)
    raw_rows = _source_rows(body_payload)
    try:
        expected_item_codes = frozenset(financial_statement_items(checked_context.statement_type))
    except (KeyError, TypeError, ValueError):
        raise FinancialObservationStoreError(
            "financial statement item mapping is unavailable"
        ) from None
    if not expected_item_codes:
        raise FinancialObservationStoreError("financial statement item mapping is empty")
    if len(expected_item_codes) > _MAX_ITEM_CODES_PER_ROW:
        raise FinancialObservationStoreError(
            "statement mapping exceeds the bounded SQL item budget"
        )
    if len(raw_rows) > _MAX_FINANCIAL_ROWS_PER_CAPTURE:
        raise FinancialObservationStoreError(
            "response exceeds the single-capture row limit; no rows were truncated"
        )
    if len(raw_rows) * len(expected_item_codes) > _MAX_OBSERVATION_REFERENCES:
        raise FinancialObservationStoreError("response exceeds the bounded observation SQL budget")
    expected_quarantine: dict[str, str | None] = {}
    observed_date = checked_context.observed_at_utc.astimezone(_SHANGHAI).date()
    for index, row in enumerate(raw_rows):
        row_pointer = f"/data/item/{index}"
        report_period = _validate_raw_row_scope(row, checked_context)
        expected_quarantine[row_pointer] = _expected_quarantine_reason(
            row,
            report_period=report_period,
            observed_date=observed_date,
        )

    capture_id = checked_evidence.manifest.capture_id
    capture_values = _capture_values(
        checked_context,
        checked_evidence,
        checked_result,
    )

    prepared_observations: dict[str, dict[str, object]] = {}
    links: dict[str, list[dict[str, object]]] = {}
    seen_value_pointers: set[str] = set()
    item_codes_by_row: dict[str, set[str]] = {}
    for reference in checked_result.observations:
        observation = _validate_observation_reference(
            reference,
            context=checked_context,
            evidence=checked_evidence,
            body_payload=body_payload,
        )
        observation_id = observation.observation_id
        natural_key_sha256 = _natural_key_sha256(observation.natural_key)
        values = _observation_values(observation, natural_key_sha256)
        existing = prepared_observations.get(observation_id)
        if existing is not None and not _row_values_equal(
            existing,
            values,
            ignored_fields=frozenset({"observed_at_utc"}),
        ):
            raise FinancialObservationStoreConflictError(
                "one observation identity has inconsistent decoded fields"
            )
        prepared_observations[observation_id] = values
        if reference.value_pointer in seen_value_pointers:
            raise FinancialObservationStoreConflictError(
                "a capture contains duplicate source value pointers"
            )
        seen_value_pointers.add(reference.value_pointer)
        row_pointer = reference.row_pointer
        item_codes_by_row.setdefault(row_pointer, set()).add(observation.item_code)
        link: dict[str, object] = {
            "link_id": _link_id(capture_id, reference.value_pointer),
            "capture_id": capture_id,
            "value_pointer": reference.value_pointer,
            "observation_id": observation_id,
            "row_pointer": row_pointer,
        }
        links.setdefault(observation_id, []).append(link)

    quarantined_values: list[dict[str, object]] = []
    seen_quarantine_pointers: set[str] = set()
    quarantine_reason_by_row: dict[str, str] = {}
    for quarantined in checked_result.quarantined_rows:
        _validate_quarantine_reference(
            quarantined,
            context=checked_context,
            body_sha256=checked_evidence.manifest.response_sha256,
            body_payload=body_payload,
        )
        if quarantined.row_pointer in seen_quarantine_pointers:
            raise FinancialObservationStoreConflictError(
                "a capture contains duplicate quarantined row pointers"
            )
        if quarantined.row_pointer in {
            link["row_pointer"] for rows in links.values() for link in rows
        }:
            raise FinancialObservationStoreConflictError(
                "a capture cannot both publish and quarantine the same source row"
            )
        seen_quarantine_pointers.add(quarantined.row_pointer)
        quarantine_reason_by_row[quarantined.row_pointer] = quarantined.reason
        quarantine_id = hashlib.sha256(
            json.dumps(
                [capture_id, quarantined.row_pointer, quarantined.reason],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        quarantined_values.append(
            {
                "quarantine_id": quarantine_id,
                "capture_id": capture_id,
                "row_pointer": quarantined.row_pointer,
                "disclosure_pointer": quarantined.disclosure_pointer,
                "reason": quarantined.reason,
            }
        )

    for row_pointer, reason in expected_quarantine.items():
        actual_reason = quarantine_reason_by_row.get(row_pointer)
        if reason is None:
            if actual_reason is not None or item_codes_by_row.get(row_pointer) != set(
                expected_item_codes
            ):
                raise FinancialObservationStoreError(
                    "decoded item references do not cover a valid source row"
                )
        elif actual_reason != reason or item_codes_by_row.get(row_pointer):
            raise FinancialObservationStoreError(
                "quarantine references do not cover an unusable source row"
            )

    if not prepared_observations and not quarantined_values:
        raise FinancialObservationStoreError(
            "decoded financial response contains neither observations nor quarantined rows"
        )
    return _PreparedRows(
        capture=capture_values,
        capture_id=capture_id,
        observations=tuple(sorted(prepared_observations.items())),
        links_by_observation={
            observation_id: tuple(sorted(rows, key=lambda row: str(row["value_pointer"])))
            for observation_id, rows in links.items()
        },
        quarantine=tuple(quarantined_values),
    )


def _validate_context(
    context: FinancialObservationCaptureContext,
) -> FinancialObservationCaptureContext:
    """Revalidate caller-owned scope at the persistence boundary."""
    if type(context) is not FinancialObservationCaptureContext:
        raise FinancialObservationStoreError("capture context is not the reviewed exact type")
    if (
        type(context.source_id) is not str
        or context.source_id != _EXPECTED_SOURCE_ID
        or _SOURCE_ID_PATTERN.fullmatch(context.source_id) is None
    ):
        raise FinancialObservationStoreError("capture context source id is invalid")
    if (
        type(context.instrument_id) is not str
        or _INSTRUMENT_ID_PATTERN.fullmatch(context.instrument_id) is None
    ):
        raise FinancialObservationStoreError("capture context instrument id is invalid")
    if type(context.statement_type) is not str or context.statement_type not in {
        "income",
        "balance",
        "cashflow",
    }:
        raise FinancialObservationStoreError("capture context statement type is invalid")
    if type(context.periodicity) is not str or context.periodicity not in {
        "annual",
        "quarterly",
    }:
        raise FinancialObservationStoreError("capture context periodicity is invalid")
    observed_at = _utc_aware_to_naive(context.observed_at_utc, "capture observation time")
    if type(context.raw_source) is not RawResponseSource:
        raise FinancialObservationStoreError("expected raw source identity is invalid")
    try:
        checked_source = RawResponseSource.model_validate(
            context.raw_source.model_dump(mode="python"), strict=True
        )
    except (AttributeError, TypeError, ValueError):
        raise FinancialObservationStoreError("expected raw source identity is invalid") from None
    _require_sha256(context.request_summary_sha256, "request summary digest")
    return FinancialObservationCaptureContext(
        source_id=context.source_id,
        instrument_id=context.instrument_id,
        statement_type=context.statement_type,
        periodicity=context.periodicity,
        observed_at_utc=observed_at.replace(tzinfo=timezone.utc),
        raw_source=checked_source,
        request_summary_sha256=context.request_summary_sha256,
    )


def _validate_evidence(
    evidence: RawEvidenceReadResult,
    context: FinancialObservationCaptureContext,
    expected_http_status: int,
) -> RawEvidenceReadResult:
    """Recompute manifest and body fingerprints and bind the caller's status/scope."""
    if type(expected_http_status) is not int or expected_http_status not in {200, 201}:
        raise FinancialObservationStoreError("expected HTTP status is not a successful response")
    if type(evidence) is not RawEvidenceReadResult:
        raise FinancialObservationStoreError("raw evidence must be a verified reader result")
    if type(evidence.manifest) is not RawResponseManifest:
        raise FinancialObservationStoreError("raw evidence manifest is invalid")
    if type(evidence.body) is not bytes or not 1 <= len(evidence.body) <= _MAX_RAW_BODY_BYTES:
        raise FinancialObservationStoreError("raw evidence body exceeds its finite input contract")
    try:
        manifest = RawResponseManifest.model_validate(
            evidence.manifest.model_dump(mode="python"), strict=True
        )
    except (AttributeError, TypeError, ValueError):
        raise FinancialObservationStoreError("raw evidence manifest failed validation") from None
    manifest_bytes = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
        raise FinancialObservationStoreError("raw evidence manifest exceeds its byte limit")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    response_sha256 = hashlib.sha256(evidence.body).hexdigest()
    if (
        type(evidence.manifest_sha256) is not str
        or _SHA256_PATTERN.fullmatch(evidence.manifest_sha256) is None
        or evidence.manifest_sha256 != manifest_sha256
    ):
        raise FinancialObservationStoreError("raw manifest digest does not match its fields")
    if (
        manifest.response_sha256 != response_sha256
        or manifest.response_size_bytes != len(evidence.body)
        or manifest.source != context.raw_source
        or manifest.request_summary_sha256 != context.request_summary_sha256
        or manifest.http_status != expected_http_status
    ):
        raise FinancialObservationStoreError("raw evidence is not bound to the expected request")
    if _CAPTURE_ID_PATTERN.fullmatch(manifest.capture_id) is None:
        raise FinancialObservationStoreError("raw evidence capture id is invalid")
    captured_at = _utc_aware_to_naive(manifest.captured_at_utc, "raw capture time")
    observed_at = _utc_aware_to_naive(context.observed_at_utc, "capture observation time")
    if captured_at < observed_at:
        raise FinancialObservationStoreError(
            "raw capture time precedes the response observation time"
        )
    return RawEvidenceReadResult(
        body=evidence.body,
        manifest=manifest,
        manifest_sha256=manifest_sha256,
    )


def _validate_decode_result(
    result: FinancialObservationDecodeResult,
    evidence: RawEvidenceReadResult,
) -> FinancialObservationDecodeResult:
    """Bind decoder output digest, request id, and declarations to reader evidence."""
    if type(result) is not FinancialObservationDecodeResult:
        raise FinancialObservationStoreError("decoder result is not the reviewed exact type")
    if (
        type(result.body_sha256) is not str
        or _SHA256_PATTERN.fullmatch(result.body_sha256) is None
        or result.body_sha256 != evidence.manifest.response_sha256
    ):
        raise FinancialObservationStoreError("decoder digest does not match archived response")
    if (
        type(result.source_request_id) is not str
        or not result.source_request_id
        or result.source_request_id != result.source_request_id.strip()
        or len(result.source_request_id) > 256
        or any(
            ord(character) < 32 or ord(character) == 127 for character in result.source_request_id
        )
    ):
        raise FinancialObservationStoreError("decoded request id is invalid")
    try:
        result.source_request_id.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise FinancialObservationStoreError("decoded request id is invalid") from None
    if (
        type(result.native_unit_mapping_version_declared) is not str
        or not result.native_unit_mapping_version_declared
        or result.native_unit_mapping_version_declared
        != result.native_unit_mapping_version_declared.strip()
        or len(result.native_unit_mapping_version_declared) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in result.native_unit_mapping_version_declared
        )
    ):
        raise FinancialObservationStoreError("unit mapping version declaration is invalid")
    try:
        result.native_unit_mapping_version_declared.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise FinancialObservationStoreError(
            "unit mapping version declaration is invalid"
        ) from None
    _require_sha256(
        result.native_unit_mapping_sha256_declared,
        "unit mapping declaration digest",
    )
    if (
        type(result.observations) is not tuple
        or type(result.quarantined_rows) is not tuple
        or len(result.observations) > _MAX_OBSERVATION_REFERENCES
        or len(result.quarantined_rows) > _MAX_QUARANTINED_ROWS
    ):
        raise FinancialObservationStoreError("decoder result collections are invalid")
    if not result.observations and not result.quarantined_rows:
        raise FinancialObservationStoreError("decoder result contains no decoded references")
    return result


def _capture_values(
    context: FinancialObservationCaptureContext,
    evidence: RawEvidenceReadResult,
    result: FinancialObservationDecodeResult,
) -> dict[str, object]:
    """Build one database capture row using relative content-addressed keys."""
    manifest = evidence.manifest
    manifest_hash = evidence.manifest_sha256
    body_hash = manifest.response_sha256
    capture_time = _utc_aware_to_naive(manifest.captured_at_utc, "raw capture time")
    observed_at = _utc_aware_to_naive(context.observed_at_utc, "capture observation time")
    decoded_with_quarantine = bool(result.quarantined_rows)
    return {
        "capture_id": manifest.capture_id,
        "provider_id": manifest.source.provider_id,
        "dataset_id": manifest.source.dataset_id,
        "operation_id": manifest.source.operation_id,
        "source_id": context.source_id,
        "instrument_id": context.instrument_id,
        "statement_type": context.statement_type,
        "periodicity": context.periodicity,
        "observed_at_utc": observed_at,
        "captured_at_utc": capture_time,
        "source_request_id": result.source_request_id,
        "http_status": manifest.http_status,
        "request_summary_sha256": manifest.request_summary_sha256,
        "raw_object_sha256": body_hash,
        "raw_object_size_bytes": manifest.response_size_bytes,
        "raw_object_key": _content_addressed_key(body_hash, suffix=".blob"),
        "raw_manifest_sha256": manifest_hash,
        "raw_manifest_size_bytes": len(_canonical_manifest_bytes(manifest)),
        "raw_manifest_key": _content_addressed_key(manifest_hash, suffix=".json"),
        "retention_class": manifest.retention_class,
        "source_history_limit": manifest.source_history_limit,
        "native_unit_mapping_version_declared": result.native_unit_mapping_version_declared,
        "native_unit_mapping_sha256_declared": result.native_unit_mapping_sha256_declared,
        "unit_mapping_declaration_status": _OPERATOR_DECLARED,
        "decode_status": "decoded_with_quarantine" if decoded_with_quarantine else "decoded",
    }


def _validate_observation_reference(
    reference: FinancialObservationSourceReference,
    *,
    context: FinancialObservationCaptureContext,
    evidence: RawEvidenceReadResult,
    body_payload: object,
) -> FinancialStatementObservation:
    """Validate observation scope and prove its JSON pointer reaches its value."""
    if type(reference) is not FinancialObservationSourceReference:
        raise FinancialObservationStoreError("financial source reference has an invalid type")
    observation = reference.observation
    if type(observation) is not FinancialStatementObservation:
        raise FinancialObservationStoreError("financial observation has an invalid type")
    observation = _revalidate_observation(observation)
    if (
        reference.body_sha256 != evidence.manifest.response_sha256
        or observation.source_id != context.source_id
        or observation.instrument_id != context.instrument_id
        or observation.statement_type != context.statement_type
        or observation.periodicity != context.periodicity
        or observation.observed_at != context.observed_at_utc
    ):
        raise FinancialObservationStoreError("financial observation is outside capture scope")
    _require_sha256(observation.observation_id, "observation id")
    _require_sha256(observation.content_sha256, "observation content digest")
    segments = _validate_pointer(reference.row_pointer, "financial row pointer")
    value_segments = _validate_pointer(reference.value_pointer, "financial value pointer")
    if (
        len(segments) != 3
        or segments[:2] != ["data", "item"]
        or value_segments[:3] != segments
        or len(value_segments) != 4
        or value_segments[3] != observation.item_code
    ):
        raise FinancialObservationStoreError("financial JSON pointer does not match its item")
    row = _resolve_body_row(body_payload, segments)
    report_period = _validate_raw_row_scope(row, context)
    if report_period != observation.report_period:
        raise FinancialObservationStoreError("financial report period does not match its row")
    disclosure_date = _source_millis_to_date(row.get("report_date_ms", _MISSING))
    if disclosure_date is None or disclosure_date != observation.source_disclosure_date:
        raise FinancialObservationStoreError("financial disclosure date does not match its row")
    if disclosure_date > context.observed_at_utc.astimezone(_SHANGHAI).date():
        raise FinancialObservationStoreError("financial disclosure date is after observation time")
    try:
        item_codes = financial_statement_items(context.statement_type)
    except (KeyError, TypeError, ValueError):
        raise FinancialObservationStoreError(
            "financial statement item mapping is unavailable"
        ) from None
    if observation.item_code not in item_codes:
        raise FinancialObservationStoreError("financial item code is outside its statement mapping")
    expected_metadata = {key: value for key, value in row.items() if key not in item_codes}
    if dict(observation.source_metadata) != expected_metadata:
        raise FinancialObservationStoreError("financial metadata does not match its source row")
    if row.get(observation.item_code, _MISSING) is _MISSING:
        raise FinancialObservationStoreError("financial source value is missing at its pointer")
    if not _raw_value_matches(row[observation.item_code], observation.value):
        raise FinancialObservationStoreError("financial source value does not match its pointer")
    return observation


def _revalidate_observation(
    observation: FinancialStatementObservation,
) -> FinancialStatementObservation:
    """Re-run the immutable domain contract and recompute its content identities."""
    try:
        checked = FinancialStatementObservation(
            instrument_id=observation.instrument_id,
            statement_type=observation.statement_type,
            periodicity=observation.periodicity,
            report_period=observation.report_period,
            source_disclosure_date=observation.source_disclosure_date,
            observed_at=observation.observed_at,
            source_id=observation.source_id,
            item_code=observation.item_code,
            value=observation.value,
            native_unit=observation.native_unit,
            source_metadata=observation.source_metadata,
            observation_policy=observation.observation_policy,
        )
    except (ArithmeticError, OverflowError, TypeError, ValueError):
        raise FinancialObservationStoreError(
            "financial observation failed domain validation"
        ) from None
    if (
        checked.observation_id != observation.observation_id
        or checked.content_sha256 != observation.content_sha256
        or checked.natural_key != observation.natural_key
    ):
        raise FinancialObservationStoreConflictError(
            "financial observation identity does not match its immutable fields"
        )
    return checked


def _validate_raw_row_scope(
    row: dict[str, object],
    context: FinancialObservationCaptureContext,
) -> date:
    """Bind the source row's identity and fiscal period to the explicit request."""
    if (
        row.get("thscode") != context.instrument_id
        or row.get("ticker") != context.instrument_id[:6]
        or row.get("period") != context.periodicity
        or row.get("currency") != "CNY"
    ):
        raise FinancialObservationStoreError("financial source row does not match capture scope")
    year = row.get("fiscal_year")
    fiscal_period = row.get("fiscal_period")
    if type(year) is not int or not 1 <= year <= 9999 or type(fiscal_period) is not str:
        raise FinancialObservationStoreError("financial source row period is invalid")
    if fiscal_period not in FINANCIAL_PERIOD_END:
        raise FinancialObservationStoreError("financial source row period is invalid")
    if (context.periodicity == "annual" and fiscal_period != "FY") or (
        context.periodicity == "quarterly" and fiscal_period == "FY"
    ):
        raise FinancialObservationStoreError("financial source row cadence is inconsistent")
    try:
        month, day = FINANCIAL_PERIOD_END[fiscal_period]
        report_period = date(year, month, day)
    except (OverflowError, TypeError, ValueError):
        raise FinancialObservationStoreError("financial source row period is invalid") from None
    if _source_millis_to_date(row.get("period_end_ms", _MISSING)) != report_period:
        raise FinancialObservationStoreError("financial source period end is inconsistent")
    return report_period


def _source_rows(payload: object) -> list[dict[str, object]]:
    """Return every bounded source row or reject an unrecognized response shape."""
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        raise FinancialObservationStoreError("archived financial response has no data object")
    raw_rows = payload["data"].get("item")
    if type(raw_rows) is not list or not 1 <= len(raw_rows) <= 64:
        raise FinancialObservationStoreError(
            "archived response exceeds the single-capture row limit; no rows were truncated"
        )
    if any(not _is_json_object(row) for row in raw_rows):
        raise FinancialObservationStoreError("archived financial response contains an invalid row")
    return raw_rows


def _expected_quarantine_reason(
    row: dict[str, object],
    *,
    report_period: date,
    observed_date: date,
) -> str | None:
    """Classify only the decoder's three explicit disclosure-date quarantine cases."""
    if "report_date_ms" not in row:
        return "disclosure_date_missing"
    disclosure_date = _source_millis_to_date(row["report_date_ms"])
    if disclosure_date is None or disclosure_date < report_period:
        return "disclosure_date_invalid"
    if disclosure_date > observed_date:
        return "disclosure_date_after_observed_at"
    return None


def _source_millis_to_date(value: object) -> date | None:
    """Interpret an exact source millisecond timestamp at Shanghai midnight."""
    if type(value) is not int:
        return None
    try:
        local = (_UTC_EPOCH + timedelta(milliseconds=value)).astimezone(_SHANGHAI)
    except (OverflowError, OSError, TypeError, ValueError):
        return None
    if local.timetz().replace(tzinfo=None) != time.min:
        return None
    return local.date()


def _validate_quarantine_reference(
    reference: QuarantinedFinancialObservationRow,
    *,
    context: FinancialObservationCaptureContext,
    body_sha256: str,
    body_payload: object,
) -> None:
    """Bind one safe quarantine reason and its pointers to the archived row."""
    if type(reference) is not QuarantinedFinancialObservationRow:
        raise FinancialObservationStoreError("quarantined financial row has an invalid type")
    if type(reference.reason) is not str or reference.reason not in {
        "disclosure_date_missing",
        "disclosure_date_invalid",
        "disclosure_date_after_observed_at",
    }:
        raise FinancialObservationStoreError("quarantine reason is not declared")
    if reference.body_sha256 != body_sha256:
        raise FinancialObservationStoreError("quarantined row is not bound to raw response")
    row_segments = _validate_pointer(reference.row_pointer, "quarantine row pointer")
    if len(row_segments) != 3 or row_segments[:2] != ["data", "item"]:
        raise FinancialObservationStoreError("quarantine pointer is outside the source rows")
    row = _resolve_body_row(body_payload, row_segments)
    report_period = _validate_raw_row_scope(row, context)
    if reference.reason == "disclosure_date_missing":
        if reference.disclosure_pointer is not None or "report_date_ms" in row:
            raise FinancialObservationStoreError(
                "missing disclosure date does not match its source row"
            )
    elif reference.reason in {"disclosure_date_invalid", "disclosure_date_after_observed_at"}:
        expected_pointer = f"{reference.row_pointer}/report_date_ms"
        if reference.disclosure_pointer != expected_pointer or "report_date_ms" not in row:
            raise FinancialObservationStoreError("disclosure date pointer does not match its row")
        disclosure_segments = _validate_pointer(
            reference.disclosure_pointer,
            "quarantine disclosure pointer",
        )
        if (
            disclosure_segments[:3] != row_segments
            or len(disclosure_segments) != 4
            or disclosure_segments[3] != "report_date_ms"
        ):
            raise FinancialObservationStoreError("quarantine disclosure pointer is inconsistent")
        disclosure_date = _source_millis_to_date(row["report_date_ms"])
        observed_date = context.observed_at_utc.astimezone(_SHANGHAI).date()
        if reference.reason == "disclosure_date_invalid":
            if disclosure_date is not None and disclosure_date >= report_period:
                raise FinancialObservationStoreError(
                    "invalid disclosure date reason does not match its source row"
                )
        elif (
            disclosure_date is None
            or disclosure_date < report_period
            or disclosure_date <= observed_date
        ):
            raise FinancialObservationStoreError(
                "future disclosure date reason does not match its source row"
            )
    else:
        raise FinancialObservationStoreError("quarantine reason is not declared")


def _parse_json_body(body: bytes) -> object:
    """Parse exact archived JSON bytes while rejecting duplicate or nonfinite tokens."""

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def reject_nonfinite(_token: str) -> object:
        raise ValueError("nonfinite JSON number")

    try:
        return json.loads(
            body.decode("utf-8", errors="strict"),
            parse_float=Decimal,
            parse_constant=reject_nonfinite,
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise FinancialObservationStoreError(
            "archived financial response JSON is invalid"
        ) from None


def _validate_request_id(payload: object, expected: str) -> None:
    """Verify the decoded request id against the exact captured envelope."""
    if (
        type(expected) is not str
        or not expected
        or expected != expected.strip()
        or len(expected) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in expected)
        or type(payload) is not dict
        or payload.get("request_id") != expected
    ):
        raise FinancialObservationStoreError("decoded request id does not match raw response")


_MISSING = object()


def _is_json_object(value: object) -> TypeGuard[dict[str, object]]:
    """Return whether a decoded value is an exact string-keyed JSON object."""
    return type(value) is dict and all(type(key) is str for key in value)


def _resolve_body_row(payload: object, segments: list[str]) -> dict[str, object]:
    """Resolve a validated `/data/item/{index}` pointer to one JSON object."""
    if type(payload) is not dict:
        raise FinancialObservationStoreError("archived financial response is not an object")
    data = payload.get("data")
    if type(data) is not dict:
        raise FinancialObservationStoreError("archived financial response has no data object")
    items = data.get("item")
    if type(items) is not list or len(items) > 64:
        raise FinancialObservationStoreError("archived financial response has no item rows")
    index_text = segments[2]
    if re.fullmatch(r"0|[1-9][0-9]{0,5}", index_text) is None:
        raise FinancialObservationStoreError("financial row pointer index is invalid")
    index = int(index_text)
    if index >= len(items):
        raise FinancialObservationStoreError("financial row pointer does not resolve")
    row = items[index]
    if not _is_json_object(row):
        raise FinancialObservationStoreError("financial row pointer does not resolve")
    return row


def _raw_value_matches(raw: object, expected: Decimal | int | str | None) -> bool:
    """Compare source JSON null or exact decimal magnitude to the decoded value."""
    if raw is None:
        return expected is None
    if expected is None or type(raw) is bool or type(raw) is float:
        return False
    if isinstance(expected, Decimal):
        expected_value = expected
    elif type(expected) is int:
        expected_value = Decimal(expected)
    elif type(expected) is str and len(expected) <= 128:
        try:
            expected_value = Decimal(expected)
        except InvalidOperation:
            return False
    else:
        return False
    if isinstance(raw, Decimal):
        value = raw
    elif type(raw) is int:
        value = Decimal(raw)
    elif type(raw) is str:
        if len(raw) > 128:
            return False
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return False
    else:
        return False
    return value.is_finite() and expected_value.is_finite() and value == expected_value


def _observation_values(
    observation: FinancialStatementObservation,
    natural_key_sha256: str,
) -> dict[str, object]:
    """Map one source-native observation to immutable SQL fields."""
    value = observation.value
    if value is not None and not isinstance(value, Decimal):
        raise FinancialObservationStoreError("financial observation value is not exact Decimal")
    decimal_text = _decimal_storage_text(value) if value is not None else None
    if decimal_text is not None and len(decimal_text) > 128:
        raise FinancialObservationStoreError("financial decimal text exceeds the schema limit")
    metadata_json = observation.source_metadata_json
    if not 2 <= len(metadata_json.encode("utf-8", errors="strict")) <= 65_536:
        raise FinancialObservationStoreError("financial source metadata exceeds the schema limit")
    return {
        "observation_id": observation.observation_id,
        "natural_key_sha256": natural_key_sha256,
        "content_sha256": observation.content_sha256,
        "source_id": observation.source_id,
        "instrument_id": observation.instrument_id,
        "statement_type": observation.statement_type,
        "periodicity": observation.periodicity,
        "report_period": observation.report_period,
        "source_disclosure_date": observation.source_disclosure_date,
        "observed_at_utc": _utc_aware_to_naive(observation.observed_at, "observation time"),
        "item_code": observation.item_code,
        "value_decimal_text": decimal_text,
        "native_unit": observation.native_unit,
        "source_metadata_json": metadata_json,
        "observation_policy": observation.observation_policy,
    }


def _natural_key_sha256(natural_key: tuple[str, ...]) -> str:
    """Hash the canonical JSON array representation of the immutable natural key."""
    canonical = json.dumps(
        natural_key,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()


def _link_id(capture_id: str, value_pointer: str) -> str:
    """Derive a stable identifier from the per-capture source-value location."""
    canonical = json.dumps(
        [capture_id, value_pointer],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()


def _content_addressed_key(digest: str, *, suffix: Literal[".blob", ".json"]) -> str:
    """Derive a relative key only from validated lowercase content digests."""
    _require_sha256(digest, "content address")
    if type(suffix) is not str or suffix not in {".blob", ".json"}:
        raise FinancialObservationStoreError("content address suffix is not declared")
    return f"{digest[:2]}/{digest}{suffix}"


def _decimal_storage_text(value: Decimal) -> str:
    """Render a validated Decimal as exact non-exponent database text."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0", "+0"}:
        return "0"
    return rendered[1:] if rendered.startswith("+") else rendered


def _canonical_manifest_bytes(manifest: RawResponseManifest) -> bytes:
    """Encode validated manifest fields exactly as the raw-evidence writer does."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")


def _utc_aware_to_naive(value: datetime, name: str) -> datetime:
    """Validate zero-offset aware UTC and return SQL's UTC-naive representation."""
    if type(value) is not datetime:
        raise FinancialObservationStoreError(f"{name} must be an aware UTC datetime")
    try:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise FinancialObservationStoreError(f"{name} must be an aware UTC datetime")
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    except (NotImplementedError, OverflowError, TypeError, ValueError):
        raise FinancialObservationStoreError(f"{name} must be an aware UTC datetime") from None


def _require_sha256(value: object, name: str) -> None:
    """Require a lowercase SHA-256 token without echoing the supplied value."""
    if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
        raise FinancialObservationStoreError(f"{name} must be lowercase SHA-256")


def _validate_pointer(value: object, name: str) -> list[str]:
    """Validate a bounded RFC 6901 pointer and decode its path segments."""
    if type(value) is not str or not value.startswith("/") or len(value) > 512:
        raise FinancialObservationStoreError(f"{name} is invalid")
    raw_segments = value[1:].split("/")
    segments: list[str] = []
    for raw_segment in raw_segments:
        if _POINTER_ESCAPE_PATTERN.search(raw_segment) is not None:
            raise FinancialObservationStoreError(f"{name} contains an invalid escape")
        segments.append(raw_segment.replace("~1", "/").replace("~0", "~"))
    return segments


def _row_values_equal(
    left: Mapping[str, object],
    right: Mapping[str, object],
    *,
    ignored_fields: frozenset[str] = frozenset(),
) -> bool:
    """Compare every immutable persisted field, with explicit first-seen skips."""
    if set(left) != set(right):
        return False
    return all(left[field] == right[field] for field in left if field not in ignored_fields)


def _insert_or_verify(
    connection: Connection,
    table: Table,
    values: dict[str, object],
    *,
    lookup_keys: Sequence[tuple[str, ...]],
    ignored_existing_fields: frozenset[str] = frozenset(),
) -> bool:
    """Insert inside a savepoint or verify an identical row after key collision."""
    try:
        with connection.begin_nested():
            connection.execute(insert(table).values(**values))
        return True
    except IntegrityError:
        matches: dict[tuple[object, ...], RowMapping] = {}
        for lookup_key in lookup_keys:
            predicate = and_(*(table.c[name] == values[name] for name in lookup_key))
            row = (
                connection.execute(select(table).where(predicate).with_for_update())
                .mappings()
                .one_or_none()
            )
            if row is not None:
                identity_fields = tuple(
                    column.name for column in table.primary_key.columns if column.name is not None
                )
                identity = tuple(row[name] for name in identity_fields)
                matches[identity] = row
        if len(matches) != 1:
            raise FinancialObservationStoreError(
                "financial archive uniqueness conflict has no single matching row"
            ) from None
        existing = next(iter(matches.values()))
        comparable = {name: existing[name] for name in values}
        if not _row_values_equal(
            comparable,
            values,
            ignored_fields=ignored_existing_fields,
        ):
            raise FinancialObservationStoreConflictError(
                "an immutable financial archive key was reused for different content"
            ) from None
        return False


def _require_connection(connection: Connection) -> None:
    """Require an active caller-owned physical transaction on SQLite as well."""
    if not isinstance(connection, Connection):
        raise TypeError("connection must be a synchronous SQLAlchemy Connection")
    if connection.closed or not connection.in_transaction():
        raise FinancialObservationStoreTransactionError(
            "financial observation storage requires an active caller-owned transaction"
        )
    if connection.dialect.name == "sqlite":
        try:
            driver_connection = connection.connection.driver_connection
            physical_transaction = getattr(driver_connection, "in_transaction", None)
        except SQLAlchemyError:
            physical_transaction = None
        if type(physical_transaction) is not bool or not physical_transaction:
            raise FinancialObservationStoreTransactionError(
                "SQLite storage requires an already-open physical transaction; "
                "the caller must issue BEGIN IMMEDIATE"
            )


__all__ = [
    "FinancialObservationCaptureContext",
    "PreparedCapture",
    "FinancialObservationStoreConflictError",
    "FinancialObservationStoreError",
    "FinancialObservationStoreReceipt",
    "FinancialObservationStoreTransactionError",
    "persist_prepared_financial_observation_capture",
    "prepare_financial_observation_capture",
]
