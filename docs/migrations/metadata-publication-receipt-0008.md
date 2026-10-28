# Metadata publication receipt, migration 0008

Revision `0008` follows main control/app revision `0007` and creates an empty
`od_metadata_publication_receipts` table. It is a receiver-side storage schema
for an exact bounded copy of a publication event. It does not modify the
warehouse publisher or its outbox and does not backfill existing publications.

## Stored identity and evidence

The primary key is `(producer_identity, publication_id)`. A second unique key
on `(producer_identity, dataset_id, publication_sequence)` makes a different
publication ID for the same producer sequence a conflict. Identity columns use
binary comparison on MySQL and SQLite's binary comparison. A future receiver
must canonicalize `producer_identity`, `publication_id`, `dataset_id`, and
`schema_version` as ASCII before insertion. The database schema bounds their
lengths but does not itself prove their ASCII form: SQLite `length(TEXT)` counts
characters, while MySQL `length(TEXT)` counts bytes. Requiring canonical ASCII
at the writer boundary makes those bounds and binary identity comparisons
consistent across both dialects. `dataset_id` already follows the platform's
ASCII `Identifier` contract; the future receipt writer must validate the other
fields against their declared formats as well.

A future receiver must parse `event_json_utf8` and require exact equality
between its event fields and the redundant receipt columns: publication ID,
dataset ID, publication sequence, record count, payload digest, contract
digest/evidence kind, provenance digest, schema version, event version, and
published timestamp. It must also verify the domain-separated event digest
against the exact stored bytes. A duplicate row may be treated as an
idempotent replay only after its complete event bytes and evidence match; the
schema only provides uniqueness constraints and does not implement that
comparison or replay behavior.

`producer_identity_evidence_kind` is constrained to
`caller_supplied_external_pin_unverified`. The identity value is caller supplied
from an external pin. This records its input source only; it does not prove
authentication, pin ownership, contractual authorization, or the producer's
identity. The contract fields preserve the existing distinction between an
absent digest and `caller_supplied_unverified`. The digest columns are bounded
by length only; the receiver must validate digest syntax and event-hash
computation before inserting a receipt.

`event_json_utf8` stores the exact UTF-8 bytes used for the event digest. It is
a BLOB in both database families and is bounded to 2–8192 bytes by the schema;
the MySQL BLOB avoids requiring a particular InnoDB row format for the maximum
event size. A future receiver must validate UTF-8, the event JSON contract, and
the domain-separated event digest before storage; the schema does not parse JSON
or recompute `event_sha256`. Event, payload, and provenance digests are unsigned
hashes: a matching digest can bind bytes to a value but cannot authenticate the
producer or prove that the producer was authorized. Contract digests remain
explicitly absent or caller-supplied and unverified. `published_at_us` is copied
event metadata. Because this receipt contains no fact rows, `payload_sha256`
cannot be independently recomputed from this table. `received_at_us` is the
receiver's own receive time and must be provided by the receiver. Neither field
asserts source availability or a verified platform visibility time.

## Meaning and limits

The existence of a row means only that an event copy was persisted in this
table. It does not indicate authorization, successful relay, data completeness,
data readiness, a visibility seal, or availability to an AFI consumer. There is
no status column, automatic writer, API, relay, scheduler, delivery acknowledgement,
or cross-database transaction in this slice. Older publications without an
outbox event remain absent; this migration does not synthesize receipt rows or
historical evidence.

## Database and rollback behavior

The migration supports SQLite and MySQL 8.0.16 or later. MariaDB, other
dialects, and offline SQL generation fail closed. MySQL uses InnoDB and
`utf8mb4_0900_bin` for identity comparison. SQLite uses binary text comparison
and BLOB byte-length checks.

Downgrade checks the table before dropping it. It removes the table only when
it is empty; once receipt rows exist, downgrade raises and preserves them. The
empty-table query and `DROP TABLE` are not one atomic operation, so before any
future receipt writer is enabled, an operator must stop writers, drain their
transactions, and keep writes frozen through downgrade to prevent a row from
arriving between the check and drop.

## Schema-only acceptance (2026-10-28)

The isolated SQLite acceptance run completed with exit code 0: 22 tests passed.
The schema module and migration branch-inclusive coverage were each 100% for
that run. A separate disposable, synthetic MySQL 8.4 instance exercised the
actual `0007` and `0008` upgrade bodies, confirmed that the new table starts
without backfilled rows, and checked InnoDB, binary collation, the unique-key
behavior, selected `CHECK` constraints, and both empty-table and non-empty-table
downgrade behavior. The final MySQL acceptance run exited 0.

Post-run harness analysis attributed two earlier failures to an ordering query
selecting multiple rows and a PyMySQL error-code mapping that did not match the
driver's returned exception. This attribution is the executor's analysis, not a
conclusion contained in the archived failure records, which capture only the
stage and error type. The harness was corrected before the final successful
run. The successful MySQL run retains source pins; failed attempts retain source
snapshot hashes. SQLite logs, MySQL reports, and isolation/teardown records for
the failed and successful runs are archived under
`var/reports/afi204/2026-10-28-pending-metadata-receipt/`.

This acceptance used a synthetic `0007` baseline and tested only the `0007` to
`0008` upgrade. It does not establish that the complete `0000`–`0008` migration
chain succeeds. The MySQL checks covered selected constraints only. No receipt
writer, JSON/hash validation, relay, production database, or production data
was exercised. The schema therefore remains a storage-only migration with the
behavioral limits described above.
