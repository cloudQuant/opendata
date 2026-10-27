"""Persistent declarative job definitions for the OpenData control plane.

The module only describes its SQLAlchemy table at import time. Constructing a
store performs no I/O; callers must pass an explicit engine and invoke
``create_schema`` before use. No legacy script or source parameters are stored.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import IntegrityError

from opendata.pipeline.run_identity import TaskDefinition

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

JOB_DEFINITION_METADATA = MetaData()
MAX_JOB_DEFINITION_LIST_ROWS = 10_000
JOB_DEFINITION_TABLE = Table(
    "od_job_definitions",
    JOB_DEFINITION_METADATA,
    Column(
        "legacy_task_id",
        String(255).with_variant(String(255, collation="utf8mb4_bin"), "mysql"),
        primary_key=True,
    ),
    Column(
        "job_id",
        String(128).with_variant(String(128, collation="utf8mb4_bin"), "mysql"),
        nullable=False,
    ),
    Column(
        "version",
        String(128).with_variant(String(128, collation="utf8mb4_bin"), "mysql"),
        nullable=False,
    ),
    Column("definition_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
    Column("identity_sha256", String(64), nullable=False),
    Column("provenance_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
    Column("provenance_sha256", String(64), nullable=False),
    Column("receipt_sha256", String(64), nullable=False),
    Column("created_at_us", BigInteger, nullable=False),
    CheckConstraint("length(identity_sha256) = 64", name="ck_od_job_def_identity_sha_len"),
    CheckConstraint("length(provenance_sha256) = 64", name="ck_od_job_def_provenance_sha_len"),
    CheckConstraint("length(receipt_sha256) = 64", name="ck_od_job_def_receipt_sha_len"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)
Index(
    "ux_od_job_definitions_job_version",
    JOB_DEFINITION_TABLE.c.job_id,
    JOB_DEFINITION_TABLE.c.version,
    unique=True,
)


class JobDefinitionStoreError(RuntimeError):
    """Raised when persistent job definition metadata is inconsistent."""


class JobDefinitionConflictError(JobDefinitionStoreError):
    """Raised when an imported task identity or its pinned provenance changes."""


@dataclass(frozen=True, slots=True)
class StoredJobDefinition:
    """Validated stored task definition and its non-secret audit receipt."""

    legacy_task_id: str
    definition: TaskDefinition
    identity_sha256: str
    provenance: dict[str, object]
    provenance_sha256: str
    receipt_sha256: str
    created_at_us: int


@dataclass(frozen=True, slots=True)
class SaveJobDefinitionResult:
    """Result of an idempotent insert into the definition metadata table."""

    record: StoredJobDefinition
    created: bool


class JobDefinitionStore:
    """Persist default-disabled ``TaskDefinition`` rows on an explicit engine.

    The constructor does not connect to the engine. It never reads application
    settings, creates a database, schedules work, or changes run/checkpoint
    state. The caller owns the engine and schema lifecycle.
    """

    def __init__(self, engine: Engine) -> None:
        """Bind a synchronous SQLAlchemy engine without performing I/O.

        Args:
            engine: Explicit caller-owned control-metadata engine.

        Raises:
            TypeError: If ``engine`` is not a synchronous SQLAlchemy Engine.
        """
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        self._engine = engine

    @property
    def engine(self) -> Engine:
        """Return the explicit engine bound to this store."""
        return self._engine

    def create_schema(self) -> None:
        """Create the prototype table on an explicitly owned test engine.

        Production deployments must create this table through the repository's
        Alembic migration, not by calling this convenience method.
        """
        JOB_DEFINITION_TABLE.create(self._engine, checkfirst=True)

    def get(self, legacy_task_id: str) -> StoredJobDefinition | None:
        """Load one definition by its immutable legacy task key."""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    select(JOB_DEFINITION_TABLE).where(
                        JOB_DEFINITION_TABLE.c.legacy_task_id == legacy_task_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        return _record_from_row(row) if row is not None else None

    def list_all(
        self, *, limit: int = MAX_JOB_DEFINITION_LIST_ROWS
    ) -> tuple[StoredJobDefinition, ...]:
        """Load a bounded stable page of stored definitions.

        Args:
            limit: Maximum number of rows to materialize, capped at 10,000.

        Raises:
            ValueError: If the requested bound is invalid.
            JobDefinitionStoreError: If more rows exist than the requested bound.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_JOB_DEFINITION_LIST_ROWS:
            raise ValueError("limit must be between 1 and 10000")
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    select(JOB_DEFINITION_TABLE)
                    .order_by(JOB_DEFINITION_TABLE.c.legacy_task_id)
                    .limit(limit + 1)
                )
                .mappings()
                .all()
            )
        if len(rows) > limit:
            raise JobDefinitionStoreError("definition list exceeds requested row limit")
        return tuple(_record_from_row(row) for row in rows)

    def _save_reviewed_definition(
        self,
        *,
        legacy_task_id: str,
        definition: TaskDefinition,
        provenance: Mapping[str, object],
        receipt_sha256: str,
        verify_target_unchanged: Callable[[], None],
    ) -> SaveJobDefinitionResult:
        """Persist one validated definition or return an exact idempotent replay.

        This internal method is intentionally called only after the migration
        importer has recomputed all input, plan, mapping, and target pins.
        """
        task = _revalidate_definition(definition)
        definition_payload = task.model_dump(mode="json")
        definition_json = _canonical_json(definition_payload)
        identity_sha256 = _sha256(definition_json.encode("utf-8"))
        provenance_payload = _canonical_mapping(provenance)
        provenance_json = _canonical_json(provenance_payload)
        provenance_sha256 = _sha256(provenance_json.encode("utf-8"))
        if not _is_sha256(receipt_sha256):
            raise JobDefinitionStoreError("import receipt hash is invalid")

        expected = {
            "legacy_task_id": legacy_task_id,
            "job_id": task.job_id,
            "version": task.version,
            "definition_json": definition_json,
            "identity_sha256": identity_sha256,
            "provenance_json": provenance_json,
            "provenance_sha256": provenance_sha256,
            "receipt_sha256": receipt_sha256,
        }
        table = JOB_DEFINITION_TABLE
        with self._engine.begin() as connection:
            existing = (
                connection.execute(select(table).where(table.c.legacy_task_id == legacy_task_id))
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                record = _record_from_row(existing)
                _require_exact_replay(record, expected)
                verify_target_unchanged()
                return SaveJobDefinitionResult(record=record, created=False)

            identity_owner = (
                connection.execute(
                    select(table.c.legacy_task_id).where(
                        table.c.job_id == task.job_id,
                        table.c.version == task.version,
                    )
                )
                .scalars()
                .one_or_none()
            )
            if identity_owner is not None:
                raise JobDefinitionConflictError(
                    "job_id/version is already claimed by another legacy task"
                )

            created_at_us = time.time_ns() // 1_000
            values = {**expected, "created_at_us": created_at_us}
            try:
                # SQLite's legacy transaction mode may release its first
                # SAVEPOINT as a commit before BEGIN. A direct insert also
                # remains statement-scoped on MySQL/InnoDB, so the outer
                # engine.begin() owns rollback for both supported dialects.
                connection.execute(insert(table).values(**values))
            except IntegrityError:
                # A concurrent importer may have won the primary-key race.
                existing = (
                    connection.execute(
                        select(table).where(table.c.legacy_task_id == legacy_task_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    identity_owner = (
                        connection.execute(
                            select(table.c.legacy_task_id).where(
                                table.c.job_id == task.job_id,
                                table.c.version == task.version,
                            )
                        )
                        .scalars()
                        .one_or_none()
                    )
                    if identity_owner is not None:
                        raise JobDefinitionConflictError(
                            "job_id/version is already claimed by another legacy task"
                        ) from None
                    raise JobDefinitionStoreError(
                        "concurrent definition insert was not visible"
                    ) from None
                record = _record_from_row(existing)
                _require_exact_replay(record, expected)
                verify_target_unchanged()
                return SaveJobDefinitionResult(record=record, created=False)

            row = (
                connection.execute(select(table).where(table.c.legacy_task_id == legacy_task_id))
                .mappings()
                .one()
            )
            verify_target_unchanged()
            return SaveJobDefinitionResult(record=_record_from_row(row), created=True)


def _revalidate_definition(value: TaskDefinition) -> TaskDefinition:
    """Reconstruct a definition from declared fields to reject mutated models."""
    if not isinstance(value, TaskDefinition):
        raise JobDefinitionStoreError("definition must be a TaskDefinition")
    try:
        return TaskDefinition.model_validate(value.model_dump(mode="python"))
    except ValidationError as exc:
        raise JobDefinitionStoreError("definition validation failed") from exc


def _canonical_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Copy provenance through strict JSON to prevent mutable/custom objects."""
    if type(value) is not dict:
        raise JobDefinitionStoreError("provenance must be a JSON object")
    try:
        copied = json.loads(_canonical_json(value))
    except (TypeError, ValueError, RecursionError) as exc:
        raise JobDefinitionStoreError("provenance must be canonical JSON") from exc
    if type(copied) is not dict:
        raise JobDefinitionStoreError("provenance must be a JSON object")
    return copied


def _canonical_json(value: object) -> str:
    """Encode compact deterministic JSON with finite numeric values only."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise JobDefinitionStoreError("value is not canonical JSON") from exc


def _record_from_row(row: RowMapping) -> StoredJobDefinition:
    """Validate persisted JSON and hashes before returning a public record."""
    definition_json = row["definition_json"]
    provenance_json = row["provenance_json"]
    if not isinstance(definition_json, str) or not isinstance(provenance_json, str):
        raise JobDefinitionStoreError("stored definition JSON is invalid")
    try:
        definition = TaskDefinition.model_validate_json(definition_json, strict=True)
        provenance_value = json.loads(provenance_json)
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise JobDefinitionStoreError("stored definition or provenance failed validation") from exc
    if type(provenance_value) is not dict:
        raise JobDefinitionStoreError("stored provenance is not an object")
    identity = _sha256(definition_json.encode("utf-8"))
    provenance_sha = _sha256(provenance_json.encode("utf-8"))
    if identity != row["identity_sha256"] or provenance_sha != row["provenance_sha256"]:
        raise JobDefinitionStoreError("stored definition digest mismatch")
    receipt = row["receipt_sha256"]
    created_at_us = row["created_at_us"]
    if not isinstance(receipt, str) or not _is_sha256(receipt):
        raise JobDefinitionStoreError("stored import receipt hash is invalid")
    if type(created_at_us) is not int:
        raise JobDefinitionStoreError("stored definition timestamp is invalid")
    return StoredJobDefinition(
        legacy_task_id=str(row["legacy_task_id"]),
        definition=definition,
        identity_sha256=identity,
        provenance=provenance_value,
        provenance_sha256=provenance_sha,
        receipt_sha256=receipt,
        created_at_us=created_at_us,
    )


def _require_exact_replay(record: StoredJobDefinition, expected: Mapping[str, object]) -> None:
    """Reject task-identity or source-provenance drift for an existing key."""
    same = (
        record.legacy_task_id == expected["legacy_task_id"]
        and record.definition.job_id == expected["job_id"]
        and record.definition.version == expected["version"]
        and record.identity_sha256 == expected["identity_sha256"]
        and record.provenance_sha256 == expected["provenance_sha256"]
        and record.receipt_sha256 == expected["receipt_sha256"]
    )
    if not same:
        raise JobDefinitionConflictError(
            "existing legacy task definition has changed identity or pins"
        )


def _is_sha256(value: object) -> bool:
    """Return whether a value is a canonical lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256(value: bytes) -> str:
    """Return the canonical SHA-256 digest of bytes."""
    return hashlib.sha256(value).hexdigest()
