"""Create the offline SQLite source attempt reservation gate.

Revision ID: 0015_source_attempt_gate
Revises: 0014_sina_fzb_slot_reconciliation
Create Date: 2026-10-27

This revision creates the source-attempt gate only on SQLite. On supported
MySQL 8.0.16+ warehouses it is an explicit no-op: the source gate runtime is
SQLite-only and this revision must not create its tables in the warehouse.
MySQL versions outside that range, MariaDB, and other dialects are rejected.
The grant evidence fields store operator declarations and references; they do
not determine legal rights. Reservation rows are permanent idempotency
tombstones and must not be deleted casually.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    column,
    select,
    table,
)

from alembic import op

revision = "0015_source_attempt_gate"
down_revision = "0014_sina_fzb_slot_reconciliation"
branch_labels = None
depends_on = None


_CLOCK = "od_source_gate_clock"
_POLICIES = "od_source_gate_policies"
_BINDINGS = "od_source_gate_source_bindings"
_BUCKETS = "od_source_gate_state"
_GRANTS = "od_source_grants"
_GRANT_HOSTS = "od_source_grant_hosts"
_GRANT_HOST_SETS = "od_source_grant_host_sets"
_GRANT_EVENTS = "od_source_grant_events"
_ATTEMPTS = "od_source_attempt_reservations"


def _target_mode() -> str:
    """Return the supported migration mode or reject an unverified target."""
    dialect = op.get_bind().dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mysql" and not getattr(dialect, "is_mariadb", False):
        version = dialect.server_version_info
        if (
            not isinstance(version, tuple)
            or len(version) < 3
            or any(type(part) is not int for part in version[:3])
            or version[0] != 8
            or version[:3] < (8, 0, 16)
        ):
            raise RuntimeError("source attempt gate revision 0015 supports MySQL 8.0.16+ only")
        return "mysql-noop"
    raise RuntimeError("source attempt gate revision 0015 supports SQLite and MySQL 8.0.16+ only")


def upgrade() -> None:
    """Create SQLite gate tables; explicitly leave supported MySQL unchanged."""
    if _target_mode() == "mysql-noop":
        return
    op.create_table(
        _CLOCK,
        Column("clock_id", BigInteger(), primary_key=True, nullable=False),
        Column("highwater_us", BigInteger(), nullable=False),
        CheckConstraint("clock_id = 1", name="ck_od_src_gate_clock_singleton"),
        CheckConstraint("highwater_us >= 0", name="ck_od_src_gate_clock_nonnegative"),
    )
    op.execute("INSERT INTO od_source_gate_clock (clock_id, highwater_us) VALUES (1, 0)")

    op.create_table(
        _POLICIES,
        Column("quota_key", String(128), primary_key=True, nullable=False),
        Column("policy_revision", BigInteger(), nullable=False),
        Column("rate_microtokens_per_second", BigInteger(), nullable=False),
        Column("capacity_microtokens", BigInteger(), nullable=False),
        Column("policy_reference", String(512), nullable=False),
        Column("policy_sha256", String(64), nullable=False),
        Column("configured_by", String(128), nullable=False),
        Column("configured_at_us", BigInteger(), nullable=False),
        CheckConstraint(
            "length(trim(quota_key)) BETWEEN 1 AND 128",
            name="ck_od_src_gate_policy_key",
        ),
        CheckConstraint(
            "policy_revision BETWEEN 1 AND 2147483647",
            name="ck_od_src_gate_policy_revision",
        ),
        CheckConstraint(
            "rate_microtokens_per_second BETWEEN 1 AND 1000000000000",
            name="ck_od_src_gate_policy_rate",
        ),
        CheckConstraint(
            "capacity_microtokens BETWEEN 1000000 AND 1000000000000",
            name="ck_od_src_gate_policy_capacity",
        ),
        CheckConstraint(
            "length(trim(policy_reference)) BETWEEN 1 AND 512",
            name="ck_od_src_gate_policy_reference",
        ),
        CheckConstraint(
            "length(policy_sha256) = 64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_od_src_gate_policy_sha256",
        ),
        CheckConstraint(
            "length(trim(configured_by)) BETWEEN 1 AND 128",
            name="ck_od_src_gate_policy_configured_by",
        ),
        CheckConstraint("configured_at_us >= 0", name="ck_od_src_gate_policy_configured_at"),
    )
    op.create_table(
        _BINDINGS,
        Column("source", String(128), primary_key=True, nullable=False),
        Column(
            "quota_key",
            String(128),
            ForeignKey(f"{_POLICIES}.quota_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("binding_reference", String(512), nullable=False),
        Column("binding_sha256", String(64), nullable=False),
        Column("configured_by", String(128), nullable=False),
        Column("configured_at_us", BigInteger(), nullable=False),
        CheckConstraint(
            "length(trim(source)) BETWEEN 1 AND 128", name="ck_od_src_gate_binding_source"
        ),
        CheckConstraint(
            "length(trim(quota_key)) BETWEEN 1 AND 128", name="ck_od_src_gate_binding_key"
        ),
        CheckConstraint(
            "length(trim(binding_reference)) BETWEEN 1 AND 512",
            name="ck_od_src_gate_binding_reference",
        ),
        CheckConstraint(
            "length(binding_sha256) = 64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_od_src_gate_binding_sha256",
        ),
        CheckConstraint(
            "length(trim(configured_by)) BETWEEN 1 AND 128",
            name="ck_od_src_gate_binding_configured_by",
        ),
        CheckConstraint("configured_at_us >= 0", name="ck_od_src_gate_binding_configured_at"),
    )
    op.create_table(
        _BUCKETS,
        Column(
            "quota_key",
            String(128),
            ForeignKey(f"{_POLICIES}.quota_key", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        Column("tokens_micro", BigInteger(), nullable=False),
        Column("remainder", BigInteger(), nullable=False),
        Column("last_refill_us", BigInteger(), nullable=False),
        Column("policy_revision", BigInteger(), nullable=False),
        CheckConstraint("tokens_micro >= 0", name="ck_od_src_gate_state_tokens"),
        CheckConstraint("remainder BETWEEN 0 AND 999999", name="ck_od_src_gate_state_remainder"),
        CheckConstraint("last_refill_us >= 0", name="ck_od_src_gate_state_refill_at"),
        CheckConstraint(
            "policy_revision BETWEEN 1 AND 2147483647",
            name="ck_od_src_gate_state_revision",
        ),
    )
    op.create_table(
        _GRANTS,
        Column("grant_id", String(128), primary_key=True, nullable=False),
        Column("source", String(128), nullable=False),
        Column("canonical_model", String(128), nullable=False),
        Column("operation", String(16), nullable=False),
        Column("principal_scope", String(128), nullable=False),
        Column("decision", String(8), nullable=False, server_default="UNKNOWN"),
        Column("revision", BigInteger(), nullable=False),
        Column("not_before_us", BigInteger(), nullable=False),
        Column("expires_at_us", BigInteger(), nullable=False),
        Column("conditions_json", Text(), nullable=False, server_default="[]"),
        Column("rights_evidence_reference", String(512), nullable=True),
        Column("rights_evidence_sha256", String(64), nullable=True),
        Column("operator_attestation_note", String(1024), nullable=True),
        Column("attested_by", String(128), nullable=True),
        Column("attested_at_us", BigInteger(), nullable=True),
        Column("revoked_at_us", BigInteger(), nullable=True),
        Column("revocation_actor", String(128), nullable=True),
        Column("revocation_reason_code", String(64), nullable=True),
        Column("created_at_us", BigInteger(), nullable=False),
        Column("updated_at_us", BigInteger(), nullable=False),
        CheckConstraint("length(trim(grant_id)) BETWEEN 1 AND 128", name="ck_od_src_grant_id"),
        CheckConstraint("length(trim(source)) BETWEEN 1 AND 128", name="ck_od_src_grant_source"),
        CheckConstraint(
            "length(trim(canonical_model)) BETWEEN 1 AND 128",
            name="ck_od_src_grant_model",
        ),
        CheckConstraint(
            "operation IN ('query', 'store', 'export', 'subscribe')",
            name="ck_od_src_grant_operation",
        ),
        CheckConstraint(
            "length(trim(principal_scope)) BETWEEN 1 AND 128",
            name="ck_od_src_grant_principal",
        ),
        CheckConstraint(
            "decision IN ('UNKNOWN', 'DENIED', 'ALLOWED')", name="ck_od_src_grant_decision"
        ),
        CheckConstraint("revision BETWEEN 1 AND 2147483647", name="ck_od_src_grant_revision"),
        CheckConstraint(
            "not_before_us >= 0 AND expires_at_us > not_before_us",
            name="ck_od_src_grant_validity",
        ),
        CheckConstraint("length(conditions_json) <= 16384", name="ck_od_src_grant_conditions_size"),
        CheckConstraint(
            "decision != 'ALLOWED' OR ("
            "rights_evidence_reference IS NOT NULL AND rights_evidence_sha256 IS NOT NULL AND "
            "operator_attestation_note IS NOT NULL AND attested_by IS NOT NULL AND "
            "attested_at_us IS NOT NULL AND "
            "length(trim(rights_evidence_reference)) BETWEEN 1 AND 512 AND "
            "length(rights_evidence_sha256) = 64 AND "
            "rights_evidence_sha256 NOT GLOB '*[^0-9a-f]*' AND "
            "length(trim(operator_attestation_note)) BETWEEN 1 AND 1024 AND "
            "length(trim(attested_by)) BETWEEN 1 AND 128)",
            name="ck_od_src_grant_allowed_has_declaration",
        ),
        CheckConstraint(
            "(revoked_at_us IS NULL AND revocation_actor IS NULL "
            "AND revocation_reason_code IS NULL) OR "
            "(decision = 'DENIED' AND revoked_at_us IS NOT NULL AND "
            "revocation_actor IS NOT NULL AND revocation_reason_code IS NOT NULL AND "
            "revoked_at_us BETWEEN 0 AND 253402300799999999 AND "
            "length(trim(revocation_actor)) BETWEEN 1 AND 128 AND "
            "length(trim(revocation_reason_code)) BETWEEN 1 AND 64)",
            name="ck_od_src_grant_revocation_shape",
        ),
        CheckConstraint(
            "created_at_us BETWEEN 0 AND 253402300799999999 AND "
            "updated_at_us BETWEEN created_at_us AND 253402300799999999",
            name="ck_od_src_grant_times",
        ),
    )
    op.create_table(
        _GRANT_HOSTS,
        Column(
            "grant_id",
            String(128),
            ForeignKey(f"{_GRANTS}.grant_id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        Column("host", String(253), primary_key=True, nullable=False),
        CheckConstraint("length(trim(host)) BETWEEN 1 AND 253", name="ck_od_src_grant_host"),
    )
    op.create_table(
        _GRANT_HOST_SETS,
        Column(
            "grant_id",
            String(128),
            ForeignKey(f"{_GRANTS}.grant_id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        Column("revision", BigInteger(), nullable=False),
        Column("host_count", Integer(), nullable=False),
        Column("sealed_at_us", BigInteger(), nullable=False),
        Column("sealed_by", String(128), nullable=False),
        Column("reason_code", String(64), nullable=False),
        CheckConstraint("revision BETWEEN 1 AND 2147483647", name="ck_od_src_host_set_revision"),
        CheckConstraint("host_count BETWEEN 1 AND 10000", name="ck_od_src_host_set_count"),
        CheckConstraint(
            "sealed_at_us BETWEEN 0 AND 253402300799999999",
            name="ck_od_src_host_set_time",
        ),
        CheckConstraint(
            "length(trim(sealed_by)) BETWEEN 1 AND 128", name="ck_od_src_host_set_actor"
        ),
        CheckConstraint(
            "length(trim(reason_code)) BETWEEN 1 AND 64", name="ck_od_src_host_set_reason"
        ),
    )
    op.create_table(
        _GRANT_EVENTS,
        Column("event_id", Integer(), primary_key=True, autoincrement=True, nullable=False),
        Column(
            "grant_id",
            String(128),
            ForeignKey(f"{_GRANTS}.grant_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("event_type", String(16), nullable=False),
        Column("old_revision", BigInteger(), nullable=False),
        Column("new_revision", BigInteger(), nullable=False),
        Column("actor", String(128), nullable=False),
        Column("reason_code", String(64), nullable=False),
        Column("occurred_at_us", BigInteger(), nullable=False),
        UniqueConstraint(
            "grant_id", "event_type", "new_revision", name="uq_od_source_grant_event_revision"
        ),
        CheckConstraint(
            "event_type IN ('REVOKED', 'HOSTS_SEALED')", name="ck_od_src_grant_event_type"
        ),
        CheckConstraint(
            "(event_type = 'REVOKED' AND new_revision = old_revision + 1) OR "
            "(event_type = 'HOSTS_SEALED' AND new_revision = old_revision)",
            name="ck_od_src_grant_event_revision",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 128", name="ck_od_src_grant_event_actor"
        ),
        CheckConstraint(
            "length(trim(reason_code)) BETWEEN 1 AND 64", name="ck_od_src_grant_event_reason"
        ),
        CheckConstraint(
            "occurred_at_us BETWEEN 0 AND 253402300799999999",
            name="ck_od_src_grant_event_time",
        ),
    )
    op.create_table(
        _ATTEMPTS,
        Column("attempt_id", String(128), primary_key=True, nullable=False),
        Column("request_id", String(128), nullable=False),
        Column("attempt_no", BigInteger(), nullable=False),
        Column("grant_id", String(128), nullable=False),
        Column("expected_revision", BigInteger(), nullable=False),
        Column("source", String(128), nullable=False),
        Column("canonical_model", String(128), nullable=False),
        Column("operation", String(16), nullable=False),
        Column("principal_scope", String(128), nullable=False),
        Column("host", String(253), nullable=False),
        Column("run_key", String(128), nullable=True),
        Column("outcome", String(8), nullable=False),
        Column("denial_code", String(64), nullable=True),
        Column("retry_after_us", BigInteger(), nullable=True),
        Column("decided_at_us", BigInteger(), nullable=False),
        Column("reserved_at_us", BigInteger(), nullable=True),
        Column(
            "quota_key",
            String(128),
            ForeignKey(f"{_POLICIES}.quota_key", ondelete="RESTRICT"),
            nullable=True,
        ),
        Column("policy_revision", BigInteger(), nullable=True),
        Column("rate_microtokens_per_second", BigInteger(), nullable=True),
        Column("capacity_microtokens", BigInteger(), nullable=True),
        CheckConstraint("length(trim(attempt_id)) BETWEEN 1 AND 128", name="ck_od_src_attempt_id"),
        CheckConstraint(
            "length(trim(request_id)) BETWEEN 1 AND 128", name="ck_od_src_attempt_request"
        ),
        CheckConstraint("attempt_no BETWEEN 1 AND 1000000", name="ck_od_src_attempt_no"),
        CheckConstraint("length(trim(grant_id)) BETWEEN 1 AND 128", name="ck_od_src_attempt_grant"),
        CheckConstraint(
            "expected_revision BETWEEN 1 AND 2147483647", name="ck_od_src_attempt_revision"
        ),
        CheckConstraint("length(trim(source)) BETWEEN 1 AND 128", name="ck_od_src_attempt_source"),
        CheckConstraint(
            "length(trim(canonical_model)) BETWEEN 1 AND 128",
            name="ck_od_src_attempt_model",
        ),
        CheckConstraint(
            "operation IN ('query', 'store', 'export', 'subscribe')",
            name="ck_od_src_attempt_operation",
        ),
        CheckConstraint(
            "length(trim(principal_scope)) BETWEEN 1 AND 128",
            name="ck_od_src_attempt_principal",
        ),
        CheckConstraint("length(trim(host)) BETWEEN 1 AND 253", name="ck_od_src_attempt_host"),
        CheckConstraint(
            "run_key IS NULL OR length(trim(run_key)) BETWEEN 1 AND 128",
            name="ck_od_src_attempt_run_key",
        ),
        CheckConstraint(
            "(outcome = 'RESERVED' AND denial_code IS NULL AND reserved_at_us IS NOT NULL) OR "
            "(outcome = 'DENIED' AND denial_code IS NOT NULL "
            "AND length(trim(denial_code)) BETWEEN 1 AND 64 "
            "AND reserved_at_us IS NULL)",
            name="ck_od_src_attempt_outcome_shape",
        ),
        CheckConstraint(
            "retry_after_us IS NULL OR retry_after_us BETWEEN 1 AND 1000000000000000000",
            name="ck_od_src_attempt_retry_after",
        ),
        CheckConstraint("decided_at_us >= 0", name="ck_od_src_attempt_decided_at"),
        CheckConstraint(
            "reserved_at_us IS NULL OR reserved_at_us = decided_at_us",
            name="ck_od_src_attempt_reserved_at",
        ),
        CheckConstraint(
            "(quota_key IS NULL AND policy_revision IS NULL "
            "AND rate_microtokens_per_second IS NULL AND capacity_microtokens IS NULL) OR "
            "(quota_key IS NOT NULL AND policy_revision IS NOT NULL "
            "AND rate_microtokens_per_second IS NOT NULL AND capacity_microtokens IS NOT NULL AND "
            "length(trim(quota_key)) BETWEEN 1 AND 128 AND "
            "policy_revision BETWEEN 1 AND 2147483647 AND "
            "rate_microtokens_per_second BETWEEN 1 AND 1000000000000 AND "
            "capacity_microtokens BETWEEN 1000000 AND 1000000000000)",
            name="ck_od_src_attempt_policy_snapshot",
        ),
        UniqueConstraint("request_id", "attempt_no", name="uq_od_src_attempt_request_no"),
    )

    op.execute(
        f"""CREATE TRIGGER trg_od_source_attempt_immutable_update
        BEFORE UPDATE ON {_ATTEMPTS}
        BEGIN SELECT RAISE(ABORT, 'source attempt tombstones are immutable'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_attempt_immutable_delete
        BEFORE DELETE ON {_ATTEMPTS}
        BEGIN SELECT RAISE(ABORT, 'source attempt tombstones are permanent'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_revoke_only
        BEFORE UPDATE ON {_GRANTS}
        WHEN NOT (
            OLD.decision = 'ALLOWED' AND NEW.decision = 'DENIED'
            AND NEW.revision = OLD.revision + 1
            AND NEW.revoked_at_us IS NOT NULL
            AND NEW.grant_id = OLD.grant_id
            AND NEW.source = OLD.source
            AND NEW.canonical_model = OLD.canonical_model
            AND NEW.operation = OLD.operation
            AND NEW.principal_scope = OLD.principal_scope
            AND NEW.not_before_us = OLD.not_before_us
            AND NEW.expires_at_us = OLD.expires_at_us
            AND NEW.conditions_json = OLD.conditions_json
            AND NEW.rights_evidence_reference IS OLD.rights_evidence_reference
            AND NEW.rights_evidence_sha256 IS OLD.rights_evidence_sha256
            AND NEW.operator_attestation_note IS OLD.operator_attestation_note
            AND NEW.attested_by IS OLD.attested_by
            AND NEW.attested_at_us IS OLD.attested_at_us
            AND OLD.revoked_at_us IS NULL
            AND OLD.revocation_actor IS NULL
            AND OLD.revocation_reason_code IS NULL
            AND NEW.revocation_actor IS NOT NULL
            AND length(trim(NEW.revocation_actor)) BETWEEN 1 AND 128
            AND NEW.revocation_reason_code IS NOT NULL
            AND length(trim(NEW.revocation_reason_code)) BETWEEN 1 AND 64
            AND NEW.created_at_us = OLD.created_at_us
            AND NEW.revoked_at_us >= OLD.updated_at_us
            AND NEW.updated_at_us = NEW.revoked_at_us
        )
        BEGIN SELECT RAISE(ABORT, 'source grant updates may only revoke the current grant'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_od_source_grant_revoke_event
        AFTER UPDATE ON od_source_grants
        WHEN OLD.decision = 'ALLOWED' AND NEW.decision = 'DENIED'
        BEGIN
            INSERT INTO od_source_grant_events
            (grant_id, event_type, old_revision, new_revision, actor, reason_code, occurred_at_us)
            VALUES
            (NEW.grant_id, 'REVOKED', OLD.revision, NEW.revision,
             NEW.revocation_actor, NEW.revocation_reason_code, NEW.revoked_at_us);
        END"""
    )
    op.execute(
        """CREATE TRIGGER trg_od_source_grant_host_insert_before_seal
        BEFORE INSERT ON od_source_grant_hosts
        WHEN EXISTS (
            SELECT 1 FROM od_source_grant_host_sets WHERE grant_id = NEW.grant_id
        ) OR NOT EXISTS (
            SELECT 1 FROM od_source_grants
            WHERE grant_id = NEW.grant_id AND decision = 'ALLOWED'
        )
        BEGIN SELECT RAISE(ABORT, 'grant hosts may only be initialized before sealing'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_od_source_grant_host_set_validate
        BEFORE INSERT ON od_source_grant_host_sets
        WHEN NOT EXISTS (
            SELECT 1 FROM od_source_grants
            WHERE grant_id = NEW.grant_id AND decision = 'ALLOWED'
              AND revision = NEW.revision AND updated_at_us <= NEW.sealed_at_us
        ) OR NEW.host_count != (
            SELECT COUNT(*) FROM od_source_grant_hosts WHERE grant_id = NEW.grant_id
        )
        BEGIN SELECT RAISE(ABORT, 'grant host set does not match the current grant'); END"""
    )
    op.execute(
        """CREATE TRIGGER trg_od_source_grant_host_set_event
        AFTER INSERT ON od_source_grant_host_sets
        BEGIN
            INSERT INTO od_source_grant_events
            (grant_id, event_type, old_revision, new_revision, actor, reason_code, occurred_at_us)
            VALUES
            (NEW.grant_id, 'HOSTS_SEALED', NEW.revision, NEW.revision,
             NEW.sealed_by, NEW.reason_code, NEW.sealed_at_us);
        END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_host_set_immutable_update
        BEFORE UPDATE ON {_GRANT_HOST_SETS}
        BEGIN SELECT RAISE(ABORT, 'grant host seals are immutable'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_host_set_immutable_delete
        BEFORE DELETE ON {_GRANT_HOST_SETS}
        BEGIN SELECT RAISE(ABORT, 'grant host seals are permanent'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_immutable_delete
        BEFORE DELETE ON {_GRANTS}
        BEGIN SELECT RAISE(ABORT, 'source grants are permanent audit records'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_host_immutable_update
        BEFORE UPDATE ON {_GRANT_HOSTS}
        BEGIN SELECT RAISE(ABORT, 'source grant hosts are immutable'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_host_immutable_delete
        BEFORE DELETE ON {_GRANT_HOSTS}
        BEGIN SELECT RAISE(ABORT, 'source grant hosts are permanent'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_event_immutable_update
        BEFORE UPDATE ON {_GRANT_EVENTS}
        BEGIN SELECT RAISE(ABORT, 'source grant events are immutable'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_grant_event_immutable_delete
        BEFORE DELETE ON {_GRANT_EVENTS}
        BEGIN SELECT RAISE(ABORT, 'source grant events are permanent'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_gate_policy_revision
        BEFORE UPDATE ON {_POLICIES}
        WHEN NEW.policy_revision != OLD.policy_revision + 1
        BEGIN SELECT RAISE(ABORT, 'source gate policy updates require the next revision'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_gate_policy_immutable_delete
        BEFORE DELETE ON {_POLICIES}
        BEGIN SELECT RAISE(ABORT, 'source gate policies are permanent audit records'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_gate_binding_immutable_update
        BEFORE UPDATE ON {_BINDINGS}
        BEGIN SELECT RAISE(ABORT, 'source quota bindings are immutable'); END"""
    )
    op.execute(
        f"""CREATE TRIGGER trg_od_source_gate_binding_immutable_delete
        BEFORE DELETE ON {_BINDINGS}
        BEGIN SELECT RAISE(ABORT, 'source quota bindings are permanent audit records'); END"""
    )


def _has_rows(table_name: str) -> bool:
    """Check whether authorization or attempt evidence exists before downgrade."""
    rows = table(table_name, column("row_marker"))
    return op.get_bind().execute(select(1).select_from(rows).limit(1)).first() is not None


def downgrade() -> None:
    """Refuse to discard attempt, grant, policy, or bucket evidence."""
    if _target_mode() == "mysql-noop":
        return
    protected = (
        _ATTEMPTS,
        _GRANT_EVENTS,
        _GRANT_HOST_SETS,
        _GRANT_HOSTS,
        _GRANTS,
        _BUCKETS,
        _BINDINGS,
        _POLICIES,
    )
    if any(_has_rows(table_name) for table_name in protected):
        raise RuntimeError("source gate evidence must be archived before downgrade")
    for trigger in (
        "trg_od_source_gate_binding_immutable_delete",
        "trg_od_source_gate_binding_immutable_update",
        "trg_od_source_gate_policy_immutable_delete",
        "trg_od_source_gate_policy_revision",
        "trg_od_source_grant_event_immutable_delete",
        "trg_od_source_grant_event_immutable_update",
        "trg_od_source_grant_host_immutable_delete",
        "trg_od_source_grant_host_immutable_update",
        "trg_od_source_grant_immutable_delete",
        "trg_od_source_grant_revoke_event",
        "trg_od_source_grant_revoke_only",
        "trg_od_source_grant_host_set_immutable_delete",
        "trg_od_source_grant_host_set_immutable_update",
        "trg_od_source_grant_host_set_event",
        "trg_od_source_grant_host_set_validate",
        "trg_od_source_grant_host_insert_before_seal",
        "trg_od_source_attempt_immutable_delete",
        "trg_od_source_attempt_immutable_update",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_table(_ATTEMPTS)
    op.drop_table(_GRANT_EVENTS)
    op.drop_table(_GRANT_HOST_SETS)
    op.drop_table(_GRANT_HOSTS)
    op.drop_table(_GRANTS)
    op.drop_table(_BUCKETS)
    op.drop_table(_BINDINGS)
    op.drop_table(_POLICIES)
    op.drop_table(_CLOCK)


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
