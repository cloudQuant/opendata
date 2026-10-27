# 0017: Sina capture discovery lookup index

Revision `0017_sina_capture_discovery_index` adds
`ix_od_fact_capture_discovery` to `od_published_fact_versions`, following
`0016_legacy_turnover_nullable`.

The full index key is:

```text
(dataset_id, publication_id, instrument_id, publication_sequence,
 is_deleted, observed_at, capture_id)
```

For the reviewed MySQL schema, the five character columns use `utf8mb4` and
have widths 128, 32, 128, 40, and 64. Their full key width is 1,568 bytes;
the signed `BIGINT` and `TINYINT(1)` add 9 bytes, for 1,577 bytes. The migration
checks the live column metadata instead of assuming that declared character
lengths imply that the explicitly declared index parts fit the nominal
3,072-byte InnoDB key limit. MySQL targets must use 16 KiB InnoDB pages and
`DYNAMIC` or `COMPRESSED` row format. InnoDB secondary-index records also carry
primary-key fields that are absent from the declared index, so this byte
calculation does not prove that the storage engine accepts the schema. The
exact DDL must pass on an isolated MySQL 8 instance before rollout.
The `dataset_id`, `instrument_id`, and `capture_id` columns must retain their
`utf8mb4_0900_bin` identity comparison from revisions 0011/0012. The other two
text columns retain their existing column collations.

## Upgrade behavior

The migration accepts online SQLite and MySQL 8.0.16+ only. It checks the exact
single Alembic marker (`0016_legacy_turnover_nullable`), the required target
column types, widths and nullability, the composite primary key, the existing
`ix_od_fact_capture_read` key order, and the absence of the new index. MySQL
also checks InnoDB storage metadata and every indexed column's charset and
maximum byte length. Unsupported schema or index states stop before DDL.

For MySQL, the only write is:

```sql
ALTER TABLE od_published_fact_versions
  ADD INDEX ix_od_fact_capture_discovery (...),
  ALGORITHM=INPLACE, LOCK=NONE
```

The migration sets only the current connection's `lock_wait_timeout` to 10
seconds around that statement and restores its prior value afterward. The
explicit algorithm and lock clauses make MySQL fail the operation if the
requested online behavior is unavailable; it cannot silently fall back to a
table copy or exclusive lock. Offline SQL generation, MariaDB, MySQL versions
outside major version 8, non-16-KiB InnoDB pages, and unsupported table formats
are rejected.

Index creation reads the fact table and writes one secondary-index entry per
row. The seven declared key parts use about 1.6 KiB per row at their maximum
utf8mb4 widths, before the primary-key fields and record overhead that InnoDB
stores in a secondary-index record. Expect substantial I/O, temporary space
use, and possible replication delay for a large table. `LOCK=NONE` still needs
metadata locks at the start and end; the 10-second setting bounds waiting for
those locks, not the total index-build duration. It is not an index size or
resource limit.

SQLite adds the same index using Alembic's transactional index operation. It
checks the reflected key order and `PRAGMA index_xinfo` effective collation and
sort order for both indexes. Each key part must use SQLite `BINARY`; a column's
inherited `NOCASE` collation is rejected even when the `CREATE INDEX` statement
does not spell out a collation. Expressions, unique indexes, and partial-index
variations are rejected before changing the schema.
Before building the full discovery index, upgrade creates a temporary partial
probe index with a false predicate, inspects its inherited key collations, and
drops it. If any discovery key would inherit a non-`BINARY` collation, upgrade
stops and removes the probe before creating the full index.

## Partial DDL and downgrade

MySQL commits DDL independently of Alembic's version-marker update. If a run
fails after the index was created, the next upgrade sees the index while the
marker still names 0016 and stops. An operator must inspect the live index and
marker offline, then explicitly reconcile the state; the migration will not
guess, drop, or adopt the index. If DDL fails before creation, the migration
restores the session lock timeout and leaves the version marker unchanged.
If the DDL succeeds but restoring the session setting fails, Alembic still
cannot advance its marker; inspect the exact index and session state offline
before retrying.

Downgrade requires the exact 0017 marker and exact full-column index shape.
SQLite drops the index transactionally. MySQL uses `DROP INDEX` with the same
`ALGORITHM=INPLACE`, `LOCK=NONE`, and bounded metadata-lock wait. A malformed or
missing index blocks downgrade before DDL.

## Acceptance boundary

This index is a prerequisite for evaluating the still-disabled Sina capture
discovery prototype. It does not enable the route, prove that the optimizer
uses the index, limit physical rows examined, provide a strict scan budget, or
establish source completeness or point-in-time semantics. Before the API can
be enabled, acceptance must inspect the production-equivalent MySQL schema,
run `EXPLAIN ANALYZE` on an isolated production-shaped copy with representative
data and filters (the statement executes the query), measure rows examined
under the agreed cap, and separately verify transaction and publication-pinning
behavior. A successful migration alone is not that acceptance.

### Recorded validation (2026-10-28)

The frozen migration and its tests were checked with 72 passing tests; the
migration coverage report is 97.46%. The report files are
[`pytest-final.log`](../../var/reports/afi204/2026-10-28-pending-sina-index/pytest-final.log)
and
[`coverage-final.txt`](../../var/reports/afi204/2026-10-28-pending-sina-index/coverage-final.txt).
The source snapshot used for the database acceptance has the same migration
and test SHA-256 values as this revision: migration
`afac37f3e53af404577a89539fd5292b5c5d2094492179d9c307a6fc2276475f`, tests
`c95f080a4fbabeb0a638d8643c5ba8eeab0f8690386511e124a75d0c107e04b8`. The
isolated-MySQL acceptance launcher was recorded as
`d541331b390edd3840793f3a123dee0487adc1690147d3d4f6f952e521763fc8`.

The DDL acceptance passed on an isolated MySQL 8.4.0 instance using a
synthetic schema, two synthetic publications, and seven synthetic fact rows.
Its minimal Alembic environment started at a synthetic 0016 marker; it did not
replay the repository's complete migration chain. The acceptance checked the
created index against the expected key shape, verified the marker and index
through upgrade, downgrade, and re-upgrade, and confirmed that the synthetic
publication and fact rows were unchanged. The synthetic snapshot table had no
seed rows, so snapshot-data behavior was not exercised. An intentionally held
metadata lock produced MySQL error 1205 at the bounded timeout; afterward the
marker remained at the synthetic 0016 state and the discovery index was
absent. The teardown record confirmed that no matching MySQL server remained
after the isolated namespace exited.

Launch-time notes report two earlier pre-start failures: one during Bubblewrap
mount setup because of a bind-source path, and another during the `awk`
bootstrap step. They produced no database acceptance result and are not
counted as successful validation. The
restricted
`mysql84-failed-stage/` and `mysql84-failed-awk/` directories retain only
teardown receipts; raw stdout and stderr for those failures were not retained.
The retained [candidate README](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-candidate/README.md)
is a pre-execution snapshot: its statement that the
candidate had not yet run is historical, not the result of the later
successful run pinned to the launcher SHA above.

Selected successful-run artifacts are retained in the restricted local
directory `var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/`.
The retained [acceptance result](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/acceptance-result.txt),
[migration transitions](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/migration-transitions.txt),
[lock-timeout result](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/lock-timeout-result.txt),
and [namespace teardown record](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/namespace-teardown-verification.txt)
capture the checked outcomes.
All 253 entries in the original outer manifest were verified against the
original evidence directory under `/tmp/od0017-mysql84-evidence.IGEy7pBr`.
Only selected artifacts were copied into the report archive, so its retained
[manifest copy](../../var/reports/afi204/2026-10-28-pending-sina-index/mysql84-success/original-outer-evidence.sha256)
has SHA-256
`a760166e1a86bd9faffe0c470f18898770acbeff067f520afd3f0b73e1d00ca4`, but its
entries still point to the original evidence under
`/tmp/od0017-mysql84-evidence.IGEy7pBr`; it is not a completeness manifest
for the selected archive. The original successful MySQL datadir remains
outside the repository under
`/tmp/od0017-mysql84-evidence.IGEy7pBr/od0017-mysql84.texuTc0Y/data`.
The MySQL datadir and TLS/private-key files were not copied into the report
archive.

This evidence covers only the migration's DDL and fail-closed behavior against
that synthetic MySQL schema. It does not validate a production database, the
full production migration history, Store or API query behavior, `EXPLAIN` or
`EXPLAIN ANALYZE`, a physical scan bound, point-in-time behavior, or full
historical data completeness. The Sina API remains disabled; this result is
not approval to enable it.
