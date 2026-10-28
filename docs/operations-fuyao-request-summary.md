# Fuyao financial fetch request summary

This note documents the caller-side request summary added to the THS/Fuyao financial
observation fetch result. It describes one small piece of request provenance; it does
not certify an upstream response, a data capture, a publication, or historical coverage.

## What the digest records

`FinancialObservationFetchResult.request_summary_sha256` is an optional field. Existing
callers that construct a result directly can omit it and receive `None`; the field uses
`compare=False`, so adding request provenance does not change result equality. A successful
`fetch()` fills it with a lowercase SHA-256 digest.

Before sending, the fetcher checks the request builder's output against the validated
query, including the exact allowed keys, value types, and values. It then copies that
mapping into an immutable mapping and computes the summary from that validated copy. The
allowlist is limited to `thscode`, `period`, `start`, `end`, and `limit`; the summary helper
rejects credential-like fields and non-scalar or out-of-bounds values.

The version 1 digest is SHA-256 over compact, UTF-8 canonical JSON containing the
normalized parameter mapping and `version: 1`. Mapping keys are sorted. The same query
parameters therefore produce the same digest for the income, balance-sheet, and cash-flow
endpoints. Endpoint identity is deliberately outside this digest.

The digest excludes the HTTP method, endpoint or URL, headers, credentials/API key, and
request body. It records what this client validated and prepared for transport before the
send. It does not prove what bytes went onto the wire, what the provider received or
accepted, which account authenticated, or what data the provider used to form its reply.
It is a deterministic hash, not encryption; sensitive values must not be added to the
allowlist, and low-entropy values can be guessed by someone who already has the digest.

The transport may retry one logical fetch. Those physical attempts share the logical
query's digest; the digest is not an attempt-level receipt and does not distinguish retry
count, timing, or individual transport outcomes. A separate attempt record or provider
receipt would be needed for those claims.

## Compatibility with raw evidence

The request-summary helper and `RawEvidenceError` now live in the side-effect-free
`opendata.data.request_summary` module. `opendata.pipeline.raw_evidence` continues to
re-export the same helper, value type, and error class for existing import paths. The
canonical version 1 algorithm and the version 1 raw-response manifest fields remain
unchanged; the new fetch-result field does not alter or create a raw-response manifest.

The error class's `__module__` now names `opendata.data.request_summary`. New pickles of
that class will therefore refer to the new module path. Older installations that predate
the module may not be able to unpickle those new objects. The legacy re-export lets the
current code resolve the old `opendata.pipeline.raw_evidence.RawEvidenceError` import and
pickle path, but it does not provide reverse compatibility for old software reading new
pickles. Do not use Python pickle as a durable interchange format for this exception.

## Verification and limits

The reported isolated acceptance run passed 173 tests using temporary data, with network
access disabled and no project `.env` loaded. The reported statement-plus-branch coverage
for the request-summary helper, financial fetcher, and raw-evidence module was 93%, 99%,
and 96%, respectively; Ruff passed. The first run had two harness-related failures under
an initial `umask 077` environment; the corrected `r2` and final runs passed, and the first
run's evidence is retained.

The run did not perform a live Fuyao capture or establish provider receipt/authentication,
durable storage, production operation, source completeness, full-history recovery, or
point-in-time correctness. Its evidence is associated with
`var/reports/afi204/2026-10-28-fuyao-request-summary/`.
