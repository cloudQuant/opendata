"""Persistent run identity, lease, and state transitions for pipeline jobs.

The table is stored in the warehouse database so a successful run transition
can commit atomically with the facts it produced. This module does not create
the table, connect to application settings, or wire itself into the production
scheduler. Callers must construct it with the warehouse engine.

Result callbacks are trusted, reviewed application code. Transaction-event
guards catch ordinary SQLAlchemy boundary mistakes; they are not a Python
sandbox and cannot intercept access through raw DBAPI handles or other engines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, TypeVar
from uuid import uuid4

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
    event,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Callable

RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

QUEUED: RunStatus = "queued"
RUNNING: RunStatus = "running"
SUCCEEDED: RunStatus = "succeeded"
FAILED: RunStatus = "failed"
CANCELLED: RunStatus = "cancelled"
TERMINAL_STATUSES = (SUCCEEDED, FAILED, CANCELLED)

MIN_TTL = timedelta(seconds=1)
MAX_TTL = timedelta(days=1)
MAX_RUN_KEY_LENGTH = 255
MAX_WORKER_LENGTH = 255
MAX_SNAPSHOT_BYTES = 1_048_576
MAX_ERROR_TEXT_BYTES = 1_048_576

RUN_LEDGER_METADATA = MetaData()
RUN_LEDGER_TABLE = Table(
    "od_run_ledger",
    RUN_LEDGER_METADATA,
    Column(
        "run_key",
        String(MAX_RUN_KEY_LENGTH).with_variant(
            String(MAX_RUN_KEY_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        primary_key=True,
    ),
    Column(
        "job_snapshot_json",
        Text().with_variant(MEDIUMTEXT(), "mysql"),
        nullable=False,
    ),
    Column(
        "status",
        String(16).with_variant(String(16, collation="utf8mb4_0900_bin"), "mysql"),
        nullable=False,
    ),
    Column(
        "worker",
        String(MAX_WORKER_LENGTH).with_variant(
            String(MAX_WORKER_LENGTH, collation="utf8mb4_0900_bin"), "mysql"
        ),
        nullable=True,
    ),
    Column(
        "lease_id",
        String(32).with_variant(String(32, collation="utf8mb4_0900_bin"), "mysql"),
        nullable=True,
    ),
    Column("fencing_token", BigInteger, nullable=False, default=0),
    Column("lease_expires_at_us", BigInteger, nullable=True),
    Column("created_at_us", BigInteger, nullable=False),
    Column("updated_at_us", BigInteger, nullable=False),
    Column("started_at_us", BigInteger, nullable=True),
    Column("heartbeat_at_us", BigInteger, nullable=True),
    Column("finished_at_us", BigInteger, nullable=True),
    Column("error_text", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True),
    CheckConstraint(
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
        name="ck_od_run_ledger_status",
    ),
    CheckConstraint("fencing_token >= 0", name="ck_od_run_ledger_fencing_token"),
)
Index(
    "ix_od_run_ledger_claim",
    RUN_LEDGER_TABLE.c.status,
    RUN_LEDGER_TABLE.c.lease_expires_at_us,
    RUN_LEDGER_TABLE.c.created_at_us,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECONDS_PER_SECOND = 1_000_000
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """A read-only snapshot of one pipeline run's ledger state."""

    run_key: str
    job_snapshot_json: str
    status: RunStatus
    worker: str | None
    lease_id: str | None
    fencing_token: int
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    error_text: str | None


class RunLedgerConflictError(ValueError):
    """Raised when one idempotency key is reused for a different job snapshot."""


class RunLeaseExpiredError(RuntimeError):
    """Raised when a lease expires while its fenced warehouse write is running."""


class RunTransactionBoundaryError(RuntimeError):
    """Raised when a fenced write callback tries to end or replace its transaction."""


class RunLedger:
    """Operate on an explicitly supplied warehouse SQLAlchemy engine.

    The caller owns table creation and engine lifetime. Instantiate this class
    only after the table exists; the constructor performs no database I/O. The
    engine must target the same database as facts written by ``commit_result``.
    The default ``caller`` clock policy preserves the same-host/test contract;
    production workers spread across hosts must select ``database`` so lease
    decisions use the warehouse server's UTC clock.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock_policy: Literal["caller", "database"] = "caller",
    ) -> None:
        """Bind an engine and select either caller-host or warehouse-server time.

        ``caller`` preserves the compatibility contract for tests and workers
        sharing one host clock. Multi-host production workers must select
        ``database``; that policy never falls back to the local host clock.
        """
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

    def _timestamp_us(
        self, connection: Connection, caller_timestamp: datetime | None = None
    ) -> int:
        """Read the policy-selected UTC clock on the active warehouse connection."""
        if self._clock_policy == "database":
            return _database_clock_us(connection)
        if caller_timestamp is None:
            caller_timestamp = _application_utc_now()
        return _datetime_to_epoch_us(caller_timestamp)

    def _begin_database_write(self, connection: Connection) -> None:
        """Acquire SQLite's writer lock before reading time for a write transaction."""
        if self._clock_policy == "database" and connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")

    def _validate_timestamp_argument(self, timestamp: datetime) -> None:
        """Validate API timestamp shape even when database policy ignores its value."""
        _datetime_to_epoch_us(timestamp)

    def enqueue(self, run_key: str, jobsnapshot_json: str) -> RunRecord:
        """Insert a queued run once and return the stored record on retries.

        Reusing a key with a different immutable job snapshot raises
        :class:`RunLedgerConflictError` so a changed payload cannot be hidden by
        an idempotent retry.
        """
        _validate_run_key(run_key)
        _validate_snapshot(jobsnapshot_json)
        try:
            with self._engine.begin() as connection:
                self._begin_database_write(connection)
                now_us = self._timestamp_us(connection)
                connection.execute(
                    insert(RUN_LEDGER_TABLE).values(
                        run_key=run_key,
                        job_snapshot_json=jobsnapshot_json,
                        status=QUEUED,
                        worker=None,
                        lease_id=None,
                        fencing_token=0,
                        lease_expires_at_us=None,
                        created_at_us=now_us,
                        updated_at_us=now_us,
                        started_at_us=None,
                        heartbeat_at_us=None,
                        finished_at_us=None,
                        error_text=None,
                    )
                )
                row = _select_run(connection, run_key)
        except IntegrityError:
            existing = self.get(run_key)
            if existing is None:
                raise
            if existing.job_snapshot_json != jobsnapshot_json:
                raise RunLedgerConflictError(
                    "run_key already exists with a different job snapshot"
                ) from None
            return existing
        if row is None:
            raise RuntimeError("inserted run ledger row could not be read")
        return row

    def claim(self, worker: str, now: datetime, ttl: timedelta) -> RunRecord | None:
        """Atomically claim one queued run or reclaim an expired lease.

        A successful claim increments the fencing token. A live lease is never
        stolen, and an expired worker's token cannot be used after reclamation.
        """
        _validate_worker(worker)
        self._validate_timestamp_argument(now)
        ttl_us = _timedelta_to_ttl_us(ttl)
        lease_id = uuid4().hex
        with self._engine.begin() as connection:
            self._begin_database_write(connection)
            now_us = self._timestamp_us(connection, now)
            claimable = or_(
                RUN_LEDGER_TABLE.c.status == QUEUED,
                and_(
                    RUN_LEDGER_TABLE.c.status == RUNNING,
                    RUN_LEDGER_TABLE.c.lease_expires_at_us <= now_us,
                ),
            )
            candidate_key = (
                select(RUN_LEDGER_TABLE.c.run_key)
                .where(claimable)
                .order_by(RUN_LEDGER_TABLE.c.created_at_us, RUN_LEDGER_TABLE.c.run_key)
                .limit(1)
            )
            if self._engine.dialect.name in {"mysql", "postgresql"}:
                candidate = connection.execute(
                    candidate_key.with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if candidate is None:
                    return None
                claim_filter = RUN_LEDGER_TABLE.c.run_key == candidate
            else:
                claim_filter = RUN_LEDGER_TABLE.c.run_key == candidate_key.scalar_subquery()
            result = connection.execute(
                update(RUN_LEDGER_TABLE)
                .where(claim_filter, claimable)
                .values(
                    status=RUNNING,
                    worker=worker,
                    lease_id=lease_id,
                    fencing_token=RUN_LEDGER_TABLE.c.fencing_token + 1,
                    lease_expires_at_us=now_us + ttl_us,
                    updated_at_us=now_us,
                    started_at_us=case(
                        (RUN_LEDGER_TABLE.c.started_at_us.is_(None), now_us),
                        else_=RUN_LEDGER_TABLE.c.started_at_us,
                    ),
                    heartbeat_at_us=now_us,
                    finished_at_us=None,
                    error_text=None,
                )
            )
            if result.rowcount != 1:
                return None
            return _select_run_by_lease(connection, lease_id)

    def heartbeat(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> RunRecord | None:
        """Extend a live lease only when its current owner and token match."""
        _validate_run_key(run_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        self._validate_timestamp_argument(now)
        ttl_us = _timedelta_to_ttl_us(ttl)
        return self._leased_transition(
            run_key,
            worker,
            fencing_token,
            now,
            lambda now_us: {
                "lease_expires_at_us": now_us + ttl_us,
                "heartbeat_at_us": now_us,
                "updated_at_us": now_us,
            },
        )

    def finish(
        self, run_key: str, worker: str, fencing_token: int, now: datetime
    ) -> RunRecord | None:
        """Mark a live run successful if the owner and fencing token still match."""
        _validate_run_key(run_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        self._validate_timestamp_argument(now)
        return self._leased_transition(
            run_key,
            worker,
            fencing_token,
            now,
            lambda now_us: {
                "status": SUCCEEDED,
                "worker": None,
                "lease_id": None,
                "lease_expires_at_us": None,
                "updated_at_us": now_us,
                "finished_at_us": now_us,
                "error_text": None,
            },
        )

    def commit_result(
        self,
        run_key: str,
        *,
        worker: str,
        fencing_token: int,
        now: datetime,
        operation: Callable[[Connection], _ResultT],
        clock: Callable[[], datetime] | None = None,
    ) -> tuple[RunRecord, _ResultT] | None:
        """Commit warehouse writes and run success under the current lease.

        ``operation`` receives the connection holding the run-ledger lock. It
        must perform only bounded SQL writes through that connection: it must
        not fetch from a provider, open another engine, commit, roll back, or
        otherwise end the outer transaction. SQLAlchemy ``begin_nested()``
        scopes are allowed; direct transaction-control SQL and raw DBAPI
        transaction methods are not. Its return value is paired with the
        succeeded record. A stale, cancelled, or already expired lease returns
        ``None`` without invoking ``operation``. If the lease expires while the
        operation runs, :class:`RunLeaseExpiredError` is raised and both the
        operation's writes and the success transition roll back.

        This contract applies to trusted callbacks obeying the supplied
        connection protocol. Raw driver access and opening another connection
        bypass SQLAlchemy's transaction events and are unsupported. Never pass
        arbitrary user code or dynamically imported scripts as ``operation``.

        SQLite acquires its reserved write lock through a conditional update
        before reading the lease; MySQL and PostgreSQL lock the row with
        ``SELECT FOR UPDATE``. This prevents cancellation or lease reclamation
        from racing the warehouse write. Under the ``database`` clock policy,
        both lease checks use the warehouse's UTC clock and a ``clock`` override
        is rejected. The override exists only for deterministic tests under the
        ``caller`` policy.
        """
        _validate_run_key(run_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        self._validate_timestamp_argument(now)
        if not callable(operation):
            raise TypeError("operation must be callable")
        if self._clock_policy == "database" and clock is not None:
            raise ValueError("clock overrides are forbidden with database clock policy")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        commit_clock = clock or _application_utc_now

        try:
            with self._engine.begin() as connection:
                if self._engine.dialect.name == "sqlite":
                    self._begin_database_write(connection)
                    now_us = self._timestamp_us(connection, now)
                    # SQLite has no row-level FOR UPDATE. This CAS obtains its
                    # database-wide reserved write lock before user SQL runs.
                    locked = connection.execute(
                        update(RUN_LEDGER_TABLE)
                        .where(
                            and_(
                                RUN_LEDGER_TABLE.c.run_key == run_key,
                                RUN_LEDGER_TABLE.c.status == RUNNING,
                                RUN_LEDGER_TABLE.c.worker == worker,
                                RUN_LEDGER_TABLE.c.fencing_token == fencing_token,
                                RUN_LEDGER_TABLE.c.lease_expires_at_us > now_us,
                            )
                        )
                        .values(
                            updated_at_us=case(
                                (RUN_LEDGER_TABLE.c.updated_at_us < now_us, now_us),
                                else_=RUN_LEDGER_TABLE.c.updated_at_us,
                            )
                        )
                    )
                    if locked.rowcount != 1:
                        return None
                else:
                    locked_row = (
                        connection.execute(
                            select(RUN_LEDGER_TABLE)
                            .where(RUN_LEDGER_TABLE.c.run_key == run_key)
                            .with_for_update()
                        )
                        .mappings()
                        .one_or_none()
                    )
                    now_us = self._timestamp_us(connection, now)
                    if not _row_has_live_lease(locked_row, worker, fencing_token, now_us):
                        return None

                before_operation = _select_run(connection, run_key)
                if before_operation is None:
                    return None
                if not _record_has_live_lease(before_operation, worker, fencing_token, now_us):
                    return None

                transaction_guard = _CallbackTransactionGuard(connection)
                transaction_guard.install()
                try:
                    operation_result = operation(connection)
                finally:
                    transaction_guard.remove()
                transaction_guard.raise_if_violated()
                commit_us = (
                    self._timestamp_us(connection)
                    if self._clock_policy == "database"
                    else _datetime_to_epoch_us(commit_clock())
                )
                minimum_commit_us = max(
                    now_us,
                    _datetime_to_epoch_us(before_operation.updated_at),
                )
                if commit_us < minimum_commit_us:
                    raise ValueError("commit clock must not be earlier than now or run updated_at")
                if commit_us >= _record_lease_expiry_us(before_operation):
                    raise RunLeaseExpiredError("run lease expired before warehouse result commit")

                # Re-read inside the still-held transaction in case the
                # callback accidentally touched the ledger row. The guarded
                # update ensures it cannot silently turn another state into
                # success.
                current = _select_run(connection, run_key)
                if current != before_operation or not _record_has_live_lease(
                    current, worker, fencing_token, commit_us
                ):
                    raise _LostRunLeaseError
                finished = connection.execute(
                    update(RUN_LEDGER_TABLE)
                    .where(
                        and_(
                            RUN_LEDGER_TABLE.c.run_key == run_key,
                            RUN_LEDGER_TABLE.c.status == RUNNING,
                            RUN_LEDGER_TABLE.c.worker == worker,
                            RUN_LEDGER_TABLE.c.fencing_token == fencing_token,
                            RUN_LEDGER_TABLE.c.lease_expires_at_us > commit_us,
                        )
                    )
                    .values(
                        status=SUCCEEDED,
                        worker=None,
                        lease_id=None,
                        lease_expires_at_us=None,
                        updated_at_us=commit_us,
                        finished_at_us=commit_us,
                        error_text=None,
                    )
                )
                if finished.rowcount != 1:
                    raise _LostRunLeaseError
                record = _select_run(connection, run_key)
                if record is None:
                    raise RuntimeError("committed run ledger row could not be read")
            return record, operation_result
        except _LostRunLeaseError:
            # Raising inside engine.begin() rolls back callback writes before
            # the stale-worker outcome is translated back to the public API.
            return None

    def fail(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        error_text: str,
    ) -> RunRecord | None:
        """Mark a live run failed if the owner and fencing token still match."""
        _validate_run_key(run_key)
        _validate_worker(worker)
        _validate_fencing_token(fencing_token)
        _validate_error_text(error_text)
        self._validate_timestamp_argument(now)
        return self._leased_transition(
            run_key,
            worker,
            fencing_token,
            now,
            lambda now_us: {
                "status": FAILED,
                "worker": None,
                "lease_id": None,
                "lease_expires_at_us": None,
                "updated_at_us": now_us,
                "finished_at_us": now_us,
                "error_text": error_text,
            },
        )

    def cancel(self, run_key: str, now: datetime) -> RunRecord | None:
        """Cancel a queued or running run and invalidate its current token.

        Repeated cancellation of a terminal run returns that terminal record;
        terminal state never moves back to an active state.
        """
        _validate_run_key(run_key)
        self._validate_timestamp_argument(now)
        with self._engine.begin() as connection:
            self._begin_database_write(connection)
            if self._clock_policy == "database" and connection.dialect.name in {
                "mysql",
                "postgresql",
            }:
                connection.execute(
                    select(RUN_LEDGER_TABLE.c.run_key)
                    .where(RUN_LEDGER_TABLE.c.run_key == run_key)
                    .with_for_update()
                ).scalar_one_or_none()
            now_us = self._timestamp_us(connection, now)
            connection.execute(
                update(RUN_LEDGER_TABLE)
                .where(
                    and_(
                        RUN_LEDGER_TABLE.c.run_key == run_key,
                        RUN_LEDGER_TABLE.c.status.in_((QUEUED, RUNNING)),
                    )
                )
                .values(
                    status=CANCELLED,
                    worker=None,
                    lease_id=None,
                    fencing_token=RUN_LEDGER_TABLE.c.fencing_token + 1,
                    lease_expires_at_us=None,
                    updated_at_us=now_us,
                    finished_at_us=now_us,
                    error_text=None,
                )
            )
            return _select_run(connection, run_key)

    def get(self, run_key: str) -> RunRecord | None:
        """Return the current record for ``run_key``, if it exists."""
        _validate_run_key(run_key)
        with self._engine.connect() as connection:
            return _select_run(connection, run_key)

    def _leased_transition(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        values: Callable[[int], dict[str, object]],
    ) -> RunRecord | None:
        """Apply one live-lease compare-and-set transition."""
        with self._engine.begin() as connection:
            self._begin_database_write(connection)
            if self._clock_policy == "database" and connection.dialect.name in {
                "mysql",
                "postgresql",
            }:
                locked = connection.execute(
                    select(RUN_LEDGER_TABLE.c.run_key)
                    .where(RUN_LEDGER_TABLE.c.run_key == run_key)
                    .with_for_update()
                ).scalar_one_or_none()
                if locked is None:
                    return None
            now_us = self._timestamp_us(connection, now)
            result = connection.execute(
                update(RUN_LEDGER_TABLE)
                .where(
                    and_(
                        RUN_LEDGER_TABLE.c.run_key == run_key,
                        RUN_LEDGER_TABLE.c.status == RUNNING,
                        RUN_LEDGER_TABLE.c.worker == worker,
                        RUN_LEDGER_TABLE.c.fencing_token == fencing_token,
                        RUN_LEDGER_TABLE.c.lease_expires_at_us > now_us,
                    )
                )
                .values(**values(now_us))
            )
            if result.rowcount != 1:
                return None
            return _select_run(connection, run_key)


def _select_run(connection: Connection, run_key: str) -> RunRecord | None:
    """Read a single row and convert its integer UTC timestamps."""
    result = (
        connection.execute(select(RUN_LEDGER_TABLE).where(RUN_LEDGER_TABLE.c.run_key == run_key))
        .mappings()
        .one_or_none()
    )
    if result is None:
        return None
    return _record_from_row(result)


def _row_has_live_lease(
    row: RowMapping | None, worker: str, fencing_token: int, now_us: int
) -> bool:
    """Check a locked database row against the requested live lease."""
    return (
        row is not None
        and row["status"] == RUNNING
        and row["worker"] == worker
        and row["fencing_token"] == fencing_token
        and isinstance(row["lease_expires_at_us"], int)
        and row["lease_expires_at_us"] > now_us
    )


def _record_has_live_lease(
    record: RunRecord | None, worker: str, fencing_token: int, now_us: int
) -> bool:
    """Check a public ledger snapshot against the requested live lease."""
    return (
        record is not None
        and record.status == RUNNING
        and record.worker == worker
        and record.fencing_token == fencing_token
        and record.lease_expires_at is not None
        and _datetime_to_epoch_us(record.lease_expires_at) > now_us
    )


def _record_lease_expiry_us(record: RunRecord) -> int:
    """Return the required lease deadline from a claimed record."""
    if record.lease_expires_at is None:
        raise RuntimeError("running run ledger row has no lease expiry")
    return _datetime_to_epoch_us(record.lease_expires_at)


class _LostRunLeaseError(RuntimeError):
    """Force rollback when ownership changes during a fenced callback."""


_ALLOWED_CALLBACK_SQL = frozenset(
    {"DELETE", "INSERT", "MERGE", "REPLACE", "SELECT", "UPDATE", "WITH"}
)
_SQL_KEYWORD = re.compile(r"\A\s*([A-Za-z_]+)")
_SQL_LEADING_COMMENTS = re.compile(
    r"\A(?:\s|--[^\r\n]*(?:\r?\n|\Z)|\#[^\r\n]*(?:\r?\n|\Z)|/\*.*?\*/)*",
    re.DOTALL,
)
_SAVEPOINT_NAME = re.compile(r"\A(?:_?sa_savepoint_\d+)\Z", re.IGNORECASE)
_SAVEPOINT_SQL = re.compile(r"\ASAVEPOINT\s+([A-Za-z_][A-Za-z0-9_]*)\Z", re.IGNORECASE)
_ROLLBACK_SAVEPOINT_SQL = re.compile(
    r"\AROLLBACK\s+TO(?:\s+SAVEPOINT)?\s+([A-Za-z_][A-Za-z0-9_]*)\Z",
    re.IGNORECASE,
)
_RELEASE_SAVEPOINT_SQL = re.compile(
    r"\ARELEASE(?:\s+SAVEPOINT)?\s+([A-Za-z_][A-Za-z0-9_]*)\Z",
    re.IGNORECASE,
)


class _CallbackTransactionGuard:
    """Reject transaction boundaries and implicit-commit SQL on one connection."""

    def __init__(self, connection: Connection) -> None:
        """Bind event guards to the exact transaction connection."""
        self._connection = connection
        self._violation: str | None = None
        self._pending_savepoint_sql: tuple[str, str | None] | None = None
        self._listeners: list[tuple[str, Callable[..., None]]] = [
            ("commit", self._reject_root_commit),
            ("rollback", self._reject_root_rollback),
            ("savepoint", self._allow_savepoint),
            ("rollback_savepoint", self._allow_rollback_savepoint),
            ("release_savepoint", self._allow_release_savepoint),
            ("before_cursor_execute", self._reject_transaction_sql),
        ]

    def install(self) -> None:
        """Install the boundary listeners before calling user-owned storage code."""
        for event_name, listener in self._listeners:
            event.listen(self._connection, event_name, listener)

    def remove(self) -> None:
        """Remove listeners before the repository completes or rolls back."""
        for event_name, listener in reversed(self._listeners):
            event.remove(self._connection, event_name, listener)

    def raise_if_violated(self) -> None:
        """Fail the outer transaction if a callback caught a guard exception."""
        if self._violation is not None:
            raise RunTransactionBoundaryError(self._violation)

    def _reject_root_commit(self, _connection: Connection) -> None:
        """Roll back the DBAPI transaction before vetoing Connection.commit()."""
        self._reject_root_boundary("Connection.commit()")

    def _reject_root_rollback(self, _connection: Connection) -> None:
        """Roll back the DBAPI transaction before vetoing Connection.rollback()."""
        self._reject_root_boundary("Connection.rollback()")

    def _reject_root_boundary(self, method_name: str) -> None:
        """Undo SQLAlchemy's root transaction before it marks it inactive."""
        self._violation = f"{method_name} is forbidden in a fenced write"
        try:
            # ConnectionEvents fire before SQLAlchemy deactivates its
            # RootTransaction. A listener exception alone would leave some
            # drivers' DBAPI transaction open when the logical transaction is
            # deactivated, so roll the DBAPI transaction back before vetoing.
            self._connection.connection.rollback()
        except BaseException as exc:
            raise RunTransactionBoundaryError(self._violation) from exc
        raise RunTransactionBoundaryError(self._violation)

    def _allow_savepoint(self, _connection: Connection, _name: str | None = None) -> None:
        """Permit a SQLAlchemy-owned nested savepoint, but no raw savepoint SQL."""
        self._set_pending_savepoint_sql("SAVEPOINT", None)

    def _allow_rollback_savepoint(
        self, _connection: Connection, name: str, _context: object
    ) -> None:
        """Permit SQLAlchemy to roll back only the named nested savepoint."""
        self._set_pending_savepoint_sql("ROLLBACK TO SAVEPOINT", name)

    def _allow_release_savepoint(
        self, _connection: Connection, name: str, _context: object
    ) -> None:
        """Permit SQLAlchemy to release only the named nested savepoint."""
        self._set_pending_savepoint_sql("RELEASE SAVEPOINT", name)

    def _set_pending_savepoint_sql(self, command: str, name: str | None) -> None:
        """Authorize the single dialect statement emitted by a savepoint event."""
        if self._violation is not None:
            raise RunTransactionBoundaryError(self._violation)
        if self._pending_savepoint_sql is not None:
            self._violation = "unexpected nested transaction command in a fenced write"
            raise RunTransactionBoundaryError(self._violation)
        self._pending_savepoint_sql = (command, name)

    def _consume_savepoint_sql(self, statement: str) -> bool:
        """Match a pending SQLAlchemy savepoint event to its exact SQL statement."""
        pending = self._pending_savepoint_sql
        if pending is None:
            return False
        self._pending_savepoint_sql = None
        command, expected_name = pending
        if command == "SAVEPOINT":
            match = _SAVEPOINT_SQL.fullmatch(statement)
            valid = match is not None and _SAVEPOINT_NAME.fullmatch(match.group(1)) is not None
        elif command == "ROLLBACK TO SAVEPOINT":
            match = _ROLLBACK_SAVEPOINT_SQL.fullmatch(statement)
            valid = match is not None and match.group(1) == expected_name
        else:
            match = _RELEASE_SAVEPOINT_SQL.fullmatch(statement)
            valid = match is not None and match.group(1) == expected_name
        if not valid:
            self._violation = "unexpected savepoint SQL in a fenced write"
            raise RunTransactionBoundaryError(self._violation)
        return True

    def _reject_transaction_sql(
        self,
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        """Allow one DML/SELECT statement and reject transaction or DDL SQL."""
        if self._violation is not None:
            raise RunTransactionBoundaryError(self._violation)
        if ";" in statement:
            self._violation = "multi-statement SQL is forbidden in a fenced write"
            raise RunTransactionBoundaryError(self._violation)
        if self._consume_savepoint_sql(statement):
            return
        uncommented = _SQL_LEADING_COMMENTS.sub("", statement, count=1)
        match = _SQL_KEYWORD.match(uncommented)
        keyword = match.group(1).upper() if match is not None else ""
        if keyword not in _ALLOWED_CALLBACK_SQL:
            self._violation = (
                "only single-statement warehouse DML or SELECT is allowed in a fenced write"
            )
            raise RunTransactionBoundaryError(self._violation)


def _record_from_row(row: RowMapping) -> RunRecord:
    """Convert a validated SQL row mapping to a public immutable record."""
    return RunRecord(
        run_key=_as_string(row["run_key"], "run_key"),
        job_snapshot_json=_as_string(row["job_snapshot_json"], "job_snapshot_json"),
        status=_as_status(row["status"]),
        worker=_as_optional_string(row["worker"], "worker"),
        lease_id=_as_optional_string(row["lease_id"], "lease_id"),
        fencing_token=_as_integer(row["fencing_token"], "fencing_token"),
        lease_expires_at=_as_optional_datetime(row["lease_expires_at_us"]),
        created_at=_epoch_us_to_datetime(row["created_at_us"]),
        updated_at=_epoch_us_to_datetime(row["updated_at_us"]),
        started_at=_as_optional_datetime(row["started_at_us"]),
        heartbeat_at=_as_optional_datetime(row["heartbeat_at_us"]),
        finished_at=_as_optional_datetime(row["finished_at_us"]),
        error_text=_as_optional_string(row["error_text"], "error_text"),
    )


def _select_run_by_lease(connection: Connection, lease_id: str) -> RunRecord:
    """Read the single run row written by the current claim statement."""
    result = (
        connection.execute(select(RUN_LEDGER_TABLE).where(RUN_LEDGER_TABLE.c.lease_id == lease_id))
        .mappings()
        .one_or_none()
    )
    if result is None:
        raise RuntimeError("claimed run ledger row could not be read")
    return _record_from_row(result)


def _validate_run_key(run_key: str) -> None:
    """Reject empty or oversized run identities before database access."""
    if not isinstance(run_key, str) or not run_key.strip():
        raise ValueError("run_key must be a non-empty string")
    if run_key != run_key.strip():
        raise ValueError("run_key must not have leading or trailing whitespace")
    if len(run_key) > MAX_RUN_KEY_LENGTH:
        raise ValueError(f"run_key must be at most {MAX_RUN_KEY_LENGTH} characters")


def _validate_worker(worker: str) -> None:
    """Reject invalid lease owner identifiers before database access."""
    if not isinstance(worker, str) or not worker.strip():
        raise ValueError("worker must be a non-empty string")
    if worker != worker.strip():
        raise ValueError("worker must not have leading or trailing whitespace")
    if len(worker) > MAX_WORKER_LENGTH:
        raise ValueError(f"worker must be at most {MAX_WORKER_LENGTH} characters")


def _validate_snapshot(jobsnapshot_json: str) -> None:
    """Require canonical JSON and reject oversized or secret-bearing snapshots."""
    if not isinstance(jobsnapshot_json, str):
        raise ValueError("jobsnapshot_json must be a JSON string")
    try:
        encoded_snapshot = jobsnapshot_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("jobsnapshot_json must be valid UTF-8") from exc
    if len(encoded_snapshot) > MAX_SNAPSHOT_BYTES:
        raise ValueError(f"jobsnapshot_json exceeds {MAX_SNAPSHOT_BYTES} UTF-8 bytes")

    def reject_constant(value: str) -> None:
        """Reject NaN and Infinity, which are outside the JSON standard."""
        raise ValueError(f"invalid JSON constant: {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """Reject duplicate object keys instead of silently keeping the last one."""
        values: dict[str, object] = {}
        for key, value in pairs:
            if key in values:
                raise ValueError("duplicate JSON object key")
            values[key] = value
        return values

    try:
        snapshot: object = json.loads(
            jobsnapshot_json,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("jobsnapshot_json must contain valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise ValueError("jobsnapshot_json must encode a JSON object")
    _reject_secret_fields(snapshot)
    canonical_json = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        canonical_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("jobsnapshot_json must be valid UTF-8") from exc
    if canonical_json != jobsnapshot_json:
        raise ValueError("jobsnapshot_json must use canonical JSON encoding")


def _validate_error_text(error_text: str) -> None:
    """Bound stored run errors to the warehouse's 1 MiB diagnostic budget."""
    if not isinstance(error_text, str) or not error_text.strip():
        raise ValueError("error_text must be a non-empty string")
    try:
        encoded_error = error_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("error_text must be valid UTF-8") from exc
    if len(encoded_error) > MAX_ERROR_TEXT_BYTES:
        raise ValueError(f"error_text exceeds {MAX_ERROR_TEXT_BYTES} UTF-8 bytes")


def _reject_secret_fields(value: object) -> None:
    """Prevent snapshots from persisting credential-like fields."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("snapshot object keys must be strings")
            normalized_key = key.casefold().replace("_", "").replace("-", "")
            if any(
                marker in normalized_key
                for marker in (
                    "apikey",
                    "secret",
                    "password",
                    "credential",
                    "authorization",
                    "cookie",
                    "token",
                    "privatekey",
                )
            ):
                raise ValueError("jobsnapshot_json must not contain credential-like fields")
            _reject_secret_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_fields(nested)


def _validate_fencing_token(fencing_token: int) -> None:
    """Require a non-negative integer fencing token."""
    if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
        raise ValueError("fencing_token must be an integer")
    if fencing_token < 0:
        raise ValueError("fencing_token must be non-negative")


def _datetime_to_epoch_us(value: datetime) -> int:
    """Validate an aware UTC timestamp and encode it as integer microseconds."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC datetimes")
    if value.utcoffset() != timedelta(0):
        raise ValueError("timestamps must use UTC")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * _MICROSECONDS_PER_SECOND + delta.microseconds


def _application_utc_now() -> datetime:
    """Read the local host clock for the legacy caller-clock policy only."""
    return datetime.now(timezone.utc)


def _database_clock_us(connection: Connection) -> int:
    """Read an authoritative UTC timestamp from a supported warehouse dialect."""
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
        # MySQL UTC_TIMESTAMP(6) is UTC by definition but drivers return it naive.
        value = value.replace(tzinfo=timezone.utc)
    return _datetime_to_epoch_us(value.astimezone(timezone.utc))


def _timedelta_to_ttl_us(ttl: timedelta) -> int:
    """Validate a lease duration and convert it to integer microseconds."""
    if not isinstance(ttl, timedelta) or ttl < MIN_TTL or ttl > MAX_TTL:
        raise ValueError(f"ttl must be between {MIN_TTL} and {MAX_TTL}")
    return (ttl.days * 86_400 + ttl.seconds) * _MICROSECONDS_PER_SECOND + ttl.microseconds


def _epoch_us_to_datetime(value: object) -> datetime:
    """Convert a stored UTC microsecond timestamp to an aware datetime."""
    return _EPOCH + timedelta(microseconds=_as_integer(value, "timestamp"))


def _as_optional_datetime(value: object) -> datetime | None:
    """Convert an optional stored integer timestamp."""
    if value is None:
        return None
    return _epoch_us_to_datetime(value)


def _as_integer(value: object, field: str) -> int:
    """Return an integer database value or report a corrupt ledger row."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"run ledger has invalid {field}")
    return value


def _as_string(value: object, field: str) -> str:
    """Return a string database value or report a corrupt ledger row."""
    if not isinstance(value, str):
        raise RuntimeError(f"run ledger has invalid {field}")
    return value


def _as_optional_string(value: object, field: str) -> str | None:
    """Return an optional string database value or report a corrupt row."""
    if value is None:
        return None
    return _as_string(value, field)


def _as_status(value: object) -> RunStatus:
    """Convert a stored status to the run-state literal type."""
    if value in (QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED):
        return value
    raise RuntimeError("run ledger has invalid status")


__all__ = [
    "RUN_LEDGER_METADATA",
    "RUN_LEDGER_TABLE",
    "RunLedger",
    "RunLedgerConflictError",
    "RunLeaseExpiredError",
    "RunRecord",
    "RunStatus",
    "RunTransactionBoundaryError",
]
