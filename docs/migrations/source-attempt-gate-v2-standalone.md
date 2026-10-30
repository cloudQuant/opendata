# Standalone source-attempt gate migration

The source-attempt gate is a local file-backed SQLite store. Its schema has an
independent Alembic chain (`sg0000` through `sg0004`) and version table,
`alembic_version_source_gate`. This chain never runs as part of OpenData startup
and does not invoke or advance `alembic_data` or create its
`alembic_version_data` marker.

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
ability to swap a pathname away and back between checks. SQLite's write lock
coordinates SQLite writers on the opened database; it is not a security boundary
against a process running as the same user that can change the path or edit the
file outside this migration. The operator must keep the target path and its
parent directory under trusted control for the duration of the command.

For legacy adoption, the gate-schema comparison intentionally allows unrelated
tables and rows to coexist with a complete v1 gate, and preserves them. It
therefore cannot prove that the selected SQLite file is a standalone
source-gate database or distinguish a warehouse database that also contains the
exact v1 gate schema. The operator must select and review the intended local
file before invoking this migration; stronger database identity and provenance
design remains a separate follow-up. Explicit empty-file initialization below
has a stricter rule and rejects any pre-existing user schema object.

### Explicit initialization of an empty SQLite file

The default command still rejects an empty file. To initialize one, first
prepare an existing regular, non-symlink UTF-8 SQLite file with no user tables,
indexes, views, or triggers, then opt in explicitly:

```bash
python scripts/migrate_source_gate.py \
  --database-path /absolute/path/to/empty-source-gate.sqlite \
  --initialize-empty
```

The option does not create the file. The migration verifies the pinned v1
reference, installs that schema, then continues the independent source-gate
chain to `sg0004_source_one_shot_v4`. The only Alembic bookkeeping table
created is `alembic_version_source_gate`; it never fabricates or advances a
data-chain head in `alembic_version_data` and does not seed grants, policies,
bindings, quota, attempts, or scope records. The required source-gate clock
starts at `(clock_id=1, highwater_us=0)`.

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

Before taking a write lock, the environment performs read-only checks. After
`BEGIN IMMEDIATE`, it rechecks the target device/inode and starting schema, then
uses that one SQLite write transaction for the source-gate version table, all
revisions needed to reach the requested target, and final schema validation.
For empty initialization this includes the v1 DDL and source-gate marker; a
programmatic upgrade stopped at `sg0000_gate_v1_initialize` can be resumed later
from that exact V0 marker. The standalone CLI requests `head`, currently
`sg0004_source_one_shot_v4`. A migration exception rolls back the transaction,
including the marker and DDL. Acceptance should verify this behavior on fresh
disposable copies of both an empty file and an exact legacy v1 database. These
checks do not pin an OS file handle, prevent a same-user path race or direct file
edit, or verify filesystem backup integrity.

Downgrading the v1 adoption marker is prohibited. The v2 revision refuses to
discard persisted scope evidence; archive and recovery procedures must be
reviewed before any future schema reversal.
