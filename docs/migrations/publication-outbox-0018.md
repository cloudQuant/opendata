# Publication outbox, warehouse revision 0018

Revision `0018_publication_outbox` follows
`0017_sina_capture_discovery_index` and creates `od_publication_outbox`. Runtime
event insertion is part of `publish_in_connection`'s new-publication branch.
Publication metadata, fact revisions, head update, and the event use the same
caller-owned transaction and publication savepoint. The publisher's existing
idempotency hit and uniqueness-race replay paths return the existing
publication without inserting or repairing an event.

The migration adds an empty table and performs no historical `INSERT ... SELECT`
or other backfill. Existing publications remain without outbox rows. Do not
create events for them by invoking private helpers or manual repair scripts.

## Frozen event contract

The event is keyed by `publication_id`; a unique `(dataset_id,
publication_sequence)` constraint independently prevents a second event for
the same publication sequence. The publication foreign key uses `ON DELETE
RESTRICT`. Each row freezes the dataset, sequence, record count, payload hash,
schema version, and original publication timestamp.

`contract_sha256` is nullable. When `provenance_json` contains a valid
lowercase `contract_sha256`, the event stores that caller-provided value with
`contract_evidence_kind="caller_supplied_unverified"`; the generic publisher
does not resolve or authenticate a registry record. When the field is absent,
the event stores a null hash and `contract_evidence_kind="absent"`. It never
derives or labels a value as a contract digest. `provenance_sha256` separately
hashes the exact canonical UTF-8 provenance JSON persisted on the publication.

The event JSON uses UTF-8, sorted keys, compact separators, and rejects
non-finite numbers. `event_sha256` is

```text
SHA256("opendata.publication-outbox.event.v1\n" || canonical_event_json_utf8)
```

The runtime publisher accepts SQLite and MySQL 8.0.16+ only for this write
path. It does not create engines, commit or roll back the caller's transaction,
or perform cross-database work. The migration deliberately rejects offline SQL,
unsupported dialects, MariaDB, and mismatched revision markers. Its schema
checks validate required publication columns and selected types, together
with the parent tables' storage engines; they are not a complete parent-schema
fingerprint. On MySQL, revision 0018 requires the existing
`od_publication_heads`, `od_publications`, and `od_published_fact_versions`
tables to report `InnoDB`; it never changes a legacy table engine. It creates
the outbox as `InnoDB`. Before any publisher write, one uncached, parameterized
query against `information_schema.TABLES` and the current `DATABASE()` must
return exactly one row for each of the four participating tables and verify
each uses `InnoDB`; missing, extra, or duplicate names, unknown engine metadata,
query errors, or any other engine fail closed. The query is repeated per call
without caching, so engine changes are observed while the guard costs one
metadata round trip. Fresh runtime metadata also declares InnoDB for those four
tables. Downgrade refuses to drop a non-empty outbox.

If a deployed database has a non-InnoDB parent table, 0018 stops before DDL.
Plan and validate any engine conversion separately against a backup and an
isolated copy; this migration does not perform an implicit production rewrite.
The isolated test run passed 246 tests; reported coverage was 90% for the
publication store and 98% for the outbox. An isolated synthetic MySQL 8.4 run,
starting from a synthetic 0016 revision marker, applied the real 0017-to-0018
migrations and exercised the runtime write path. It confirmed all four tables
were InnoDB, rejected a MyISAM parent, and checked successful commit, caller
rollback, and a forced outbox insert failure (trigger signal, errno 1644)
with recovery to the pre-call baseline. It also confirmed that replaying an
older publication does not append an event and exercised concurrent calls
through two database connections. This evidence covers the application code
and that synthetic isolated instance only; it does not cover production, the
earlier migration chain, or deployed data.

The event is insert-only in the publisher path, but the schema does not install
triggers that reject direct SQL `UPDATE` or `DELETE`. Protect event rows with
least-privilege database grants and operational controls; `ON DELETE RESTRICT`
protects the referenced publication row only.

## Acceptance boundary

This slice establishes local transactional event creation only. It does not
implement a delivery relay, consumer receipts, retry leases, cross-database
publication verification, idempotent consumer application, or a sealed
completion protocol. It is not the complete A04-03 acceptance. The isolated
tests and synthetic MySQL 8.4 run above do not constitute production acceptance
or prove the full earlier migration chain, relay, receipts, or seal.
