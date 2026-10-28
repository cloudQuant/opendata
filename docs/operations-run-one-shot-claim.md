# Run ledger one-shot claim rollout

## Status and scope

This document defines deployment and incident-handling requirements for
`claim_policy` on `od_run_ledger` and the reviewed one-shot CLI/runtime path.
The frozen v11 candidate passed 532 isolated tests. Its statement-plus-branch
coverage was 92.36% for `opendata/reviewed_cli.py` (544/589, zero excluded
lines), 94.44% for `opendata/pipeline/runtime_roles.py` (102/108, zero
excluded), and 94.95% for `opendata/pipeline/reviewed_runtime.py` (301/317,
zero excluded). Its evidence is under
`/tmp/od204-one-shot-cli-runtime-v11-20261029-ce74/evidence`; the snapshot is
based on commit `ce74c25399ead699f4af8fcf63dc1cdf098c9ddc`. The subsequent v12
snapshot, on the same base and with the same test selection, also passed 532
isolated tests with the same coverage results. Its evidence is under
`/tmp/od204-one-shot-cli-runtime-v12-20261029-ce74/evidence`. In v11,
`reviewed_runtime.py` had SHA-256
`1f475d52681acb25811472f2295f6736f6a480c6ef3514236867af1d3fd077c2`; v12 has
SHA-256 `3a43c27b5055da83377cbf994015fee2a9797fe382a993b3f6a5d6caf9b84bd4`.
The v12 runtime change adds `one_shot_submitter` to `_require_role`'s type
annotation; the current file matches that accepted v12 snapshot. These test
results accept the frozen candidates only within their isolated test scope.
They do not prove that a candidate is deployed, that a production warehouse
has been migrated, or that a production data-source request is authorized. A
production schema is accepted only after its own migration and post-migration
checks complete.

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

1. **Review the accepted candidate and production migration separately.** The
   isolated v11 and v12 evidence covers the frozen candidates described above,
   not a production rollout. Review the exact migration and runtime artifact
   for the target environment, then record migration evidence for each intended
   warehouse, including a successful upgrade and the expected failure and
   retry states. Revision 0020 is online-only and supports SQLite and MySQL
   8.0.16+ within MySQL 8; it refuses MariaDB and offline SQL generation.
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

The frozen v11 candidate adds the JSON operations `one_shot_submit` and
`one_shot_run` to the strict offline `opendata.reviewed_cli` protocol. These are
injected Python interfaces for a reviewed composition root, not an enabled
production command, HTTP endpoint, environment switch, or Fuyao integration.
The standalone `main()` always calls the CLI with `application=None` and has no
environment or import-string factory. With arguments exactly in the
`--request PATH` form, it exits 78 with `reviewed_application_required` before
reading the JSON; missing or malformed CLI arguments exit 2 before that check.
Until a separately reviewed composition root is implemented, do not present
`python -m opendata.reviewed_cli` as an operator interface.

The CLI accepts one version-1 JSON request from stdin or a regular file passed
as `--request PATH` (`--request -` reads stdin). Its one-shot request shapes
are:

```json
{"schema_version":1,"operation":"one_shot_submit","job_id":"manual","version":"v1","payload":{"idempotency_key":"once-01"},"dry_run":true}
{"schema_version":1,"operation":"one_shot_run","run_key":"<64 lowercase hex>","dry_run":true}
```

The schema rejects extra fields, malformed run keys, and named execution
selectors such as `command`, `module`, `script`, `shell`, or `sql`. It also
checks a defined set of connection-selector and credential patterns. These
checks do not identify arbitrary SQL or every possible URL or credential
embedded in a string; they are not a general-purpose sanitizer. A CLI
`one_shot_submit` request is accepted only by the dedicated
`one_shot_submitter` role; it calls `RunDispatcher.dispatch_one_shot`. An API
role cannot submit this operation through the CLI, although code with direct
dispatcher access may call `dispatch_one_shot`. The job must be in the reviewed
catalog with `reviewed=True`, `one_shot_only=True`, `task.enabled=False`, and
`task.schedule=None`. Ordinary manual, scheduled, backfill, and APScheduler
dispatch paths reject that job. A submit dry run validates the request and
returns its safe run identity without writing a ledger row.

`one_shot_run` is available only to the `one_shot_executor` role and calls the
exact-key `PersistentRunWorker.run_one_shot` path. It accepts any run key with
64 lowercase hexadecimal characters; the CLI does not verify that the key is
bound to a prior submission receipt. Operators should use the run key returned
by `one_shot_submit` and verify that receipt before invoking the executor. The
runtime requires a reviewed role plan with explicit config and sink
identifiers. `one_shot_submitter` and
`one_shot_executor` are each exclusive process roles: a plan containing either
must contain that role alone, with no API, scheduler, general worker, or legacy
role. The executor requires a fixed worker identity, lease duration, heartbeat
interval, and database I/O timeout, plus a verified worker Engine that is the
same Engine object used by the run ledger. The executor dry run validates and
reports the key without claiming or executing it. A submitter role has no
ordinary manual, scheduled, or backfill dispatch permission; the API role can
also submit one-shot work through the dedicated dispatcher method.

The dispatcher path accepts only a reviewed `one_shot_only` job. It builds a
manual run request and persists it with `claim_policy="one_shot"`. The run key
returned by submission identifies the queued row operators should pass to the
executor. The general queue claim excludes these rows, and the exact-key claim
is never reclaimed after execution starts.

The CLI reports `succeeded` only when the returned record matches the requested
key, has `claim_policy="one_shot"`, fencing token 1, a finished timestamp, and
durable `succeeded` status. A `None` result is reported as `not_claimed`; this
may mean the key is absent, already claimed, or terminal. Every other result
that does not prove the exact success condition is `uncertain`, including
worker exceptions and interruption after entering the worker boundary
(interruption keeps exit code 130). **Neither `not_claimed` nor `uncertain`
means retry is safe.** Reconcile the run key with domain evidence before
deciding how to record the outcome.

The v11 and v12 frozen selections each passed 532 tests across 22 test modules
and 17 exact offline MySQL nodes. The nodes do not use a live MySQL fixture.
They cover temporary SQLite, synthetic handlers, local subprocesses, and
offline MySQL transport/TLS/DDL checks. The coverage figures above are per
exact target with no excluded lines. This evidence accepts the candidate
CLI/runtime behavior in that isolated test scope; it does not accept its
production composition, an actual Fuyao request, source authorization, or
fleet rollout.
A plain `RunDispatcher` has no runtime profile, role plan, or open/closed
lifecycle to check, so only the runtime-bound adapter provides those guards.


The exact-type `RunLedger` check on one-shot submission and exact-key claim
also has a limited scope. It rejects duck-typed ledgers and detects replacement
or instance-shadowing of the original `enqueue` and `claim_run` methods before
those operations. Later execution-time `heartbeat`, `commit_result`, `fail`,
and `get` calls still use the injected ledger instance. This is a trusted
same-process implementation check, not a sandbox or defense against malicious
code that can mutate the process or database.

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

The Fuyao manual composition currently provides preflight only. It does not
construct or enable a `ReviewedJob`, enqueue a run, start a worker or scheduler,
reserve a source attempt, or call Fuyao. The v11/v12 CLI/runtime acceptance
does not provide or prove a Fuyao authorization grant, vendor rights, an actual
provider request, or complete A-share history. A successful preflight is not
evidence that a live one-shot request is authorized or executable; the
separate source attempt gate remains authoritative for any future physical
request.

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
