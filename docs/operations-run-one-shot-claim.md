# Run ledger one-shot claim rollout

## Status and scope

This document defines the deployment and incident-handling requirements for
`claim_policy` on `od_run_ledger`. It is an operations guide, not evidence that
revision `0020_run_ledger_claim_policy`, its tests, or a warehouse migration has
passed. Keep rollout blocked until the exact implementation has completed its
isolated acceptance checks and those results are recorded. A production schema
is accepted only after its own migration and post-migration checks complete.

The supported policies are:

| Policy | Claim behavior |
| --- | --- |
| `recover_expired` | The general queue claim may claim queued work and reclaim a running lease after expiry. This is assigned to pre-existing rows by the migration and to `INSERT` statements that omit the `claim_policy` column. |
| `one_shot` | The general queue claim excludes the row. A worker must request that exact run key, and the row must still be pristine and queued. It is never reclaimed after execution starts. |

The migration adds `claim_policy VARCHAR(16) NOT NULL DEFAULT
'recover_expired'` and an exact CHECK constraint. The default applies to
pre-existing rows and to inserts that omit the column; it does not override an
explicitly supplied policy.

## Deployment order

The order below is required because an old worker does not read `claim_policy`.
It may still see a one-shot row as ordinary queued or expired work and claim it
through the older broad-claim path.

1. **Wait for implementation acceptance.** Review the exact migration and
   runtime code version. Record isolated migration evidence for SQLite and the
   supported MySQL version, including a successful upgrade and the expected
   failure and retry states. A source review or this document does not satisfy
   that gate. Revision 0020 is online-only and supports SQLite and MySQL 8.0.16+
   within MySQL 8; it refuses MariaDB and offline SQL generation.
2. **Inventory every warehouse and process that can touch its ledger.** Include
   workers on every host, schedulers, manual dispatchers, and any process that
   can invoke broad queue claims. Freeze new run submission and dispatch during
   the maintenance window.
3. **Stop and drain every old worker before migrating.** Confirm each old
   process has exited and no hidden replica or scheduled restart can bring it
   back. Keep all workers stopped until every target warehouse has the new
   schema and every worker fleet has a policy-aware build. An old worker left
   running can steal a `one_shot` row.
4. **Back up every target warehouse and rehearse recovery.** Verify that each
   backup is readable and restore it to a disposable, production-like target.
   Run and inspect the migration there first. Record the target database
   identity, backup location, restore result, and operator for each warehouse.
5. **Run one migration runner at a time.** Apply revision 0020 with the
   deployment's reviewed warehouse Alembic configuration to one intended
   warehouse. Do not run concurrent migration processes against the same
   version table. For fleets with separate warehouse databases, finish and
   verify one target before moving to the next.
6. **Verify every warehouse before starting workers.** Confirm its data
   migration marker is exactly `0020_run_ledger_claim_policy`; the ledger has
   the expected `claim_policy` column and default; and that its CHECK set is
   exactly:

   - `ck_od_run_ledger_status`: `status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')`
   - `ck_od_run_ledger_fencing_token`: `fencing_token >= 0`
   - `ck_od_run_ledger_claim_policy`: `claim_policy IN ('recover_expired', 'one_shot')`

   On MySQL, confirm all three checks are enforced. Confirm the only ledger
   index is the non-unique `ix_od_run_ledger_claim`, with exactly the ordered
   key columns `status`, `lease_expires_at_us`, `created_at_us`; it must have
   no SQLite partial predicate or MySQL prefix length. Check that stored
   policies are exactly `recover_expired` or `one_shot`, and that no SQLite
   probe index remains. Record the checks per warehouse; a successful Alembic
   exit alone is insufficient.
7. **Deploy the policy-aware build to the whole fleet.** Restart each worker
   from the reviewed artifact. Verify that every worker capable of claiming
   runs the same policy-aware release and that no old process remains. Keep
   one-shot submission and execution disabled until this fleet check is
   complete.
8. **Enable one-shot use only after the checks above.** Start with an explicitly
   reviewed, bounded use. Verify the stored policy and claim outcome for that
   run before widening use.

If a warehouse is restored from backup or workers are rolled back, repeat the
schema and fleet checks before enabling one-shot runs. Do not restore an older
ledger snapshot over one-shot work whose external outcome is not reconciled:
that can erase the evidence that prevents a repeat.

## MySQL interruption and retry

MySQL DDL implicitly commits; an Alembic transaction cannot roll back a DDL
statement that the server has committed. Revision 0020 adds the column and its
enforced CHECK in a single `ALTER TABLE` statement. If that statement completed
but the process stopped before Alembic advanced its marker, the database may
have the complete 0020 schema while the version table still says 0019. The
migration permits a retry only when it finds that exact complete schema and
the exact old marker; the retry can then allow Alembic to advance the marker.

Any other partial or unexpected state is rejected. If the runner fails or the
schema and marker disagree:

1. Stop all application, worker, scheduler, and migration processes for that
   warehouse.
2. Preserve the migration logs and take or verify a fresh database backup
   before any repair.
3. Have the database operator inspect the version table, `SHOW CREATE TABLE
   od_run_ledger`, relevant `information_schema` column and CHECK definitions,
   and the ledger's stored policy values. Compare them with the exact revision
   and its acceptance evidence.
4. Retry the same reviewed migration only when the operator has confirmed the
   complete DDL and exact 0019 marker described above. For every other state,
   stop and prepare a reviewed repair or restore plan. Do not guess at DDL,
   edit the Alembic marker to force progress, or repeatedly rerun the migration.

## SQLite collation probe

Revision 0020 temporarily creates the partial index
`ix_od_run_ledger_claim_policy_probe_0020` to inspect the effective collation
of the ledger enum columns, then removes it. If the process is forcibly stopped
after index creation and before cleanup, the next attempt sees the named probe
and fails closed. Do not start another migration while that probe remains.

First stop all application, worker, scheduler, and migration processes using
the database. Take and verify a database backup. Inspect the exact entry in
`sqlite_master` and its key parts from `pragma_index_xinfo`, confirming the
database file, table, index name, columns, partial predicate, collation, and
sort order. Preserve the inspection output with the migration logs. **Never
delete an index solely because its name resembles the probe.** If the index
cannot be proven to be the abandoned 0020 probe on the intended database, stop
and escalate for a reviewed repair. After any authorized cleanup, verify the
probe is absent and compare the ledger schema and Alembic marker to the backup
and expected pre-migration state before retrying.

## One-shot execution and uncertain outcomes

There is currently no HTTP endpoint, CLI command, or configuration switch for
one-shot submission or execution. The capability is exposed only through the
internal explicit `RunService.submit_one_shot` / `RunService.run_one_shot` and
`PersistentRunWorker.submit_one_shot` / `PersistentRunWorker.run_one_shot` methods. The
execution method requires the exact run key and never falls back to the broad
queue claim. This document does not describe a public operator interface.

A one-shot claim requires the exact run key and an untouched queued row. Its
first claim records the worker, lease, start and heartbeat, and increments the
fencing token. The general queue claim excludes `one_shot` rows. A running
one-shot lease is never reclaimed.

The first execution attempt can outlive its lease. If the lease expires before
the fenced publication or failure transition, that invocation may return with
the ledger row still `running` and no publication receipt. If the row remains
`running`, a later `run_one_shot` call using the same key detects its expired
lease and raises `RunExecutionUncertainError`; it does not reclaim or execute
the row again. The outcome is unknown because the upstream may already have
processed the request.
Do not automatically retry it, reset it to `queued`, or create a replacement
run with the same intended effect. Reconcile the exact run key against domain
evidence such as provider request records, response captures, source-attempt
records, and publication records. A domain owner must decide how to record a
resolved outcome; the generic ledger cannot infer whether the upstream
completed the request.

Fencing tokens prevent stale workers from committing protected warehouse
transitions after losing a lease. They do not control an upstream provider and
do not provide upstream exactly-once delivery. A provider may have processed a
request even if the worker lost the response or crashed before recording its
result. Transport retries, provider idempotency keys, and reconciliation must
be assessed separately for each operation.

## Downgrade and rollback

Revision 0020 intentionally refuses downgrade. Dropping `claim_policy` erases
the distinction that prevents a one-shot run from being claimed again. Do not
use `alembic downgrade` to roll back this revision. Rollback planning must keep
the column and policy-aware claim behavior in place, or keep workers stopped
until a reviewed forward repair restores both. An older application build that
does not understand the policy is not a safe rollback target while the ledger
contains one-shot rows.
