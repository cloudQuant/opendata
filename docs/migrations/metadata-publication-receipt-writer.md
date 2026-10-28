# Metadata publication receipt writer

This document describes the receiver-side writer added for the empty
`od_metadata_publication_receipts` table from main control-database migration
`0008`. The writer stores one bounded copy of a publication-outbox v1 event. It
does not relay that event or establish that the published facts are available
to a consumer.

## API and transaction contract

The public entry point is
`opendata.data.metadata_receipt_writer.receive_metadata_publication_receipt`:

```python
receive_metadata_publication_receipt(
    connection,
    *,
    producer_identity,
    event_json_utf8,
    event_sha256,
)
```

The caller must pass an active SQLAlchemy transaction. The function neither
commits nor rolls back that outer transaction. It inserts inside a nested
savepoint so a duplicate-key conflict or a constraint error can be handled
without committing a receipt independently of the caller. Rolling back the
caller's transaction removes a newly inserted receipt. On SQLite, a zero-row
`UPDATE` first forces the physical transaction to begin before the savepoint;
on MySQL, duplicate resolution uses a locking current read after the failed
insert savepoint has rolled back.

The result reports the producer pin, publication ID, dataset ID, sequence,
verified event digest, first stored local receive time, and whether the call
was an exact replay. Malformed inputs and unsupported targets raise
`MetadataPublicationReceiptError`; an identity already bound to different
receipt content raises `MetadataPublicationReceiptConflictError`. Other
database integrity and statement errors propagate to the caller.

## Validation and idempotency

Before inserting, the writer requires a 1–128 character printable ASCII
`producer_identity` with no surrounding whitespace. The event must be 2–8192
exact bytes, decode as strict UTF-8, and contain one JSON object with unique
keys and exactly the publication-outbox v1 fields. It validates the event type
and version, normalizes the candidate through the existing publication-outbox
contract, rebuilds the canonical event bytes, and requires those bytes to equal
the supplied bytes. It also recomputes the domain-separated SHA-256 digest and
compares it with the caller-supplied `event_sha256`; it does not replace a
missing or incorrect digest.

The writer copies the event fields into the receipt columns and stores the
exact event bytes. The database primary key
`(producer_identity, publication_id)` and unique key
`(producer_identity, dataset_id, publication_sequence)` each define a possible
replay identity. A duplicate is accepted only if both identities resolve to
one row and every stored event field and byte matches. The original
`received_at_us` is retained. Reuse of either key with different content, two
keys resolving to separate rows, or a duplicate error without a matching row
fails closed. Only the reviewed SQLite unique-key errors and MySQL duplicate
error 1062 enter this replay path; a MySQL CHECK failure such as error 3819 is
not treated as a duplicate.

SQLite is supported. MySQL must be non-MariaDB MySQL 8.0.16 or later, and the
receipt table must currently use InnoDB. Each MySQL call checks the current
database catalog with one bound `information_schema.TABLES` query; it does not
cache the engine result. Other dialects, unsupported MySQL versions, a missing
or ambiguous table, and a non-InnoDB table are rejected.

## Evidence and limits

`producer_identity` is a caller-supplied external pin. The fixed evidence kind
`caller_supplied_external_pin_unverified` describes that input; it does not
authenticate the producer, establish pin ownership, or verify that the caller
is authorized. The writer has no signature-verification key or authenticated
producer channel.

The event digest check establishes that the supplied event bytes match the
supplied digest under the publication-outbox v1 hash domain. It does not prove
where the bytes originated or who created them: a caller able to choose both
the bytes and digest can construct a matching pair. There is no independent
source lookup or signature check. The `provenance_sha256` value is copied from
the event and is not independently resolved by this receipt writer.

`contract_sha256` is preserved as either absent or
`caller_supplied_unverified`. The writer does not fetch a contract, validate
its contents, or promote the evidence to verified. A matching contract digest
therefore remains an unverified caller-supplied claim.

`received_at_us` is computed from the receiver process's local wall clock when
the function is called. It is not a trusted timestamp, a monotonic clock, or
tamper evidence. An exact replay returns the first stored value. The event's
`published_at_us` and `payload_sha256` are copied metadata; because this table
contains no fact rows, the writer cannot independently recompute the payload
digest or establish source availability time.

The writer only compares existing stored columns and exact event bytes during
a replay. This can detect a mismatch against the event being replayed, but the
table does not prohibit direct `UPDATE` or `DELETE` by a database principal,
record who changed a row, or provide an independently anchored audit log.
Changes made outside the writer are therefore not tamper-proof evidence, and a
row's presence alone does not prove it was never changed or deleted.

There is a current label-contract mismatch. The publisher's `_label` accepts
nonempty, bounded, single-line `dataset_id` and `schema_version` labels,
including some Unicode or other values outside the run-identity ASCII
`Identifier` pattern. The receipt writer accepts only
`[A-Za-z0-9][A-Za-z0-9._:/+\-]*` for those event fields so they fit the
documented cross-dialect receipt identity contract. A publication valid under
the publisher's wider label rule can therefore be rejected by the receipt
writer. It is rejected explicitly, without lossy normalization; the contracts
need a coordinated migration before that publisher label space can be received
in full.

Persisting a row means only that this receiver transaction stored an event
copy. The writer has no relay loop, delivery acknowledgement, retry scheduler,
consumer API, readiness status, completeness check, visibility seal, or
cross-database transaction. It does not mark the underlying publication
ready or prove delivery to `ai-for-investor`.

## Acceptance evidence (2026-10-28)

The final isolated SQLite writer run completed with **54 tests passed**; its
log is `sqlite/pytest-v6.log` in the evidence archive. The same run recorded
179/181 covered statements and 70/72 covered branches for
`metadata_receipt_writer.py`, or 249/253 combined (98.4%, reported as 98%).
Ruff check and format-check were reported as passing by the implementation
agent, but their raw output was not included in the archive. The archive is
`var/reports/afi204/2026-10-28-pending-receipt-writer/`; it also retains
coverage output, MySQL reports, source pins, and the earlier failed MySQL
attempt.

A separate disposable MySQL 8.4 instance, reachable only through its private
Unix socket, exercised the actual `0007` and `0008` migration bodies after a
synthetic Alembic version marker at `0006`. The run used independent database
connections at `REPEATABLE READ`: one connection retained an old empty snapshot
while another inserted and committed the receipt; replay then resolved the
exact row with a current locking read and retained its first receive time. It
also exercised both unique-identity conflict cases, caller outer rollback,
MySQL CHECK error 3819 handling, and refusal to write to a MyISAM receipt
table. The unique-key shape was verified before the MyISAM guard case. Because
the wide source-sequence index exceeds MyISAM's key-width limit, that disposable
MyISAM fixture dropped this unique index before conversion; the guard case
therefore tests engine refusal, while uniqueness was tested earlier on the
InnoDB table.

An earlier MySQL attempt failed with status `myisam_fixture_conversion_failed`
at stage `myisam_guard`; its evidence is preserved under `mysql-failure/` in
the archive. The later harness analysis attributed that failure to the MyISAM
index-width issue, but the failure record contains only the stage and error
code. That attribution is a post-run inference, not a fact established by the
failure record. The successful harness setup removed the wide source-sequence
index from the disposable MyISAM fixture after verifying the full unique-key
shape on InnoDB. The final successful run does not retroactively change the
failure record.

These results cover the isolated writer behavior and selected MySQL
constraints. The MySQL run did **not** validate the complete historical
`0000`–`0008` migration chain: it applied only the actual `0007` and `0008`
revisions from a synthetic `0006` marker. It used synthetic event bytes and a
disposable database, not production data, credentials, service wiring, or
application deployment. No real producer authentication, event relay,
consumer/API delivery, data readiness, or full end-to-end availability was
tested.
