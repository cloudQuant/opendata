# Raw response cache operations

The raw-response cache is opt-in and disabled by default. Enable it with
`RAW_RESPONSE_CACHE_ENABLED=true`. Its root is `CACHE_DIR/raw_responses`; the
legacy `OPENDATA_CACHE_DIR` name is accepted as an alias for `CACHE_DIR`.
`CACHE_TTL_SECONDS` controls entry lifetime and defaults to 900 seconds. The
scheduled retention task uses the same `raw_responses` directory.

Only successful GET responses are eligible. The cache stores the original
response bytes and a small integrity-checked header. It does not cache
normalized data, adjusted or synthetic results, failed HTTP responses, or
Fuyao responses whose business envelope is not successful. A cache hit avoids
the HTTP call while preserving the provider's normal response contract.

Each entry is keyed by provider/source, query-free endpoint, normalized query
parameters, request headers and credentials, request context such as
`adjust_basis` and `as_of`, and a cache schema version. The filename contains
only a SHA-256 digest. Credential-like query values, authorization headers,
cookies, and URL userinfo are hashed before key construction; these values are
not written into filenames or metadata. Entries use atomic replacement and
are checked for TTL, body integrity, path type, and symlinks on read. Cache
errors safely miss and leave a successful upstream response usable.

To disable caching, set `RAW_RESPONSE_CACHE_ENABLED=false` (or unset it) and
restart the process. Existing files are not immediately deleted by disabling
the feature; the scheduled retention policy remains responsible for expiry.

The setting governs the contract-layer macro HTTP client and the Fuyao/THS
transport. The legacy ported `opendata_http` tree remains outside this cache
and must not be described as governed by it. No cache behavior implies that
the upstream response is current beyond the configured TTL.
