# ODS legacy stock-daily turnover NULL migration (0016)

Revision `0016_legacy_turnover_nullable` follows `0015_source_attempt_gate` and
changes only `ods_stock_daily_akshare.成交额` from `DOUBLE NOT NULL` to
`DOUBLE NULL`. Revision `0001_ods_dwd_p0` remains unchanged. The migration does
not update rows, invent values, change a zero, or modify the DWD `Bar` contract
or real-time stock-daily collection behavior.

The legacy transfer must preserve a source `成交额 = NULL` as SQL `NULL`. The
transfer report records source and target NULL counts, and its row digest uses
distinct type tags for NULL and numeric zero. Other required source values
remain validated as finite numeric values. Existing nullable auxiliary metrics
keep their prior behavior.

The migration validates the pre-migration ODS column definitions, nullability,
business primary key, and (for MySQL) InnoDB engine before changing the column.
It accepts an online SQLite connection or MySQL 8.0.16 through 8.x; MariaDB,
MySQL 9.x, and offline SQL rendering are rejected. MySQL versions outside 8.x
remain unverified. On SQLite the
revision uses Alembic batch table recreation and rejects triggers, foreign
keys, additional unique/check constraints, unsupported table options, and
expression or partial indexes. SQLite secondary indexes must be non-unique,
simple column indexes with default ascending order and no explicit collation;
otherwise the migration fails closed. The test pins preservation of a simple
secondary index, the primary key, existing values, and the genuine zero value.

The MySQL preflight accepts only the partition layout currently required by
the legacy transfer tool: `RANGE COLUMNS(日期)` with `p2024` through
`2025-01-01`, `p2025` through `2026-01-01`, `p2026` through `2027-01-01`, and
`pmax` at `MAXVALUE`. A healthy warehouse that has already advanced its
partition horizon will fail closed. Reconcile that layout before applying
this revision; do not drop or rebuild partitions to make it pass. The ODS DDL
generator currently declares the business primary key and no named secondary
index. The MySQL acceptance record must compare the full index and partition
metadata before and after the ALTER.

Downgrade is intentionally refused for every database state. It never fills
NULL with zero and cannot silently destroy missingness. Roll forward with a
new revision if a future contract change is needed.

## One-time MySQL acceptance procedure

This procedure is for a fresh, disposable MySQL 8.4 instance and randomly named
scratch schema only. It is not production migration evidence. Do not point it at
the live warehouse or use `tests/test_warehouse_migrations.py::TestLiveReplay`,
which manages a fixed scratch database name.

1. Freeze and record the SHA-256 of revision 0016, the transfer script, and its
   focused tests. Confirm the scratch server identity and exact MySQL 8.x
   version before creating the schema.
2. Prepare the pre-0016 state in the new schema. A full-chain run applies
   revisions through 0015; a targeted 0016 smoke test may instead seed a
   synthetic 0015 version marker and create the reviewed legacy ODS shape
   directly. Label which setup was used. Insert synthetic rows containing a
   genuine `成交额 = 0` and a nonzero amount, and record all ODS rows, column
   metadata, indexes, primary key, engine, partitions, and Alembic version.
3. Upgrade only to `0016_legacy_turnover_nullable`. Confirm that the version
   becomes `0016_legacy_turnover_nullable`, exactly that column is nullable,
   the full index/partition metadata is unchanged, and all pre-existing values
   are unchanged. Insert a separate row with `成交额 = NULL` and read back both
   NULL and zero distinctly.
4. Attempt downgrade in the disposable schema and confirm it fails before any
   DDL or row change. Destroy only the explicitly created scratch schema and
   retain the command, stdout/stderr, exit status, server identity, and before
   / after checksums in the private acceptance record.

MySQL `ALTER TABLE` can take metadata locks and may rebuild or otherwise use
substantial temporary space depending on server version and table shape. This
revision makes no online/instant-DDL promise. The disposable test must record
the exact server version and observed operation; any production run requires a
separately approved maintenance window and capacity review. MySQL 9.x remains
outside the accepted range until independently tested and explicitly enabled.

## Acceptance status

**Limited isolated MySQL acceptance: PASS.** A disposable MySQL 8.4.0 instance
completed the 0016 upgrade with exit code 0. The test ran in
separate PID and network namespaces, with MySQL networking disabled and a
private Unix socket. The final teardown record confirms that no matching
`mysqld` remained after the isolated process exited.

The scratch schema used a synthetic `0015_source_attempt_gate` version marker
and a synthetic ODS table; the acceptance did not replay revisions 0001–0015
or use source/production data. Two synthetic rows held `成交额` values `0` and
`123.5`. After 0016, both values and all other row values were unchanged; the
column became nullable while its type stayed `DOUBLE`. The before/after records
also show unchanged primary key, indexes, partition layout, and table metadata.
A separate row with `成交额 = NULL` was inserted and read back. The downgrade
attempt was refused, and the row and schema metadata remained unchanged.

There were two failed disposable attempts before the successful run. The first
stopped at the startup preflight when it encountered the namespace-init/PID 1
condition, before launching MySQL. The second started only the disposable
instance but failed while creating synthetic fixture DDL because the Chinese
identifiers were garbled by a client/fixture character-set mismatch; it did
not complete the 0016 upgrade. Both failures occurred outside production and
did not affect production data. The successful run records an `utf8mb4` client
session. The success evidence, including the teardown verification, is archived
at `var/reports/afi204/2026-10-28-pending-legacy-null/mysql84-success/`. The
two failed-attempt records are under
`var/reports/afi204/2026-10-28-pending-legacy-null/mysql84-failed-namespace/`
and `var/reports/afi204/2026-10-28-pending-legacy-null/mysql84-failed-client-charset/`.
The private archive is local and ignored by Git; it may not be present on other
machines. A copy of the reviewed original 1,246-item outer evidence manifest
is at
`var/reports/afi204/2026-10-28-pending-legacy-null/mysql84-success/original-outer-evidence.sha256`
(SHA-256 `bb50e579b6b2fa531a708645fe7511db7a0263bd0c0420d988f32decb0375cdc`).
The paths recorded inside that manifest still refer to the original temporary
directory, `/tmp/od0016-mysql84-evidence.Krd9Hjyn/od0016-mysql84.TPojfiJ9/`.
The private archive contains selected log snapshots, not the complete MySQL
data directory. The acceptance launcher SHA-256 is
`c5e3952630f1ca20b29802677a8602a641d49be7039b6374badcb992ac859235`.

This acceptance establishes only that the candidate 0016 DDL and downgrade
guard worked for the recorded synthetic table on MySQL 8.4.0. It does not
validate a production migration, the complete 0001–0015 history, the full
legacy transfer, source-data coverage, DWD/API consumers, or real-time
collection. It also does not establish the ALTER algorithm, lock duration,
online/instant behavior, capacity requirements, or MySQL 9.x behavior. The
operation used MySQL's normal DDL path; no server-side lock or rebuild study was
performed. Any production migration still requires a separate maintenance and
capacity review.

The earlier focused isolated test run reported 105 passed and one failed
because the missing-table assertion expected different wording. After correcting
that assertion, the integration owner reran the focused core and CLI suite:
106 passed. The associated branch-inclusive coverage report recorded 100% for
revision 0016 and 88.12% for the transfer script; Ruff and format checks also
passed. The run log is at
`var/reports/afi204/2026-10-28-pending-legacy-null/pytest-final.log`. These
results cover the focused test suite and do not expand the MySQL acceptance
scope described above. The archive is a private local report bundle and is not
guaranteed to exist on other machines.

The transfer command still requires an empty target and creates a fresh batch
identifier for each `--apply`. A failure after committed batches is not an
automatic resumable transfer; this NULL-preservation change does not alter that
existing operational contract.
