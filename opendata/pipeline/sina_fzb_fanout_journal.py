"""Durable journal for bounded, per-symbol Sina FZB scheduled fanout.

The application contract freezes each universe manifest and records a
lease-fenced enqueue cursor. The database does not prevent a principal with
direct UPDATE access from changing a manifest and its digest; deployments must
restrict table writes to the service account. This module does not dispatch
child runs, create tables, connect to application settings, or make the journal
and child ledger one transaction. Callers must use deterministic child
identities so a partially dispatched chunk can be replayed safely.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    and_,
    case,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

FanoutGroupStatus = Literal["PENDING", "ENQUEUED", "BLOCKED"]

PENDING: FanoutGroupStatus = "PENDING"
ENQUEUED: FanoutGroupStatus = "ENQUEUED"
BLOCKED: FanoutGroupStatus = "BLOCKED"

MAX_FANOUT_SYMBOLS = 20_000
MAX_FANOUT_CHUNK_SIZE = 100
SINA_FZB_JOB_ID = "sina-fzb-capture"
MAX_MANIFEST_BYTES = 2_097_152
MAX_JOB_IDENTIFIER_LENGTH = 128
MAX_WORKER_LENGTH = 255
MAX_ERROR_CODE_LENGTH = 64
MIN_TTL = timedelta(seconds=1)
MAX_TTL = timedelta(days=1)
MAX_RETRY_DELAY = timedelta(days=30)

SINA_FZB_FANOUT_METADATA = MetaData()
SINA_FZB_FANOUT_GROUP_TABLE = Table(
    "od_sina_fzb_fanout_groups",
    SINA_FZB_FANOUT_METADATA,
    Column(
        "fanout_key",
        String(64).with_variant(String(64, collation="utf8mb4_0900_bin"), "mysql"),
        primary_key=True,
    ),
    Column(
        "job_id",
        String(MAX_JOB_IDENTIFIER_LENGTH).with_variant(
            String(MAX_JOB_IDENTIFIER_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        nullable=False,
    ),
    Column(
        "job_version",
        String(MAX_JOB_IDENTIFIER_LENGTH).with_variant(
            String(MAX_JOB_IDENTIFIER_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        nullable=False,
    ),
    Column("occurrence_at_us", BigInteger, nullable=False),
    Column("manifest_sha256", String(64), nullable=False),
    Column("manifest_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
    Column("member_count", BigInteger, nullable=False),
    Column("next_index", BigInteger, nullable=False),
    Column(
        "status",
        String(16).with_variant(String(16, collation="utf8mb4_0900_bin"), "mysql"),
        nullable=False,
    ),
    Column(
        "lease_owner",
        String(MAX_WORKER_LENGTH).with_variant(
            String(MAX_WORKER_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        nullable=True,
    ),
    Column("fencing_token", BigInteger, nullable=False),
    Column("lease_expires_at_us", BigInteger, nullable=True),
    Column("attempt_count", BigInteger, nullable=False),
    Column("next_attempt_at_us", BigInteger, nullable=True),
    Column(
        "last_error_code",
        String(MAX_ERROR_CODE_LENGTH).with_variant(
            String(MAX_ERROR_CODE_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        nullable=True,
    ),
    Column("created_at_us", BigInteger, nullable=False),
    Column("updated_at_us", BigInteger, nullable=False),
    Column("enqueued_at_us", BigInteger, nullable=True),
    CheckConstraint(
        "status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
        name="ck_od_sina_fzb_fanout_status",
    ),
    CheckConstraint(
        "job_id = 'sina-fzb-capture'",
        name="ck_od_sina_fzb_fanout_job_id",
    ),
    CheckConstraint("member_count > 0", name="ck_od_sina_fzb_fanout_member_count"),
    CheckConstraint(
        "next_index >= 0 AND next_index <= member_count",
        name="ck_od_sina_fzb_fanout_cursor",
    ),
    CheckConstraint("fencing_token >= 0", name="ck_od_sina_fzb_fanout_fence"),
    CheckConstraint("attempt_count >= 0", name="ck_od_sina_fzb_fanout_attempts"),
    CheckConstraint(
        "(lease_owner IS NULL AND lease_expires_at_us IS NULL) OR "
        "(lease_owner IS NOT NULL AND lease_expires_at_us IS NOT NULL)",
        name="ck_od_sina_fzb_fanout_lease_pair",
    ),
    CheckConstraint(
        "status != 'ENQUEUED' OR "
        "(next_index = member_count AND lease_owner IS NULL AND lease_expires_at_us IS NULL)",
        name="ck_od_sina_fzb_fanout_enqueued_complete",
    ),
    CheckConstraint(
        "status != 'PENDING' OR next_index < member_count",
        name="ck_od_sina_fzb_fanout_pending_incomplete",
    ),
    CheckConstraint(
        "status != 'BLOCKED' OR lease_owner IS NULL",
        name="ck_od_sina_fzb_fanout_blocked_unleased",
    ),
    CheckConstraint(
        "status != 'BLOCKED' OR last_error_code IS NOT NULL",
        name="ck_od_sina_fzb_fanout_blocked_error",
    ),
    CheckConstraint(
        "status != 'ENQUEUED' OR last_error_code IS NULL",
        name="ck_od_sina_fzb_fanout_enqueued_no_error",
    ),
    CheckConstraint("length(fanout_key) = 64", name="ck_od_sina_fzb_fanout_key_sha_len"),
    CheckConstraint("length(manifest_sha256) = 64", name="ck_od_sina_fzb_manifest_sha_len"),
)
Index(
    "uq_od_sina_fzb_fanout_slot",
    SINA_FZB_FANOUT_GROUP_TABLE.c.job_id,
    SINA_FZB_FANOUT_GROUP_TABLE.c.occurrence_at_us,
    unique=True,
)
Index(
    "ix_od_sina_fzb_fanout_claim",
    SINA_FZB_FANOUT_GROUP_TABLE.c.status,
    SINA_FZB_FANOUT_GROUP_TABLE.c.next_attempt_at_us,
    SINA_FZB_FANOUT_GROUP_TABLE.c.lease_expires_at_us,
    SINA_FZB_FANOUT_GROUP_TABLE.c.updated_at_us,
    SINA_FZB_FANOUT_GROUP_TABLE.c.created_at_us,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECONDS_PER_SECOND = 1_000_000
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$")
_EXPECTED_MANIFEST_KEYS = frozenset(
    {
        "contract_version",
        "job_id",
        "job_version",
        "occurrence",
        "manifest_id",
        "selection_snapshot_ref",
        "symbols",
    }
)


@dataclass(frozen=True, slots=True)
class FanoutGroupRecord:
    """Immutable view of one scheduled slot's frozen fanout manifest and cursor."""

    fanout_key: str
    job_id: str
    job_version: str
    occurrence: datetime
    manifest_sha256: str
    manifest_json: str
    symbols: tuple[str, ...]
    next_index: int
    status: FanoutGroupStatus
    lease_owner: str | None
    fencing_token: int
    lease_expires_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    enqueued_at: datetime | None


class FanoutManifestConflictError(ValueError):
    """Raised when a scheduled slot is retried with a different manifest."""


class FanoutJournalCorruptionError(RuntimeError):
    """Raised when a persisted fanout row violates its immutable contract."""


class SinaFzbFanoutJournal:
    """Persist immutable fanout groups and lease-fenced enqueue progress.

    Args:
        engine: Caller-owned synchronous SQLAlchemy engine. The table must
            already exist; this class never creates or migrates it.
        clock_policy: ``database`` is required for workers on multiple hosts.
            ``caller`` is useful for one-host deployments and deterministic tests.

    The journal does not enqueue child runs itself. Its fencing token protects
    group-state transitions only; deterministic idempotency in the child run
    ledger makes replay safe across the two separate transactions. Manifest
    immutability is an application-level contract, not a database permission
    boundary: deployments must prevent untrusted principals from updating this
    table directly. The current behavior suite exercises SQLite; MySQL and
    PostgreSQL support has not yet been verified against live servers.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock_policy: Literal["caller", "database"] = "database",
    ) -> None:
        """Bind an explicit engine without opening a connection."""
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if clock_policy not in {"caller", "database"}:
            raise ValueError("clock_policy must be 'caller' or 'database'")
        if clock_policy == "database" and engine.dialect.name not in {
            "mysql",
            "postgresql",
            "sqlite",
        }:
            raise ValueError(f"database clock policy is unsupported for {engine.dialect.name!r}")
        self._engine = engine
        self._clock_policy = clock_policy

    def create_or_get_group(
        self,
        *,
        job_id: str,
        job_version: str,
        occurrence: datetime,
        manifest_json: str,
    ) -> FanoutGroupRecord:
        """Create one immutable job-slot manifest or return its exact replay.

        A changed job version or manifest in an existing ``job_id`` and UTC
        occurrence slot raises :class:`FanoutManifestConflictError`. The
        manifest must be canonical UTF-8 JSON with the contract-v1 fields.
        """
        _validate_identifier(job_id, "job_id")
        if job_id != SINA_FZB_JOB_ID:
            raise ValueError(f"job_id must be {SINA_FZB_JOB_ID!r}")
        _validate_identifier(job_version, "job_version")
        occurrence_us = _datetime_to_epoch_us(occurrence)
        symbols = _validate_manifest(manifest_json, job_id, job_version, occurrence_us)
        occurrence_text = (
            _epoch_us_to_datetime(occurrence_us)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        fanout_key = _fanout_key(job_id, occurrence_text)
        manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

        try:
            with self._engine.begin() as connection:
                _begin_sqlite_write(connection)
                now_us = self._timestamp_us(connection)
                connection.execute(
                    insert(SINA_FZB_FANOUT_GROUP_TABLE).values(
                        fanout_key=fanout_key,
                        job_id=job_id,
                        job_version=job_version,
                        occurrence_at_us=occurrence_us,
                        manifest_sha256=manifest_hash,
                        manifest_json=manifest_json,
                        member_count=len(symbols),
                        next_index=0,
                        status=PENDING,
                        lease_owner=None,
                        fencing_token=0,
                        lease_expires_at_us=None,
                        attempt_count=0,
                        next_attempt_at_us=None,
                        last_error_code=None,
                        created_at_us=now_us,
                        updated_at_us=now_us,
                        enqueued_at_us=None,
                    )
                )
                row = _select_group(connection, fanout_key)
        except IntegrityError:
            existing = self._get_by_slot(job_id, occurrence_us)
            if existing is None:
                raise FanoutJournalCorruptionError(
                    "fanout identity collided with a row outside its natural slot"
                ) from None
            if (
                existing.fanout_key != fanout_key
                or existing.job_version != job_version
                or existing.manifest_sha256 != manifest_hash
                or existing.manifest_json != manifest_json
            ):
                raise FanoutManifestConflictError(
                    "scheduled fanout slot already exists with a different immutable manifest"
                ) from None
            return existing
        if row is None:
            raise FanoutJournalCorruptionError("inserted fanout group could not be read")
        return row

    def claim_next_group(
        self,
        *,
        worker: str,
        now: datetime,
        ttl: timedelta,
    ) -> FanoutGroupRecord | None:
        """Lease the oldest due pending group and increment only its fencing token.

        Normal chunk claims do not count as failures. ``attempt_count`` advances
        only when a live lease is explicitly released for a retry.
        """
        _validate_worker(worker)
        _datetime_to_epoch_us(now)
        ttl_us = _timedelta_to_ttl_us(ttl)
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            now_us = self._timestamp_us(connection, now)
            claimable = _claimable_predicate(now_us)
            candidate_query = (
                select(table.c.fanout_key)
                .where(claimable)
                .order_by(table.c.updated_at_us, table.c.created_at_us, table.c.fanout_key)
                .limit(1)
            )
            if connection.dialect.name in {"mysql", "postgresql"}:
                candidate = connection.execute(
                    candidate_query.with_for_update(skip_locked=True)
                ).scalar_one_or_none()
            else:
                candidate = connection.execute(candidate_query).scalar_one_or_none()
            if candidate is None:
                return None
            updated = connection.execute(
                update(table)
                .where(table.c.fanout_key == candidate, claimable)
                .values(
                    lease_owner=worker,
                    fencing_token=table.c.fencing_token + 1,
                    lease_expires_at_us=now_us + ttl_us,
                    updated_at_us=case(
                        (table.c.updated_at_us < now_us, now_us),
                        else_=table.c.updated_at_us,
                    ),
                )
            )
            if updated.rowcount != 1:
                return None
            return _select_group(connection, candidate)

    def heartbeat(
        self,
        *,
        fanout_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> FanoutGroupRecord | None:
        """Extend a live group's lease only for its exact worker and fence."""
        _validate_fanout_key(fanout_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        _datetime_to_epoch_us(now)
        ttl_us = _timedelta_to_ttl_us(ttl)
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            now_us = self._timestamp_us(connection, now)
            new_expiry = now_us + ttl_us
            result = connection.execute(
                update(table)
                .where(_live_lease_predicate(table, fanout_key, worker, fencing_token, now_us))
                .values(
                    lease_expires_at_us=case(
                        (table.c.lease_expires_at_us < new_expiry, new_expiry),
                        else_=table.c.lease_expires_at_us,
                    ),
                    updated_at_us=case(
                        (table.c.updated_at_us < now_us, now_us),
                        else_=table.c.updated_at_us,
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            return _select_group(connection, fanout_key)

    def advance_cursor(
        self,
        *,
        fanout_key: str,
        worker: str,
        fencing_token: int,
        expected_index: int,
        new_index: int,
        now: datetime,
    ) -> FanoutGroupRecord | None:
        """Advance one accepted chunk under the current unexpired group lease.

        The cursor must move forward by at most ``MAX_FANOUT_CHUNK_SIZE``.
        Every successful chunk releases its lease so a pump processes only a
        bounded chunk per claim. Reaching the member count sets ``ENQUEUED``;
        that state means every child run was accepted, not that it completed.
        """
        _validate_fanout_key(fanout_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        _validate_index(expected_index, "expected_index")
        _validate_index(new_index, "new_index")
        if new_index <= expected_index:
            raise ValueError("new_index must be greater than expected_index")
        if new_index - expected_index > MAX_FANOUT_CHUNK_SIZE:
            raise ValueError(f"cursor advance exceeds {MAX_FANOUT_CHUNK_SIZE} symbols")
        _datetime_to_epoch_us(now)
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            now_us = self._timestamp_us(connection, now)
            completed = table.c.member_count == new_index
            result = connection.execute(
                update(table)
                .where(
                    and_(
                        _live_lease_predicate(table, fanout_key, worker, fencing_token, now_us),
                        table.c.next_index == expected_index,
                        new_index <= table.c.member_count,
                    )
                )
                .values(
                    next_index=new_index,
                    status=case((completed, ENQUEUED), else_=PENDING),
                    lease_owner=None,
                    lease_expires_at_us=None,
                    enqueued_at_us=case((completed, now_us), else_=table.c.enqueued_at_us),
                    next_attempt_at_us=None,
                    last_error_code=None,
                    updated_at_us=case(
                        (table.c.updated_at_us < now_us, now_us),
                        else_=table.c.updated_at_us,
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            return _select_group(connection, fanout_key)

    def release_for_retry(
        self,
        *,
        fanout_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        retry_delay: timedelta,
        error_code: str,
    ) -> FanoutGroupRecord | None:
        """Release a lease and schedule retry relative to the selected UTC clock.

        ``retry_delay`` is added to the same database/caller timestamp used for
        lease validation, avoiding host-clock offsets in multi-worker mode.
        The cursor remains unchanged for deterministic child-run replay.
        """
        _validate_fanout_key(fanout_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        _datetime_to_epoch_us(now)
        retry_delay_us = _retry_delay_to_us(retry_delay)
        _validate_error_code(error_code)
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            now_us = self._timestamp_us(connection, now)
            retry_at_us = now_us + retry_delay_us
            result = connection.execute(
                update(table)
                .where(_live_lease_predicate(table, fanout_key, worker, fencing_token, now_us))
                .values(
                    lease_owner=None,
                    lease_expires_at_us=None,
                    attempt_count=table.c.attempt_count + 1,
                    next_attempt_at_us=retry_at_us,
                    last_error_code=error_code,
                    updated_at_us=case(
                        (table.c.updated_at_us < now_us, now_us),
                        else_=table.c.updated_at_us,
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            return _select_group(connection, fanout_key)

    def block(
        self,
        *,
        fanout_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        error_code: str,
    ) -> FanoutGroupRecord | None:
        """Permanently block a live group without allowing terminal rollback."""
        _validate_fanout_key(fanout_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        _datetime_to_epoch_us(now)
        _validate_error_code(error_code)
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.begin() as connection:
            _begin_sqlite_write(connection)
            now_us = self._timestamp_us(connection, now)
            result = connection.execute(
                update(table)
                .where(_live_lease_predicate(table, fanout_key, worker, fencing_token, now_us))
                .values(
                    status=BLOCKED,
                    lease_owner=None,
                    lease_expires_at_us=None,
                    next_attempt_at_us=None,
                    last_error_code=error_code,
                    updated_at_us=case(
                        (table.c.updated_at_us < now_us, now_us),
                        else_=table.c.updated_at_us,
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            return _select_group(connection, fanout_key)

    def get(self, fanout_key: str) -> FanoutGroupRecord | None:
        """Load and validate one durable fanout group by its deterministic key."""
        _validate_fanout_key(fanout_key)
        with self._engine.connect() as connection:
            return _select_group(connection, fanout_key)

    def _get_by_slot(self, job_id: str, occurrence_us: int) -> FanoutGroupRecord | None:
        """Resolve a unique logical slot after a concurrent insert conflict."""
        table = SINA_FZB_FANOUT_GROUP_TABLE
        with self._engine.connect() as connection:
            result = (
                connection.execute(
                    select(table).where(
                        and_(
                            table.c.job_id == job_id,
                            table.c.occurrence_at_us == occurrence_us,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _record_from_row(result) if result is not None else None

    def _timestamp_us(
        self, connection: Connection, caller_timestamp: datetime | None = None
    ) -> int:
        """Read the selected UTC clock on the active write connection."""
        if self._clock_policy == "database":
            return _database_clock_us(connection)
        timestamp = caller_timestamp or datetime.now(timezone.utc)
        return _datetime_to_epoch_us(timestamp)


def _validate_manifest(
    manifest_json: str,
    job_id: str,
    job_version: str,
    occurrence_us: int,
) -> tuple[str, ...]:
    """Validate and parse the exact bounded v1 manifest shape."""
    if not isinstance(manifest_json, str):
        raise ValueError("manifest_json must be a JSON string")
    try:
        encoded = manifest_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("manifest_json must be valid UTF-8") from exc
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest_json exceeds {MAX_MANIFEST_BYTES} UTF-8 bytes")

    def reject_constant(value: str) -> None:
        """Reject non-standard JSON constants such as NaN and Infinity."""
        raise ValueError(f"invalid JSON constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Reject ambiguous repeated keys instead of silently taking the last."""
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            manifest_json,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except RecursionError as exc:
        raise ValueError("manifest_json nesting exceeds the supported parser depth") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("manifest_json must contain valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_MANIFEST_KEYS:
        raise ValueError("manifest_json must contain the exact contract-v1 fields")
    if type(payload["contract_version"]) is not int or payload["contract_version"] != 1:
        raise ValueError("manifest contract_version must be integer 1")
    if payload["job_id"] != job_id or payload["job_version"] != job_version:
        raise ValueError("manifest job identity must match the journal arguments")
    occurrence_text = (
        _epoch_us_to_datetime(occurrence_us)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    if payload["occurrence"] != occurrence_text:
        raise ValueError("manifest occurrence must match the UTC journal argument")
    _validate_opaque_ref(payload["manifest_id"], "manifest_id")
    _validate_opaque_ref(payload["selection_snapshot_ref"], "selection_snapshot_ref")
    raw_symbols = payload["symbols"]
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= MAX_FANOUT_SYMBOLS:
        raise ValueError(f"manifest symbols must contain 1..{MAX_FANOUT_SYMBOLS} entries")
    symbols: list[str] = []
    for symbol in raw_symbols:
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            raise ValueError("manifest symbols must use six-digit .SH/.SZ identifiers")
        symbols.append(symbol)
    if symbols != sorted(set(symbols)):
        raise ValueError("manifest symbols must be sorted and unique")
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if canonical != manifest_json:
        raise ValueError("manifest_json must use canonical JSON encoding")
    return tuple(symbols)


def _record_from_row(row: RowMapping) -> FanoutGroupRecord:
    """Validate persisted identity fields before exposing the immutable record."""
    job_id = _as_string(row["job_id"], "job_id")
    if job_id != SINA_FZB_JOB_ID:
        raise FanoutJournalCorruptionError("persisted fanout job identity is invalid")
    job_version = _as_string(row["job_version"], "job_version")
    try:
        _validate_identifier(job_version, "job_version")
    except ValueError as exc:
        raise FanoutJournalCorruptionError("persisted fanout job version is invalid") from exc
    occurrence = _persisted_datetime(row["occurrence_at_us"], "occurrence_at_us")
    manifest_json = _as_string(row["manifest_json"], "manifest_json")
    try:
        symbols = _validate_manifest(
            manifest_json,
            job_id,
            job_version,
            _datetime_to_epoch_us(occurrence),
        )
    except ValueError as exc:
        raise FanoutJournalCorruptionError("persisted fanout manifest is invalid") from exc
    manifest_hash = _sha256_text(manifest_json)
    expected_key = _fanout_key(
        job_id,
        occurrence.isoformat(timespec="microseconds").replace("+00:00", "Z"),
    )
    if row["fanout_key"] != expected_key or row["manifest_sha256"] != manifest_hash:
        raise FanoutJournalCorruptionError("persisted fanout identity or manifest hash is invalid")
    if _as_integer(row["member_count"], "member_count") != len(symbols):
        raise FanoutJournalCorruptionError("persisted fanout member count does not match manifest")
    next_index = _as_integer(row["next_index"], "next_index")
    if next_index < 0 or next_index > len(symbols):
        raise FanoutJournalCorruptionError("persisted fanout cursor is outside manifest bounds")
    status = _as_status(row["status"])
    lease_owner = _as_optional_string(row["lease_owner"], "lease_owner")
    lease_expires_at = _as_optional_datetime(row["lease_expires_at_us"], "lease_expires_at_us")
    enqueued_at = _as_optional_datetime(row["enqueued_at_us"], "enqueued_at_us")
    if (lease_owner is None) != (lease_expires_at is None):
        raise FanoutJournalCorruptionError("persisted fanout lease fields are inconsistent")
    if status == ENQUEUED and (
        next_index != len(symbols)
        or lease_owner is not None
        or lease_expires_at is not None
        or enqueued_at is None
    ):
        raise FanoutJournalCorruptionError("persisted enqueued group is incomplete")
    if status != ENQUEUED and enqueued_at is not None:
        raise FanoutJournalCorruptionError("persisted active group has an enqueue timestamp")
    if status == PENDING and next_index == len(symbols):
        raise FanoutJournalCorruptionError("persisted pending group has a completed cursor")
    if status == BLOCKED and lease_owner is not None:
        raise FanoutJournalCorruptionError("persisted blocked group still holds a lease")
    last_error_code = _as_optional_string(row["last_error_code"], "last_error_code")
    if last_error_code is not None:
        try:
            _validate_error_code(last_error_code)
        except ValueError as exc:
            raise FanoutJournalCorruptionError("persisted fanout error code is invalid") from exc
    if status == BLOCKED and last_error_code is None:
        raise FanoutJournalCorruptionError("persisted blocked group has no error code")
    if status == ENQUEUED and last_error_code is not None:
        raise FanoutJournalCorruptionError("persisted enqueued group has an error code")
    fencing_token = _as_integer(row["fencing_token"], "fencing_token")
    attempt_count = _as_integer(row["attempt_count"], "attempt_count")
    if fencing_token < 0 or attempt_count < 0:
        raise FanoutJournalCorruptionError("persisted fanout counters must be non-negative")
    return FanoutGroupRecord(
        fanout_key=expected_key,
        job_id=job_id,
        job_version=job_version,
        occurrence=occurrence,
        manifest_sha256=manifest_hash,
        manifest_json=manifest_json,
        symbols=symbols,
        next_index=next_index,
        status=status,
        lease_owner=lease_owner,
        fencing_token=fencing_token,
        lease_expires_at=lease_expires_at,
        attempt_count=attempt_count,
        next_attempt_at=_as_optional_datetime(row["next_attempt_at_us"], "next_attempt_at_us"),
        last_error_code=last_error_code,
        created_at=_persisted_datetime(row["created_at_us"], "created_at_us"),
        updated_at=_persisted_datetime(row["updated_at_us"], "updated_at_us"),
        enqueued_at=enqueued_at,
    )


def _select_group(connection: Connection, fanout_key: str) -> FanoutGroupRecord | None:
    """Read one group row within the current connection/transaction."""
    row = (
        connection.execute(
            select(SINA_FZB_FANOUT_GROUP_TABLE).where(
                SINA_FZB_FANOUT_GROUP_TABLE.c.fanout_key == fanout_key
            )
        )
        .mappings()
        .one_or_none()
    )
    return _record_from_row(row) if row is not None else None


def _claimable_predicate(now_us: int) -> ColumnElement[bool]:
    """Return the SQL predicate for due, unleased or expired pending groups."""
    table = SINA_FZB_FANOUT_GROUP_TABLE
    return and_(
        table.c.status == PENDING,
        or_(table.c.next_attempt_at_us.is_(None), table.c.next_attempt_at_us <= now_us),
        or_(table.c.lease_owner.is_(None), table.c.lease_expires_at_us <= now_us),
    )


def _live_lease_predicate(
    table: Table,
    fanout_key: str,
    worker: str,
    fencing_token: int,
    now_us: int,
) -> ColumnElement[bool]:
    """Build one compare-and-set predicate for live group ownership."""
    return and_(
        table.c.fanout_key == fanout_key,
        table.c.status == PENDING,
        table.c.lease_owner == worker,
        table.c.fencing_token == fencing_token,
        table.c.lease_expires_at_us > now_us,
    )


def _fanout_key(job_id: str, occurrence: str) -> str:
    """Hash the scheduled natural slot, excluding mutable job version/payload."""
    slot = json.dumps(
        {"contract_version": 1, "job_id": job_id, "occurrence": occurrence},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return _sha256_text(slot)


def _validate_identifier(value: str, field: str) -> None:
    """Validate one bounded immutable job identifier."""
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded identifier")


def _validate_opaque_ref(value: object, field: str) -> None:
    """Validate one bounded opaque manifest reference without embedding secrets."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be a non-empty bounded opaque reference")


def _validate_worker(worker: str) -> None:
    """Reject empty, whitespace-padded, or oversized worker identifiers."""
    if not isinstance(worker, str) or not worker.strip() or worker != worker.strip():
        raise ValueError("worker must be a non-empty identifier without edge whitespace")
    if len(worker) > MAX_WORKER_LENGTH:
        raise ValueError(f"worker must be at most {MAX_WORKER_LENGTH} characters")


def _validate_fanout_key(fanout_key: str) -> None:
    """Require a lowercase SHA-256 hex digest before database access."""
    if not isinstance(fanout_key, str) or re.fullmatch(r"[0-9a-f]{64}", fanout_key) is None:
        raise ValueError("fanout_key must be a lowercase SHA-256 hex digest")


def _validate_fencing_token(fencing_token: int) -> None:
    """Require an exact non-negative integer fencing token."""
    if type(fencing_token) is not int or fencing_token < 0:
        raise ValueError("fencing_token must be a non-negative integer")


def _validate_index(index: int, field: str) -> None:
    """Require an exact non-negative integer cursor position."""
    if type(index) is not int or index < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _validate_error_code(error_code: str) -> None:
    """Accept only short machine error codes, never arbitrary exception text."""
    if not isinstance(error_code, str) or _ERROR_CODE_PATTERN.fullmatch(error_code) is None:
        raise ValueError("error_code must be a short uppercase machine code")


def _datetime_to_epoch_us(value: datetime) -> int:
    """Validate an aware UTC timestamp and encode it as integer microseconds."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware UTC datetimes")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must use UTC")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _epoch_us_to_datetime(value: object) -> datetime:
    """Convert a validated stored UTC microsecond timestamp."""
    return _EPOCH + timedelta(microseconds=_as_integer(value, "timestamp"))


def _timedelta_to_ttl_us(ttl: timedelta) -> int:
    """Validate and encode a lease or retry duration."""
    if not isinstance(ttl, timedelta) or ttl < MIN_TTL or ttl > MAX_TTL:
        raise ValueError(f"ttl must be between {MIN_TTL} and {MAX_TTL}")
    return (ttl.days * 86_400 + ttl.seconds) * _MICROSECONDS_PER_SECOND + ttl.microseconds


def _retry_delay_to_us(retry_delay: timedelta) -> int:
    """Validate a non-negative retry delay bounded to thirty days."""
    if (
        not isinstance(retry_delay, timedelta)
        or retry_delay < timedelta(0)
        or retry_delay > MAX_RETRY_DELAY
    ):
        raise ValueError("retry_delay must be between zero and 30 days")
    return (
        retry_delay.days * 86_400 + retry_delay.seconds
    ) * _MICROSECONDS_PER_SECOND + retry_delay.microseconds


def _database_clock_us(connection: Connection) -> int:
    """Read database UTC time for multi-host lease coordination."""
    dialect_name = connection.dialect.name
    if dialect_name == "mysql":
        value = connection.exec_driver_sql("SELECT UTC_TIMESTAMP(6)").scalar_one()
    elif dialect_name == "postgresql":
        value = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    elif dialect_name == "sqlite":
        value = connection.exec_driver_sql(
            "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')"
        ).scalar_one()
        if not isinstance(value, str):
            raise RuntimeError("SQLite database clock returned a non-text timestamp")
        try:
            value = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise RuntimeError("SQLite database clock returned an invalid timestamp") from exc
    else:
        raise RuntimeError(f"database clock is unsupported for {dialect_name!r}")
    if not isinstance(value, datetime):
        raise RuntimeError(f"{dialect_name} database clock returned an invalid timestamp")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _datetime_to_epoch_us(value.astimezone(timezone.utc))


def _begin_sqlite_write(connection: Connection) -> None:
    """Acquire SQLite's writer reservation before selecting a lease candidate."""
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _as_status(value: object) -> FanoutGroupStatus:
    """Return a typed persisted state after corruption checks."""
    if value == PENDING:
        return PENDING
    if value == ENQUEUED:
        return ENQUEUED
    if value == BLOCKED:
        return BLOCKED
    raise FanoutJournalCorruptionError("persisted fanout status is invalid")


def _as_integer(value: object, field: str) -> int:
    """Return an exact integer column or report a corrupt row."""
    if type(value) is not int:
        raise FanoutJournalCorruptionError(f"persisted fanout {field} is invalid")
    return value


def _as_string(value: object, field: str) -> str:
    """Return a text column or report a corrupt row."""
    if not isinstance(value, str):
        raise FanoutJournalCorruptionError(f"persisted fanout {field} is invalid")
    return value


def _as_optional_string(value: object, field: str) -> str | None:
    """Return an optional persisted string or report corruption."""
    if value is None:
        return None
    return _as_string(value, field)


def _persisted_datetime(value: object, field: str) -> datetime:
    """Convert a persisted timestamp, normalizing only out-of-range corruption."""
    try:
        return _epoch_us_to_datetime(value)
    except OverflowError as exc:
        raise FanoutJournalCorruptionError(
            f"persisted fanout {field} is outside datetime range"
        ) from exc


def _as_optional_datetime(value: object, field: str) -> datetime | None:
    """Convert an optional persisted timestamp or preserve its NULL value."""
    if value is None:
        return None
    return _persisted_datetime(value, field)


def _sha256_text(value: str) -> str:
    """Hash the canonical UTF-8 representation of one text value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BLOCKED",
    "ENQUEUED",
    "MAX_FANOUT_CHUNK_SIZE",
    "MAX_FANOUT_SYMBOLS",
    "PENDING",
    "SINA_FZB_FANOUT_GROUP_TABLE",
    "SINA_FZB_FANOUT_METADATA",
    "SINA_FZB_JOB_ID",
    "FanoutGroupRecord",
    "FanoutGroupStatus",
    "FanoutJournalCorruptionError",
    "FanoutManifestConflictError",
    "SinaFzbFanoutJournal",
]
