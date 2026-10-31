# Stock-daily publication capacity gate

Status: **default off**. This gate is a deployment/capacity control for
`GET /api/v2/stock-daily/records`. Enabling it does not approve a data release,
does not authorize access to source data, and is not production acceptance.

The route remains `include_in_schema=False` whether the gate is enabled or not.

## What the gate proves

When a request succeeds, the process had all of the following at request time:

- `STOCK_DAILY_PUBLICATION_API_ENABLED` was the literal lowercase string `true`.
- `STOCK_DAILY_PUBLICATION_CAPACITY_FILE` named a readable, at-most-64 KiB JSON file.
- The SHA-256 of the exact file bytes equaled
  `STOCK_DAILY_PUBLICATION_CAPACITY_SHA256` (lowercase 64 hex).
- The file decoded as strict UTF-8 JSON with no duplicate keys, no unknown
  fields, valid field values, and a non-empty UTC validity window that contains
  the request time.
- The resolved publication matched the attested `dataset_id`, `source_id`,
  `semantic_version`, and canonical contract digest.
- If the attestation pins `publication_id`, it matched the requested publication.
- The requested/stored date window and effective page size were within the
  attested `max_window_days` and `max_page_size`.

Existing authentication, dataset grant, snapshot-create grant, contract,
selection, cursor, and row checks still run unchanged. The gate adds a bound; it
does not loosen any of them.

## What the gate does NOT prove

- **No data authorization.** It does not grant legal rights, licenses, source
  terms, or permission to redistribute rows. Grants remain a separate control.
- **No real-data proof.** A valid attestation says nothing about whether the
  publication contains real observations rather than synthetic, empty, or
  mislabeled data.
- **No source capacity or availability proof.** It does not measure AkShare
  quotas, throughput, reliability, rate limits, or production load capacity.
- **No coverage or completeness proof.** Coverage remains `unknown` in the
  response. The gate does not establish history depth, listing dates, interior
  gaps, or full A-share coverage.
- **No signature or authority proof.** The digest binds file bytes, but its
  delivery channel is trusted configuration. Anyone who can change both the file
  and the configured digest can change the attestation.
- **No OpenAPI publication.** The path stays hidden from generated API docs.

## Settings

| Setting | Required value |
| --- | --- |
| `STOCK_DAILY_PUBLICATION_API_ENABLED` | Literal `true`; any other value is disabled. |
| `STOCK_DAILY_PUBLICATION_CAPACITY_FILE` | Absolute path to the operator-owned JSON file. |
| `STOCK_DAILY_PUBLICATION_CAPACITY_SHA256` | Lowercase 64-hex SHA-256 of the exact file bytes. |

Unset, empty, malformed, inactive, or mismatched settings all produce the
route's uniform `404 not_found`. The service does not return a configuration
error that would reveal which setting failed.

The file is re-read and re-hashed on every request. There is no process-wide
attestation cache, so changing the file or settings takes effect without
relying on a stale in-memory value. Keep the file small and local.

## Attestation file

Schema: `stock-daily-capacity-v1`. Unknown fields are rejected.

```json
{
  "schema_version": "stock-daily-capacity-v1",
  "dataset_id": "cn-equity-stock-daily",
  "source_id": "akshare.daily",
  "semantic_version": "stock-daily-raw-v1",
  "contract_sha256": "<64 lowercase hex: canonical dataset contract digest>",
  "publication_id": "<optional 32 lowercase hex publication pin>",
  "max_window_days": 3660,
  "max_page_size": 1000,
  "not_before": "2026-01-01T00:00:00Z",
  "expires_at": "2027-01-01T00:00:00Z",
  "evidence_ref": "docs/evidence/stock-daily-capacity-review.md",
  "attested_by": "operator-name-or-team"
}
```

Field rules:

- `schema_version`: exactly `stock-daily-capacity-v1`.
- `dataset_id`: exactly `cn-equity-stock-daily`.
- `source_id`: exactly `akshare.daily`.
- `semantic_version`: exactly `stock-daily-raw-v1`.
- `contract_sha256`: canonical digest of the reviewed `DatasetContract`; the
  publication stores the same value under `provenance["contract_sha256"]`.
- `publication_id`: optional exact publication pin. Omit or set `null` to allow
  any publication that satisfies the other bindings.
- `max_window_days`: integer 1 through 3660.
- `max_page_size`: integer 1 through 1000.
- `not_before`, `expires_at`: RFC3339 timestamps with an explicit offset or `Z`,
  normalized to UTC. The validity interval is half-open:
  `not_before <= now < expires_at`. Naive timestamps and equal/reversed bounds
  are rejected.
- `evidence_ref`, `attested_by`: bounded printable text, 1-512 characters, no
  control characters. Do not put credentials or other secrets in either field.

## Operator procedure

1. Produce the attestation from the reviewed deployment facts. Do not copy
   limits from a different environment.
2. Write it to a service-owned location, for example
   `/etc/opendata/stock-daily-capacity.json`. Restrict permissions to the
   service account; avoid world-writable directories.
3. Compute the digest of the exact file:
   `sha256sum /etc/opendata/stock-daily-capacity.json`.
4. Set the three settings in the service environment (or its configured env
   file) and restart the service.
5. Verify that an unauthenticated/unauthorized request still returns the normal
   auth or 404 behavior and that the OpenAPI document still does not list the
   path.
6. Record the evidence reference and the digest in the deployment change record.

Do not edit the file in place without recomputing the digest and restarting or
rolling out the new settings. There is no partial enablement: a bad digest, a
malformed document, or an expired window disables the route completely.

## Failure modes

All of these return the same `404 not_found` envelope:

- flag missing or not exactly `true`;
- path missing, empty, unreadable, or file absent;
- file larger than 65,536 bytes;
- configured digest malformed or different from the file bytes;
- invalid UTF-8, non-object JSON, duplicate keys, or NaN/Infinity;
- unknown, missing, or out-of-range fields;
- naive/reversed timestamps, or a validity window that excludes now;
- publication source, dataset, semantic version, or contract digest mismatch;
- publication pin mismatch;
- requested window above `max_window_days`;
- effective page size above `max_page_size`.

Malformed client request shapes still return the route's existing `422`
responses. Grant/contract/storage failures that occur before a publication is
resolved still return the existing `403`/`503` responses. The gate deliberately
does not disclose configuration state through errors.

## Rollback

Set `STOCK_DAILY_PUBLICATION_API_ENABLED=false` (or remove it) and restart or
reload the service. The route immediately returns `404` again; the file and
digest settings may remain in place. Rollback does not delete publications,
warehouse rows, or snapshots already created. Those snapshots remain subject to
their own expiry and to the normal grant checks.

If the attestation must be withdrawn while the flag remains on, delete the file
or replace the configured digest with a value that does not match. The route
fails closed.
