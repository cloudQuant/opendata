# HTTP Transport Governance

## Governed provider paths

The ECB, FRED, IMF, and OECD adapters send runtime GET requests through the
process-wide `GovernedHttpClient` in `opendata.data.http_client`. Their
`_http_get` functions remain injectable seams and continue returning the
provider-specific `(status, text)` pair. Existing provider error codes and
query-free URL attribution are preserved; transport failures are translated
to the provider's stable `*_HTTP_ERROR` code.

The default macro transport uses a per-host token bucket of 5 requests per
second with a burst of 10, at most 4 simultaneous requests, and up to 3
attempts for retryable GET failures. The client defaults to 10 second connect
and 30 second read timeouts. Macro adapters keep their 30 second request
timeout when no override is supplied, and pass a caller's timeout override
unchanged. Only GET is retried.

After a breaker cooldown expires, one request at a time may probe the host;
concurrent requests fail fast. A successful probe closes the breaker, while
any failed probe starts a fresh cooldown.

Requests sessions are reused per worker thread. The injected session seam is
synchronized because an injected session may be shared by concurrent tests or
callers. The rate bucket, circuit breaker, and concurrency gate are shared by
all four macro adapters through their common client.

## Fuyao / THS

The Fuyao client continues to own HTTPX transport, SDK envelope parsing,
response-size checks, stable `FuyaoError` categories, and `X-api-key` header
authentication. Internally created short-lived clients for the same host
share a 5 requests-per-second, burst-10 rate limiter and cooldown. Requests
from all Fuyao clients share a 4-request per-host concurrency gate. Waiting
more than 30 seconds for a slot fails with the `rate_limited` category. An
explicitly injected limiter still takes precedence; an injected HTTPX client
gets an isolated default rate limiter for deterministic test transports.

## Request events and credential safety

Each transport attempt emits a structured `governed_http_request` event with
source, query-free endpoint, parameter key summary, elapsed seconds, request
ID, attempt number, status, and failure category. Parameter values and
headers are omitted; secret-like parameter names are marked as redacted.
Request URLs in transport errors and events omit user information, query
strings, and fragments. Raw exception text and response bodies are not logged.

The registry checks credential presence at each `source="auto"` resolution
for THS (`FUYAO_API_KEY`, then application settings) and FRED (`FRED_API_KEY`,
then application settings). A missing key skips that verified source and
emits a credential-safe warning. Explicit-source resolution remains available
for catalog metadata and leaves credential failure to the provider when data
is fetched. Provider capability metadata, verification flags, authority order,
and market ambiguity checks remain unchanged.

## Boundary

This closes the governed runtime paths described above. It does not rewrite
the byte-faithful legacy `opendata_http` tree, patch global `requests` or
HTTPX functions, or claim that every legacy HTTP call is governed.
