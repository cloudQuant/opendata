# Revision 0014 MySQL version marker width

Revision `0014_sina_fzb_slot_reconciliation` is 33 characters, while
Alembic's default `version_num` column is `VARCHAR(32)`. Revision 0014 keeps its
registered ID and `0015_source_attempt_gate` keeps its existing parent. On a
supported MySQL target, 0014 now verifies the live migration state and widens
the configured Alembic marker column to `VARCHAR(64)` before creating either
reconciliation table. A marker column already at least 64 characters wide is
left at its current width.

The preflight uses the `MigrationContext` version table name and schema, then
SQLAlchemy inspection and Alembic operations so configured identifiers are
quoted by the dialect. It proceeds only when all of these facts are true:

- The configured Alembic version table exists and contains only a bounded
  `VARCHAR version_num` column.
- That column is `NOT NULL`, is the table's sole primary key, and has no server
  default.
- Its sole stored value is exactly `0013_sina_fzb_fanout_groups`.
- Neither `od_sina_fzb_slot_reconciliation_state` nor
  `od_sina_fzb_reconciled_slots` already exists in the migration's target
  schema.

An unexpected version table shape, value, or target table is an error before
any migration write. MySQL DDL is not transactionally reversible. In
particular, if an older 0014 attempt created either table but failed before
Alembic recorded the 33-character revision, do not blindly retry, delete the
tables, or stamp the database. First take an offline backup and independently
compare `SHOW CREATE TABLE`, indexes, constraints, and the version row with the
frozen 0014 migration. If both tables are complete and exact, an operator may
plan a separately reviewed version repair; partial or mismatched objects need
manual recovery on a disposable copy before any production change is
authorized. This migration does not automate resume, cleanup, or stamping.

A non-strict MySQL server may have truncated the old 33-character marker. In
that case Alembic may reject the stored revision before it calls migration
0014, so this guard cannot repair it. Preserve the database, inspect the full
schema and version history offline, and use a separately reviewed recovery
procedure. Do not treat a truncated value as a valid 0014 revision.

This revision's MySQL path requires an online MySQL 8.0.16+ connection. Offline
SQL generation is deliberately rejected because it cannot inspect the marker
column, stored revision, or possible partial 0014 tables. SQLite continues to
use its existing schema path without inspecting or changing an Alembic version
table. MariaDB, MySQL 8.0.15 and earlier, MySQL 9.x, and other dialects are
rejected; MySQL 9.x has not been validated. Downgrade does not narrow
`version_num`, and its existing evidence-preservation guard remains in force.

## Disposable MySQL 8 acceptance

A one-time online acceptance completed on an isolated MySQL 8.4 instance using
the Unix socket transport only; TCP was not tested. Its starting point was a
synthetic `0013_sina_fzb_fanout_groups` marker; revisions `0001` through `0013`
were not executed in this run. From that marker, the actual Alembic upgrade
through `0015_source_attempt_gate` verified that `version_num` widened from
`VARCHAR(32)` to `VARCHAR(64)`, the complete 33-character 0014 marker could be
recorded, the 0014 tables and expected index were created, and the 0015 MySQL
no-op did not create gate tables. The InnoDB sentinel row remained unchanged.
Downgrade to 0014 preserved its full marker and width 64. The actual 0014
downgrade then ran to 0013 only while both 0014 tables were empty, and the
version column remained 64 characters.

The isolated migration regression run reported 33 tests passed, with recorded
coverage of 87% for 0014 and 100% for 0015. Evidence is retained under
`/home/yun/Documents/opendata/var/reports/afi204/2026-10-28-pending-warehouse-revision/mysql-8.4-scoped/`.

This is a bounded compatibility result, not a full-chain or production
acceptance. Revisions 0001–0013 were not replayed; the 0014 downgrade was
verified only with empty reconciliation tables; production endpoint behavior
and MySQL 9.x were not tested. Offline MySQL `--sql` rendering remains
intentionally rejected because the migration requires live inspection. Do not
run downgrade against a live warehouse.
