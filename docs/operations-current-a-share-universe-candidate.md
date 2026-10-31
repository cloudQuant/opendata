# Current A-share universe candidate (offline evidence builder)

Status: iteration 204 candidate tooling. This document describes an offline,
evidence-bound builder that turns already captured exchange/Fuyao catalog files
into one deterministic JSON **candidate** snapshot.

The output is **not** an authoritative universe, **not** a delisted-security
lifecycle, **not** a market-completeness attestation, and **not** a published
API/database resource. It is a review artifact whose provenance, duplicates,
conflicts, and unknowns are explicit.

## What the builder does

1. Reads exactly one manifest file using an absolute, non-symlinked regular
   path with a size limit.
2. For every manifest source, reads only the declared absolute path, rejects
   symlinks, non-regular files, oversized files, and a SHA-256 mismatch.
3. Parses only the supported captured layouts and applies only explicit field
   mappings. It never guesses a symbol, exchange, listing date, or delisting
   date.
4. Qualifies every candidate identity as declared market + six-digit symbol.
5. Reports same-identity duplicates and cross-source field disagreements
   without merging or repairing them.
6. Emits sorted, key-sorted JSON with explicit non-attestation and UNKNOWN
   fields.

No network call, `.env` read, database write, API publication, scheduler
change, or Git operation is performed.

## CLI

```bash
cd /home/yun/Documents/opendata
/home/yun/anaconda3/bin/python3.13 \
  scripts/ops/build_current_a_share_universe_candidate.py \
  --manifest /absolute/path/manifest.json \
  --output /absolute/path/candidate.json
```

`--force` replaces an existing regular output file. Symlinked outputs and
symlinked parent paths are always rejected. Exit code `2` means a stable
refusal code was printed to stderr (for example `source_sha256_mismatch`,
`path_symlink_rejected`, `path_oversize`, or `source_mapping_invalid`).

## Evidence manifest

```json
{
  "schema": "opendata.current-a-share-universe-candidate-manifest.v1",
  "as_of": "2026-10-30T12:13:34Z",
  "sources": [
    {
      "source_id": "szse.current_a_xlsx",
      "capture_id": "szse-official-xlsx-20261030-5809685a",
      "market": "CN-SZSE",
      "as_of": "2026-10-30T12:13:34Z",
      "format": "szse_current_a_xlsx",
      "path": "/absolute/path/szse-current-a-share-list.xlsx",
      "sha256": "c466df025f3d855ebb23b0a9c9a02da38d411a97936dea36775c1c01e74f0bc7",
      "mapping": {"symbol_column": "E", "name_column": "F"}
    }
  ]
}
```

Rules:

- `schema` must be exactly `opendata.current-a-share-universe-candidate-manifest.v1`.
- `as_of` and every source `as_of` must be ISO-8601 with an explicit offset.
- `market` is one of `CN-SSE`, `CN-SZSE`, `CN-BSE`. It is the only source of
  venue qualification and is never inferred from a symbol prefix.
- `source_id` is a stable lowercase identifier. `capture_id` groups raw pages
  from the same capture run.
- `path` must be absolute. `sha256` is the SHA-256 of the exact bytes.
- `mapping` values are explicit raw field names or Excel column letters. No
  aliases or automatic header detection are applied.
- A source cannot be listed twice with the same
  `(source_id, capture_id, path)`.

## Supported formats

### `normalized_rows_json`

The file is either a JSON array of flat objects or `{"rows": [...]}`. Each row
contains only scalar JSON values (string, number, boolean, null). Manifest
mapping keys:

- `symbol_field` (required): field containing the source-native code.
- `name_field` (optional): display name used for candidate-field agreement.
- `exchange_field` (optional): observed exchange label (`SH`, `SZ`, `BJ`).
- `asset_type_field` and `asset_type_value` (optional, together): when set, a
  row whose asset type is absent or different is `unsupported`.
- `status_field` (optional): retained as an uninterpreted raw value.

Source-native codes are accepted only in these explicit forms: `600000`,
`600000.SH`, or `SH600000`. The suffix/prefix exchange must match the declared
market; otherwise the row is a `conflict`.

### `szse_current_a_xlsx`

Uses the existing raw-only SZSE XLSX parser with `dataset_class=current_a`. The
manifest must declare `symbol_column` and may declare `name_column`, using
Excel letters (for example `E` and `F`). The parser's observed-header contract
is required. The caller-declared category remains a caller label; the workbook
is not authenticated as an official SZSE response by this tool.

### `sse_instrument_pages_json`

One manifest entry per captured SSE response page, grouped by
`(source_id, capture_id)`. Each entry requires `page.selector` (one of the four
fixed selector values) and `page.page_sequence`. The body must be the SSE
envelope with `result` and optional `pageHelp`. Manifest mapping keys are the
same as normalized rows. The existing SSE adapter recalculates the response
hash from exact bytes.

Use a distinct `capture_id` for each selector run so page sequences are
unambiguous. If the supplied page sequence has a gap, each captured page is
still parsed individually and the gap is reported as
`pagination: UNKNOWN` with `page_sequence_gap`; rows are not discarded.

### `fuyao_instrument_pages_json`

One manifest entry per captured Fuyao catalog page, grouped by
`(source_id, capture_id)`. Each entry requires `page.page_sequence`. The body
must be the Fuyao envelope with `data.item`. Defaults match the documented
Fuyao A-share profile (`thscode`, `name`, `exchange`, `asset_type`, `status`);
any of these can be overridden in `mapping`. Each row must carry
`asset_type=a-share` and a qualified `NNNNNN.SH|SZ|BJ` code; a suffix that
disagrees with the declared market is a conflict, not a merge.

## Identity and reconciliation semantics

- A candidate identity is the pair `(market, six-digit symbol)`.
- A BSE-reserved prefix (`4...`, `8...`, `920...`) in a non-BSE source is
  reported as `bse_reserved_prefix_outside_declared_market`. It is never
  re-labeled as BSE and never merged into SH/SZ.
- Two or more candidate rows for the same identity are retained and listed as
  duplicates, even when they agree.
- Candidate-field agreement currently covers `display_name` only. Two
  different non-null names for the same identity produce a conflict; the rows
  are marked `conflict` with reason `display_name_disagreement` and the
  identity is excluded from `candidates`.
- A row is `unknown` when its code is missing, not text, or not one of the
  accepted code forms.
- A row is `unsupported` when a required asset type field is absent or does not
  equal the declared value.
- A row is `conflict` when its source code suffix/prefix or observed exchange
  field disagrees with the declared market.
- Absence of a row is never converted into a listing, delisting, suspension, or
  retention conclusion.

## Pagination, retention, and completeness

Manifest `page` objects may declare `page_sequence`, `terminal`,
`reported_total` with `reported_total_basis` (`row_count` or `page_count`),
`declared_row_count`, and cursor fields.

- `DECLARED_CONSISTENT` means only that the supplied declarations are
  self-consistent (contiguous pages, terminal last page, stable total, matching
  row counts, coherent cursors).
- Missing or truncated declarations produce `UNKNOWN` with stable reason codes
  such as `no_pagination_evidence`, `page_sequence_gap`,
  `terminal_evidence_missing`, or `row_count_mismatch`.
- Even `DECLARED_CONSISTENT` is not proof. The top-level pagination object
  always carries `attested: false`, and
  `unknowns.pagination_termination` is always `UNKNOWN`.
- `unknowns.retention`, `unknowns.market_completeness`, and
  `unknowns.source_authenticity` are always `UNKNOWN`.

## Output

Top-level fields include:

- `schema_version`, `as_of`, `manifest` (schema, SHA-256, as-of)
- `source_manifests` (every exact file path and SHA-256)
- `source_assessments` (per-capture row count and pagination assessment)
- `counts` (`records_total`, `candidates_total` row count,
  `candidate_identities_total`, per-market and per-source disposition counts)
- `candidates` (deduplicated identities with all agreeing records)
- `records` (every row with market, symbol, source-native code, capture ID,
  as-of, page/row provenance, file hash, row hash, disposition, reason, and
  selected raw fields)
- `duplicates` (identity groups with more than one candidate row and an
  `agreement` flag)
- `conflicts` (display-name conflict groups and row-level identity conflicts)
- `pagination`, `retention`, `unknowns`
- `bse_supported`: `"unknown"` unless a BSE source was parsed, then `"true"`
- `universe_census_attested: false`
- `delisted_lifecycle_included: false`
- `authoritative: false`
- `publication: "NOT_PERFORMED"`
- `database_or_api_published: false`

## Explicit non-claims

This tool does **not**:

- publish or update a database, API route, scheduler, or production artifact;
- claim a complete current A-share universe or full-A coverage;
- include or prove a delisted-security lifecycle;
- prove pagination termination, source retention, or historical availability;
- authenticate the exchange/Fuyao source or its data-use terms;
- resolve cross-source disagreements automatically;
- infer an exchange or BSE membership from a symbol prefix.

`bse_supported: "true"` only means at least one BSE source capture was parsed;
it does not mean BSE coverage is complete.

## Validation and real-sample status

Synthetic tests (no real capture required):

```bash
cd /home/yun/Documents/opendata
/home/yun/anaconda3/bin/python3.13 -m pytest --noconftest \
  -o addopts='' tests/test_current_a_share_universe_candidate.py -q
```

The repository `pytest.ini` adds a repository-wide coverage threshold. Running
only this file with the default addopts reports the test results correctly but
fails the global 84% coverage gate; `-o addopts=''` isolates the tests from that
unrelated gate.

Opt-in real capture replay (SZSE current-A workbook already archived under
`var/reports/afi204/`):

```bash
OPENDATA_AFI204_REAL_CAPTURE=1 /home/yun/anaconda3/bin/python3.13 \
  -m pytest --noconftest -o addopts='' \
  tests/test_current_a_share_universe_candidate.py -q
```

The real run parses 2,905 SZSE rows with no conflicts and
`pagination_status: UNKNOWN` because the workbook provides no page-chain
evidence. SSE live captures in the archive contain receipts but no catalog
response bodies; BSE captures do not contain a parseable catalog; no Fuyao
catalog response body was archived. Those formats are implemented for future
captures but their real-sample run status is **NOT_RUN** for this iteration.

Evidence summary and exact commands/results:
`/home/yun/Documents/ai-for-investor/var/reports/afi204/a-share-universe-candidate-20261031/`.