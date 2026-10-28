# Standalone source-attempt gate migration

The source-attempt gate is a local file-backed SQLite store. Its schema has an
independent Alembic chain and version table, `alembic_version_source_gate`.
This chain never runs as part of OpenData startup and does not invoke or advance
`alembic_data`.

## Explicit upgrade

Back up the exact existing gate file using the approved local backup procedure,
then run the command from the OpenData repository root with its absolute path:

```bash
python scripts/migrate_source_gate.py --database-path /absolute/path/to/source-gate.sqlite
```

The file must already exist as a regular, non-symlink file. The command opens it
using SQLite `mode=rw`; a missing path cannot be created by SQLite. There is no
default URL, environment lookup, `.env` loading, scheduler hook, or startup
migration. Offline SQL generation and non-SQLite targets are rejected.
The command captures the target's device/inode and checks it again after SQLite
opens the file, before schema reads, after obtaining `BEGIN IMMEDIATE`, and
before commit. These checks detect ordinary path replacement during the
workflow, but do not pin an OS file handle or eliminate a same-user process's
ability to swap a pathname away and back between checks.

The gate-schema comparison intentionally allows unrelated tables and rows to
coexist with a complete v1 gate, and preserves them. It therefore cannot prove
that the selected SQLite file is a standalone source-gate database or
distinguish a warehouse database that also contains the exact v1 gate schema.
The operator must select and review the intended local file before invoking
this migration; stronger database identity and provenance design remains a
separate follow-up.

## Adoption and v2 scope

`sg0001_gate_v1_adopt` performs a read-only comparison against
the exact v1 schema created by `0015_source_attempt_gate`. To construct that
reference, the migration verifies the checked-in 0015 source digest and runs
that one revision only against a private in-memory SQLite database. It does not
run the warehouse revision chain against the supplied file. Missing, partial,
drifted, or unknown `od_source_*` objects, invalid marker states, non-UTF-8
database encoding, and source-gate foreign-key violations stop the command
before adoption. Unrelated tables and rows are preserved.

`sg0002_source_attempt_scope_v2` creates
`od_source_attempt_scope_v2(attempt_id, scope_version, scope_json,
scope_sha256, recorded_at_us)`. It does not backfill old reservations. New
reservation and sidecar rows are written together by the runtime. The scope
JSON uses canonical JSON encoded as UTF-8, with sorted keys, compact separators,
and `ensure_ascii=False`; the sidecar limits those bytes to 16 KiB and requires
a lowercase 64-character SHA-256. An FK ties each sidecar row to one immutable
reservation, and UPDATE/DELETE triggers preserve the sidecar as append-only
evidence. The runtime may use v2 only when the marker table contains exactly
one `version_num='sg0002_source_attempt_scope_v2'` row and the full schema
checks still pass.

The v2 JSON object is closed to these fields (no credentials or extra keys):
`attempt_id`, `attempt_no`, `budget_binding_sha256`, `budget_policy_revision`,
`budget_policy_sha256`, `budget_quota_key`, `canonical_model`,
`capture_intent_id`, `expected_grant_revision`, `grant_id`, `host`,
`operation`, `principal_scope`, `request_id`, `request_summary_sha256`,
`run_key`, `scope_version`, `source`, `source_policy_id`,
`source_policy_revision`, and `source_policy_sha256`. The canonicalizer sorts
keys before serialization; this list is the closed v2 schema, not a prescribed
input order.

## Transaction and recovery boundary

Before taking a write lock, the environment performs read-only checks. It then
uses one explicit SQLite `BEGIN IMMEDIATE` transaction for the standalone
version table and both revisions, validates the resulting v2 schema, and only
then commits. A migration exception rolls back that transaction, including the
marker and DDL. The integration acceptance must still verify this behavior on a
fresh disposable copy of a real v1 database before this migration is considered
accepted. This procedure does not prove protection from a privileged process
that can directly edit the SQLite file, nor does it verify filesystem backup
integrity or prevent same-user path races.

Downgrading the v1 adoption marker is prohibited. The v2 revision refuses to
discard persisted scope evidence; archive and recovery procedures must be
reviewed before any future schema reversal.
