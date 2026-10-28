"""Synthetic, offline tests for explicit source-grant provisioning."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import V2_OBJECT_SQL, V2_REVISION
from opendata.data.source_attempt_gate import (
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceGrantInspectionReason,
    SourceGrantInspectionRequest,
    SourceGrantInspectionStatus,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisioningCleanupError,
    SourceGrantProvisioningCommittedCleanupError,
    SourceGrantProvisioningCommitUncertainError,
    SourceGrantProvisioningConflictError,
    SourceGrantProvisioningStatus,
    SourceGrantProvisioningUnavailableError,
    SourceGrantProvisionRequest,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "source_grant_provisioning_0015_test"
GRANT_ID = "synthetic-grant"
SOURCE = "fuyao"
MODEL = "stock-financial-report"
OPERATION = "query"
PRINCIPAL = "local-research-service"
HOST = "api.fuyao.example"
EVIDENCE_REFERENCE = "operator-declaration:synthetic-test"
EVIDENCE_SHA256 = hashlib.sha256(b"synthetic evidence only").hexdigest()
ATTESTATION = "I declare the listed internal use is authorized for this source."
OPERATOR = "local-operator"
GATE_TABLE_QUERIES = (
    ("od_source_gate_clock", "SELECT * FROM od_source_gate_clock ORDER BY rowid"),
    ("od_source_gate_policies", "SELECT * FROM od_source_gate_policies ORDER BY rowid"),
    (
        "od_source_gate_source_bindings",
        "SELECT * FROM od_source_gate_source_bindings ORDER BY rowid",
    ),
    ("od_source_gate_state", "SELECT * FROM od_source_gate_state ORDER BY rowid"),
    ("od_source_grants", "SELECT * FROM od_source_grants ORDER BY rowid"),
    ("od_source_grant_hosts", "SELECT * FROM od_source_grant_hosts ORDER BY rowid"),
    ("od_source_grant_host_sets", "SELECT * FROM od_source_grant_host_sets ORDER BY rowid"),
    ("od_source_grant_events", "SELECT * FROM od_source_grant_events ORDER BY rowid"),
    (
        "od_source_attempt_reservations",
        "SELECT * FROM od_source_attempt_reservations ORDER BY rowid",
    ),
    (
        "od_source_attempt_scope_v2",
        "SELECT * FROM od_source_attempt_scope_v2 ORDER BY rowid",
    ),
)


class _MutableOffsetTzInfo(tzinfo):
    """Synthetic timezone whose offset can change after request construction."""

    def __init__(self, offset: timedelta) -> None:
        self.offset = offset
        self.utcoffset_calls = 0

    def utcoffset(self, _value: datetime | None) -> timedelta:
        self.utcoffset_calls += 1
        return self.offset

    def dst(self, _value: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _value: datetime | None) -> str:
        return "synthetic-mutable-offset"


def _load_migration() -> ModuleType:
    """Load the exact isolated 0015 revision without loading app settings."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_schema(engine: Engine) -> None:
    """Install the isolated 0015 schema and source-gate v2 marker in test storage."""
    migration = _load_migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            text(
                "CREATE TABLE alembic_version_source_gate ( "
                "version_num VARCHAR(32) NOT NULL, CONSTRAINT "
                "alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
            )
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)


def _request(**overrides: object) -> SourceGrantProvisionRequest:
    """Build one synthetic exact-scope request for the isolated fixture."""
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "grant_id": GRANT_ID,
        "revision": 1,
        "source": SOURCE,
        "canonical_model": MODEL,
        "operation": OPERATION,
        "principal_scope": PRINCIPAL,
        "host": HOST,
        "not_before": now - timedelta(seconds=30),
        "expires_at": now + timedelta(seconds=30),
        "rights_evidence_reference": EVIDENCE_REFERENCE,
        "rights_evidence_sha256": EVIDENCE_SHA256,
        "operator_confirms_authorized_use": True,
        "operator_attestation_note": ATTESTATION,
        "attested_by": OPERATOR,
        "conditions": (),
    }
    values.update(overrides)
    return SourceGrantProvisionRequest(**values)  # type: ignore[arg-type]


def _snapshot(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Read gate rows to verify all-or-nothing persistence in owned storage."""
    result: dict[str, tuple[tuple[object, ...], ...]] = {}
    with engine.connect() as connection:
        for table_name, statement in GATE_TABLE_QUERIES:
            rows = connection.exec_driver_sql(statement).all()
            result[table_name] = tuple(tuple(row) for row in rows)
    return result


@pytest.fixture
def prepared_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Provide a file-backed gate migrated only inside pytest's temporary folder."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'source-grant-provisioning.sqlite'}",
        poolclass=NullPool,
    )
    _install_schema(engine)
    gate = SourceAttemptGate(engine)
    yield engine, gate
    engine.dispose()


def test_provision_commits_exact_host_seal_and_operator_declaration(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """One operator declaration creates the exact grant and trigger-owned seal event."""
    engine, gate = prepared_gate
    result = SourceGrantProvisioner(gate).provision(_request())

    assert result.status is SourceGrantProvisioningStatus.PROVISIONED
    assert result.grant_id == GRANT_ID
    assert result.revision == 1
    assert result.host == HOST
    assert result.expires_at_us - result.not_before_us == 60_000_000
    with engine.connect() as connection:
        grant = (
            connection.execute(
                text(
                    "SELECT decision, source, canonical_model, operation, principal_scope, "
                    "revision, not_before_us, expires_at_us, conditions_json, "
                    "rights_evidence_reference, rights_evidence_sha256, "
                    "operator_attestation_note, attested_by, attested_at_us "
                    "FROM od_source_grants WHERE grant_id = :grant_id"
                ),
                {"grant_id": GRANT_ID},
            )
            .mappings()
            .one()
        )
        host = connection.execute(
            text("SELECT host FROM od_source_grant_hosts WHERE grant_id = :grant_id"),
            {"grant_id": GRANT_ID},
        ).scalar_one()
        seal = (
            connection.execute(
                text(
                    "SELECT revision, host_count, sealed_by, reason_code "
                    "FROM od_source_grant_host_sets WHERE grant_id = :grant_id"
                ),
                {"grant_id": GRANT_ID},
            )
            .mappings()
            .one()
        )
        events = (
            connection.execute(
                text(
                    "SELECT event_type, old_revision, new_revision, actor, reason_code "
                    "FROM od_source_grant_events WHERE grant_id = :grant_id"
                ),
                {"grant_id": GRANT_ID},
            )
            .mappings()
            .all()
        )

    assert grant["decision"] == "ALLOWED"
    assert (grant["source"], grant["canonical_model"]) == (SOURCE, MODEL)
    assert (grant["operation"], grant["principal_scope"]) == (OPERATION, PRINCIPAL)
    assert grant["revision"] == result.revision
    assert grant["not_before_us"] == result.not_before_us
    assert grant["expires_at_us"] == result.expires_at_us
    assert grant["conditions_json"] == "[]"
    assert grant["rights_evidence_reference"] == EVIDENCE_REFERENCE
    assert grant["rights_evidence_sha256"] == EVIDENCE_SHA256
    assert "Local operator self-declaration" in grant["operator_attestation_note"]
    assert "not independently verified" in grant["operator_attestation_note"]
    assert grant["attested_by"] == OPERATOR
    assert result.not_before_us <= grant["attested_at_us"] < result.expires_at_us
    assert host == HOST
    assert tuple(seal.values()) == (1, 1, OPERATOR, "operator-self-declaration")
    assert len(events) == 1
    assert tuple(events[0].values()) == (
        "HOSTS_SEALED",
        1,
        1,
        OPERATOR,
        "operator-self-declaration",
    )


def test_empty_conditions_are_supported_and_id_collision_rolls_back_every_table(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Empty conditions work; permanent ID conflicts do not move even the gate clock."""
    engine, gate = prepared_gate
    provisioner = SourceGrantProvisioner(gate)
    assert _request().conditions == ()
    provisioner.provision(_request())
    before = _snapshot(engine)

    with pytest.raises(SourceGrantProvisioningConflictError):
        provisioner.provision(_request())

    assert _snapshot(engine) == before


def test_failure_after_grant_and_host_inserts_rolls_back_all_gate_rows(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A seal insert failure leaves no partial grant, host, event, or clock update."""
    engine, gate = prepared_gate
    before = _snapshot(engine)

    def fail_on_host_set_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO od_source_grant_host_sets"):
            raise sa.exc.OperationalError(
                statement,
                {},
                RuntimeError("synthetic host seal failure"),
            )

    event.listen(engine, "before_cursor_execute", fail_on_host_set_insert)
    try:
        with pytest.raises(SourceGrantProvisioningUnavailableError):
            SourceGrantProvisioner(gate).provision(_request())
    finally:
        event.remove(engine, "before_cursor_execute", fail_on_host_set_insert)

    assert _snapshot(engine) == before


@pytest.mark.parametrize(
    "field_name",
    [
        "grant_id",
        "source",
        "canonical_model",
        "operation",
        "principal_scope",
        "host",
        "revision",
        "not_before_us",
        "expires_at_us",
        "rights_evidence_reference",
        "rights_evidence_sha256",
        "operator_attestation_note",
        "attested_by",
        "attested_at_us",
        "created_at_us",
    ],
)
def test_grant_fields_are_verified_against_request_before_commit(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    field_name: str,
) -> None:
    """A persisted grant that differs from the declaration is rolled back atomically."""
    engine, gate = prepared_gate
    before = _snapshot(engine)
    tampered_fields: list[str] = []

    def alter_inserted_grant(
        _connection: object,
        clauseelement: object,
        multiparams: object,
        params: object,
        execution_options: object,
    ) -> tuple[object, object, object]:
        statement = str(clauseelement).lstrip()
        targets = {
            "grant_id": (
                "INSERT INTO od_source_grants",
                "INSERT INTO od_source_grant_hosts",
                "INSERT INTO od_source_grant_host_sets",
            ),
            "revision": ("INSERT INTO od_source_grants", "INSERT INTO od_source_grant_host_sets"),
            "host": ("INSERT INTO od_source_grant_hosts",),
        }
        statement_prefixes = targets.get(field_name, ("INSERT INTO od_source_grants",))
        if statement.startswith(statement_prefixes):
            if not isinstance(params, Mapping):
                raise AssertionError("insert parameters are not a mapping")
            altered_params = dict(params)
            parameter_name = {
                "rights_evidence_reference": "evidence_reference",
                "rights_evidence_sha256": "evidence_sha256",
                "operator_attestation_note": "attestation_note",
            }.get(field_name, field_name)
            value = altered_params[parameter_name]
            if field_name in {"not_before_us", "revision"}:
                altered_params[parameter_name] = value + 1  # type: ignore[operator]
            elif field_name in {"expires_at_us", "attested_at_us", "created_at_us"}:
                altered_params[parameter_name] = value - 1  # type: ignore[operator]
            elif field_name == "operation":
                altered_params[parameter_name] = "export"
            elif field_name == "rights_evidence_sha256":
                altered_params[parameter_name] = "0" * 64
            elif field_name == "operator_attestation_note":
                altered_params[parameter_name] = "synthetic alternate declaration"
            elif field_name == "host":
                altered_params[parameter_name] = "api.other.example"
            else:
                altered_params[parameter_name] = f"{value}-tampered"
            if field_name not in tampered_fields:
                tampered_fields.append(field_name)
            return clauseelement, multiparams, altered_params
        return clauseelement, multiparams, params

    event.listen(engine, "before_execute", alter_inserted_grant, retval=True)
    try:
        with pytest.raises(SourceGrantProvisioningUnavailableError) as error:
            SourceGrantProvisioner(gate).provision(_request())
    finally:
        event.remove(engine, "before_execute", alter_inserted_grant)

    assert str(error.value) == "source grant seal verification failed"
    assert tampered_fields == [field_name]
    assert _snapshot(engine) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"revision": True},
        {"revision": 2},
        {"operation": "query; export"},
        {"host": "*.fuyao.example"},
        {"host": "https://api.fuyao.example/v1"},
        {"not_before": datetime.now()},
        {"expires_at": datetime.now()},
        {"operator_confirms_authorized_use": False},
        {"operator_confirms_authorized_use": 1},
        {"rights_evidence_sha256": "A" * 64},
        {"rights_evidence_sha256": "sk-synthetic-canary-1234567890"},
        {"conditions": ("redistribution",)},
        {"operator_attestation_note": "api_key=synthetic-secret-value"},
        {"rights_evidence_reference": ".env"},
        {"rights_evidence_reference": "https://user:synthetic-password@example.test/policy"},
    ],
)
def test_invalid_or_broad_declarations_are_rejected_before_storage(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    overrides: dict[str, object],
) -> None:
    """Strict types, bounded lifetime, exact host, and secret-safe text are mandatory."""
    engine, gate = prepared_gate
    before = _snapshot(engine)
    with pytest.raises(SourceAttemptInputError):
        request = _request(**overrides)
        SourceGrantProvisioner(gate).provision(request)
    assert _snapshot(engine) == before


def test_existing_gate_schema_must_pass_full_validation_before_any_grant_write(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A schema object outside the pinned gate contract blocks issuance."""
    engine, gate = prepared_gate
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE od_source_unreviewed_extra (value TEXT)")

    with pytest.raises(SourceGrantProvisioningUnavailableError):
        SourceGrantProvisioner(gate).provision(_request())

    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM od_source_grants").scalar_one() == 0
        assert (
            connection.exec_driver_sql("SELECT highwater_us FROM od_source_gate_clock").scalar_one()
            == 0
        )


def test_schema_validation_uses_and_restores_row_factory_on_locked_connection(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pinned-schema validation runs on the writer connection and restores its DBAPI state."""
    _engine, gate = prepared_gate
    connection = gate._open_write_connection()
    raw_connection = connection.connection.driver_connection
    original_row_factory = raw_connection.row_factory
    validator = gate._source_gate_snapshot_schema_valid
    observed: list[tuple[sqlite3.Connection, object]] = []

    def observe_validation(raw: sqlite3.Connection) -> bool:
        observed.append((raw, raw.row_factory))
        return validator(raw)

    monkeypatch.setattr(gate, "_source_gate_snapshot_schema_valid", observe_validation)
    try:
        assert connection.in_transaction()
        SourceGrantProvisioner(gate)._require_valid_gate_schema(connection)
        assert observed == [(raw_connection, sqlite3.Row)]
        assert raw_connection.row_factory is original_row_factory
        assert connection.in_transaction()
    finally:
        connection.close()


def test_schema_validation_exception_is_redacted_and_closes_restored_transaction(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema exceptions restore the DBAPI factory, roll back, and expose only a fixed error."""
    engine, gate = prepared_gate
    before = _snapshot(engine)
    opened_connections: list[Connection] = []
    raw_connections: list[sqlite3.Connection] = []
    open_connection = gate._open_write_connection

    def capture_connection() -> Connection:
        connection = open_connection()
        opened_connections.append(connection)
        return connection

    def fail_schema_validation(raw: sqlite3.Connection) -> bool:
        raw_connections.append(raw)
        assert raw.row_factory is sqlite3.Row
        raise RuntimeError("synthetic schema validation canary")

    monkeypatch.setattr(gate, "_open_write_connection", capture_connection)
    monkeypatch.setattr(gate, "_source_gate_snapshot_schema_valid", fail_schema_validation)

    with pytest.raises(SourceGrantProvisioningUnavailableError) as error:
        SourceGrantProvisioner(gate).provision(_request())

    assert str(error.value) == "source gate schema is unavailable"
    assert "synthetic schema validation canary" not in str(error.value)
    assert len(opened_connections) == 1
    assert opened_connections[0].closed
    assert len(raw_connections) == 1
    assert raw_connections[0].row_factory is None
    assert _snapshot(engine) == before


def test_nonempty_conditions_and_future_revisions_are_not_silently_accepted() -> None:
    """Provisioning refuses semantics the existing gate cannot authorize."""
    with pytest.raises(SourceAttemptInputError):
        _request(conditions=("redistribution",))
    with pytest.raises(SourceAttemptInputError):
        _request(revision=2)


def test_nonpositive_timezone_aware_interval_is_rejected_before_storage() -> None:
    """The explicit validity interval must have a strict positive duration."""
    point = datetime.now(timezone.utc) + timedelta(days=1)
    with pytest.raises(SourceAttemptInputError):
        _request(not_before=point, expires_at=point)
    with pytest.raises(SourceAttemptInputError):
        _request(not_before=point + timedelta(seconds=1), expires_at=point)


def test_explicit_timezone_aware_interval_has_no_arbitrary_seven_day_cap(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An operator may declare a longer interval while the persisted expiry stays explicit."""
    _engine, gate = prepared_gate
    not_before = datetime.now(timezone.utc) - timedelta(seconds=30)
    expires_at = not_before + timedelta(days=365)
    result = SourceGrantProvisioner(gate).provision(
        _request(not_before=not_before, expires_at=expires_at)
    )
    assert result.expires_at_us - result.not_before_us > 7 * 24 * 60 * 60 * 1_000_000


def test_custom_mutable_timezone_is_snapshotted_once_at_request_construction(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A mutable caller timezone cannot change the grant after request validation."""
    _engine, gate = prepared_gate
    mutable_timezone = _MutableOffsetTzInfo(timedelta(hours=2))
    expected_not_before = datetime.now(timezone.utc) - timedelta(seconds=30)
    expected_expires_at = expected_not_before + timedelta(minutes=2)
    local_not_before = (expected_not_before + timedelta(hours=2)).replace(tzinfo=mutable_timezone)
    local_expires_at = (expected_expires_at + timedelta(hours=2)).replace(tzinfo=mutable_timezone)

    request = _request(not_before=local_not_before, expires_at=local_expires_at)

    assert mutable_timezone.utcoffset_calls == 2
    assert request.not_before == expected_not_before
    assert request.expires_at == expected_expires_at
    assert request.not_before.tzinfo is timezone.utc
    assert request.expires_at.tzinfo is timezone.utc

    mutable_timezone.offset = timedelta(hours=-9)
    result = SourceGrantProvisioner(gate).provision(request)

    assert mutable_timezone.utcoffset_calls == 2
    assert result.not_before_us == _datetime_to_epoch_us(expected_not_before)
    assert result.expires_at_us == _datetime_to_epoch_us(expected_expires_at)


def _datetime_to_epoch_us(value: datetime) -> int:
    """Convert a known UTC synthetic date to integer microseconds without floats."""
    delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


@pytest.mark.parametrize(
    ("field_name", "canary"),
    [
        ("grant_id", "sk-synthetic-canary-1234567890"),
        ("source", "sk-synthetic-canary-1234567890"),
        ("canonical_model", "sk-synthetic-canary-1234567890"),
        ("principal_scope", "sk-synthetic-canary-1234567890"),
        ("attested_by", "sk-synthetic-canary-1234567890"),
        ("operation", "sk-synthetic-canary-1234567890"),
        ("host", "sk-synthetic-canary-1234567890.example"),
        ("rights_evidence_reference", "evidence:sk-synthetic-canary-1234567890"),
        ("operator_attestation_note", "Declaration mentions sk-synthetic-canary-1234567890"),
    ],
    ids=[
        "grant-id",
        "source",
        "canonical-model",
        "principal-scope",
        "attested-by",
        "operation",
        "host",
        "evidence-reference",
        "attestation-note",
    ],
)
def test_credential_shaped_canaries_are_rejected_without_echo(
    field_name: str,
    canary: str,
) -> None:
    """Every caller-controlled string is screened before it can be stored or returned."""
    with pytest.raises(SourceAttemptInputError) as error:
        _request(**{field_name: canary})
    error_text = str(error.value)
    if canary in error_text:
        raise AssertionError("credential-shaped input appeared in an error")


def test_request_and_result_repr_redact_operator_and_scope_strings(
    prepared_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Default object representation never echoes persisted strings."""
    _engine, gate = prepared_gate
    note = "synthetic-private-note-canary"
    request = _request(operator_attestation_note=note)
    if note in repr(request) or EVIDENCE_REFERENCE in repr(request):
        raise AssertionError("request repr exposed operator or evidence text")

    result = SourceGrantProvisioner(gate).provision(request)
    for value in (result.grant_id, result.host, result.attested_by):
        if value in repr(result):
            raise AssertionError("result repr exposed a scope identifier")


def test_precommit_close_failure_reports_cleanup_uncertainty_and_rolls_back(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-commit close has a distinct fixed error and leaves no gate writes."""
    engine, gate = prepared_gate
    before = _snapshot(engine)
    connection = gate._open_write_connection()
    close_connection = connection.close

    def close_then_fail() -> None:
        close_connection()
        raise RuntimeError("synthetic pre-commit close canary")

    proxy = SimpleNamespace(
        connection=connection.connection,
        execute=connection.execute,
        exec_driver_sql=connection.exec_driver_sql,
        commit=connection.commit,
        close=close_then_fail,
    )
    stages: list[str] = []

    def fail_before_commit(
        _connection: object,
        _request: SourceGrantProvisionRequest,
        _effective_now: int,
        _not_before_us: int,
    ) -> None:
        stages.append("precommit_verification_injected")
        raise SourceGrantProvisioningUnavailableError("synthetic verification failure")

    monkeypatch.setattr(gate, "_open_write_connection", lambda: proxy)
    monkeypatch.setattr(
        SourceGrantProvisioner,
        "_verify_inserted_rows",
        staticmethod(fail_before_commit),
    )

    with pytest.raises(SourceGrantProvisioningCleanupError) as error:
        SourceGrantProvisioner(gate).provision(_request())

    assert str(error.value) == (
        "source grant was not committed; transaction cleanup failed; "
        "resolve gate cleanup before retrying"
    )
    assert "synthetic pre-commit close canary" not in str(error.value)
    assert stages == ["precommit_verification_injected"]
    assert connection.closed
    assert _snapshot(engine) == before


def test_acknowledged_commit_close_failure_reports_committed_without_retry(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful commit acknowledgement stays distinct from close cleanup failure."""
    engine, gate = prepared_gate
    request = _request()
    connection = gate._open_write_connection()
    close_connection = connection.close

    def close_then_fail() -> None:
        close_connection()
        raise RuntimeError("synthetic close failure")

    stages: list[str] = []

    def commit_and_acknowledge() -> None:
        connection.commit()
        stages.append("commit_acknowledged")

    proxy = SimpleNamespace(
        connection=connection.connection,
        execute=connection.execute,
        exec_driver_sql=connection.exec_driver_sql,
        commit=commit_and_acknowledge,
        close=close_then_fail,
    )
    monkeypatch.setattr(gate, "_open_write_connection", lambda: proxy)
    with pytest.raises(SourceGrantProvisioningCommittedCleanupError) as error:
        SourceGrantProvisioner(gate).provision(request)
    assert str(error.value) == (
        "source grant commit was acknowledged; connection cleanup failed; do not retry this grant"
    )
    assert "synthetic close failure" not in str(error.value)
    assert stages == ["commit_acknowledged"]
    assert connection.closed

    inspection = gate.inspect_grant(
        SourceGrantInspectionRequest(
            grant_id=request.grant_id,
            revision=request.revision,
            source=request.source,
            canonical_model=request.canonical_model,
            operation=request.operation,
            principal_scope=request.principal_scope,
            host=request.host,
        )
    )
    assert inspection.status is SourceGrantInspectionStatus.MATCHED
    assert inspection.reason is SourceGrantInspectionReason.GRANT_MATCHED
    with engine.connect() as read_connection:
        assert (
            read_connection.exec_driver_sql(
                "SELECT COUNT(*) FROM od_source_grants WHERE grant_id = 'synthetic-grant'"
            ).scalar_one()
            == 1
        )


def test_lost_commit_acknowledgement_requires_grant_id_reconciliation(
    prepared_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit that completed before its acknowledgement was lost is reconciled by scope."""
    engine, gate = prepared_gate
    request = _request()
    commit = gate._commit
    connection = gate._open_write_connection()
    close_connection = connection.close

    def close_then_lose_acknowledgement() -> None:
        close_connection()
        raise RuntimeError("synthetic post-commit close canary")

    stages: list[str] = []
    proxy = SimpleNamespace(
        connection=connection.connection,
        execute=connection.execute,
        exec_driver_sql=connection.exec_driver_sql,
        commit=connection.commit,
        close=close_then_lose_acknowledgement,
    )

    def commit_then_lose_acknowledgement(current_connection: object) -> None:
        commit(current_connection)  # type: ignore[arg-type]
        stages.append("commit_completed")
        stages.append("commit_acknowledgement_lost")
        raise SourceAttemptGateUnavailable("synthetic acknowledgement loss")

    monkeypatch.setattr(gate, "_open_write_connection", lambda: proxy)
    monkeypatch.setattr(gate, "_commit", commit_then_lose_acknowledgement)
    with pytest.raises(SourceGrantProvisioningCommitUncertainError) as error:
        SourceGrantProvisioner(gate).provision(request)
    assert "inspect the exact grant ID" in str(error.value)
    assert "before retrying" in str(error.value)
    assert "synthetic post-commit close canary" not in str(error.value)
    assert stages == ["commit_completed", "commit_acknowledgement_lost"]
    assert connection.closed

    inspection = gate.inspect_grant(
        SourceGrantInspectionRequest(
            grant_id=request.grant_id,
            revision=request.revision,
            source=request.source,
            canonical_model=request.canonical_model,
            operation=request.operation,
            principal_scope=request.principal_scope,
            host=request.host,
        )
    )
    assert inspection.status is SourceGrantInspectionStatus.MATCHED
    with engine.connect() as read_connection:
        assert (
            read_connection.exec_driver_sql(
                "SELECT COUNT(*) FROM od_source_grants WHERE grant_id = 'synthetic-grant'"
            ).scalar_one()
            == 1
        )
