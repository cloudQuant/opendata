# Metadata receipt Unicode label compatibility

This note records the receipt-side alignment with the publisher's existing
`dataset_id` and `schema_version` label contract. It documents Alembic revision
`0009`, which follows receipt-table revision `0008`. It does not change the
publisher's canonical event format or authenticate the publisher.

## Publisher label contract

The publication outbox treats `dataset_id` and `schema_version` as bounded
labels, not as the platform's ASCII `Identifier` type. The publisher accepts a
non-empty `dataset_id` of at most 128 Python string characters and a non-empty
`schema_version` of at most 64. Both must have no leading or trailing
whitespace, and neither may contain a code point below U+0020. Unicode and
internal spaces are allowed. The publisher does not transliterate, trim, or
normalize these values; their original text is carried into canonical compact
JSON (`ensure_ascii=False`), encoded as UTF-8, and covered by the existing
domain-separated event digest.

For these limits, Python `len()` counts Unicode code points. SQLite's
`length(TEXT)` and MySQL's `CHAR_LENGTH()` count characters, so a multi-byte
UTF-8 label may occupy more bytes than the corresponding numeric limit while
still satisfying the publisher's contract. Unicode values that differ by code
point sequence remain distinct input values; no normalization equivalence is
introduced.

## Revision 0009

Revision `0008` created the receipt table with byte-oriented `LENGTH()` checks
for `dataset_id` and `schema_version`. Those MySQL checks were narrower than the
publisher's character limits. Revision `0009` aligns the receipt constraints:

- On MySQL 8.0.16 or later within MySQL 8, one `ALTER TABLE` drops and recreates
  both checks using `CHAR_LENGTH(dataset_id) BETWEEN 1 AND 128` and
  `CHAR_LENGTH(schema_version) BETWEEN 1 AND 64`.
- On SQLite, upgrade and downgrade are no-ops. SQLite's existing `length(TEXT)`
  checks already count characters.
- The revision changes no row values, event bytes, canonical JSON, or digest.
  It does not reserialize or backfill receipt events.

The table's existing identity behavior remains in place: MySQL text identity
columns use `utf8mb4_0900_bin`, while SQLite uses its default binary text
comparison. The revision does not broaden the separate `producer_identity`
field, which remains bounded printable ASCII in the writer, or alter the
publication ID, sequence, or unique-key definitions.

## Downgrade and supported targets

Before restoring the old MySQL byte limits, downgrade checks whether any
existing row has `LENGTH(dataset_id) > 128` or
`LENGTH(schema_version) > 64`. If so, it raises before issuing DDL and keeps
the rows and character-based constraints. If no such row exists, one `ALTER
TABLE` restores the original byte checks. The migration does not rewrite or
delete rows.

The downgrade preflight and the DDL are separate operations. A concurrent
write can race with that check; if it creates a value outside the old byte
limits before the constraints are restored, the MySQL `ALTER TABLE` may fail
when it validates the table. The combined DDL is atomic on the supported MySQL
target and should leave the prior constraints intact on failure. Stop and drain
receipt writers for a controlled downgrade; do not treat the preflight as a
concurrency lock.

Revision `0009` supports live SQLite and MySQL 8.0.16 or later within the MySQL
8 family. MariaDB, unsupported dialects, and offline SQL generation fail
closed. The receipt writer separately requires SQLite or non-MariaDB MySQL
8.0.16 or later within the MySQL 8 family and an InnoDB receipt table. A schema
migration alone does not make the receipt path a production relay or delivery
service.

## Evidence and limits

The supplied isolated acceptance record reports 89 synthetic offline tests
passing, including SQLite fixtures and simulated MySQL DDL, with branch-inclusive
coverage of 98% for the receipt writer, 100% for the schema module, and 90% for
the migration. These tests use synthetic data; the actual MySQL run is reported
separately below.

A disposable MySQL 8.4 acceptance run also passed using a synthetic `0007`
version marker followed by the actual `0008` and `0009` migration bodies. It
confirmed that an ASCII event's exact bytes and hash remain unchanged, labels
at the 128/64 character boundaries accept multi-byte Chinese text, exact
replay succeeds, and values one character over either limit are rejected.
Downgrade refused existing rows that exceeded the old byte limits. A
deterministic race hook then inserted a byte-over-limit test row after the
downgrade preflight and before the `ALTER TABLE`; MySQL returned CHECK error
3819, and the synthetic row, revision state, and `CHAR_LENGTH` checks remained
in place. After deleting that synthetic row, downgrade succeeded. This covers
the tested race interleaving; it is not a guarantee for every concurrent
workload.

An earlier candidate race-harness run reported failure after the migration
operation itself succeeded. Its hook intended to inject a byte-over-limit row
between downgrade preflight and DDL, but failed to match the generated SQL
because of a whitespace expectation; the hook was never injected and no race
was exercised. This is a harness-hook miss, not a database or migration
failure and not evidence of a migration defect. The passing run and this
harness report are retained under
`var/reports/afi204/2026-10-28-receipt-unicode-contract/mysql-acceptance/`.

The retained `acceptance.json` records `PASS`; the primary integrator observed
exit code 0 from the outer command. These are records of the same execution,
not independent attestations: the controller and worker logs are empty, and
the report has no independent signature or syscall trace. The MySQL startup
output included a warning that the optional `component_reference_cache.so`
could not be loaded, although the service started and stopped successfully. It
also included a self-signed CA warning while network access was disabled; this
warning is recorded as environment output and is not treated as a failed
acceptance result. The original MySQL runtime directory's data and log files
were mode `0640`; the retained archive copies are mode `0600` under a parent
directory with mode `0700`. Filesystem permissions do not make the report a
cryptographically signed artifact.

These runs use synthetic data and a synthetic `0007` marker. They do not
validate the complete historical migration chain or any production database,
rows, or workload.

`producer_identity` and its evidence kind remain caller-supplied and
unverified. A matching event digest proves only that the exact supplied bytes
match that digest under the declared hash domain; it does not prove who sent
the event, ownership of the external pin, contractual authorization, or that
the producer delivered it. The receipt row records a local persisted copy; it
does not prove data readiness, completeness, successful relay, or consumer
availability.

This note is the current description of the label compatibility change. The
earlier [0008 schema note](migrations/metadata-publication-receipt-0008.md)
and [writer note](migrations/metadata-publication-receipt-writer.md) describe
the state before this alignment and may retain historical references to the
ASCII-only receiver mismatch.
