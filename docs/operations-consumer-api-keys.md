# Consumer API key rate limits

Each consumer API key's configured `rate_limit` is enforced as a rolling
60-second allowance at the shared `CurrentPrincipal` dependency. Requests using
the same key share one budget across data endpoints. A blocked request returns
HTTP `429` with a positive `Retry-After` value in seconds. JWT-authenticated
requests continue using the existing authentication and IP rate-limit paths.

With `REDIS_URL` unset, a concurrency-safe in-process limiter keeps bounded,
pruned state; run one API worker for this mode so the configured limit is
process-wide. If local state reaches its bound before expired entries can be
pruned, API-key requests fail closed with HTTP `503`. When `REDIS_URL` is set,
an atomic Redis script shares the window across workers. If Redis cannot enforce
a configured shared limit, API-key requests fail closed with HTTP `503`; the
service does not fall back to per-process state. Test mode does not bypass
consumer-key limits.

Limiter state and diagnostic logs use the numeric API-key record ID. They do
not store or log the presented API-key secret or its hash.

Provider model metadata uses the same `CurrentPrincipal` dependency and shares
the consumer-key allowance. `GET /api/v1/providers/models` filters entries by
domain scope; `GET /api/v1/providers/{source}/models/{model}/schema` checks that
scope before constructing the query schema. Both require authentication.
Unknown registered identities return `404`, malformed or `auto` identities
return `400`, and a known model outside the credential's scope returns `403`.
Metadata reads do not execute provider fetchers.
