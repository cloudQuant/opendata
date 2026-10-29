# DWD-derived universe truncation guard

The incremental pipeline derives its implicit symbol list from the distinct
codes currently present in `dwd_<domain>`. The guard in
[`derive_universe`](../opendata/pipeline/jobs.py#L404) prevents that observed
list from being silently cut off when it exceeds the configured cap. It does
not build or validate a complete exchange-wide instrument universe.

## Selection and cap behavior

For `symbols=None` or an empty symbol list, the runner validates `limit` before
reading the derived universe. `None` uses
[`DEFAULT_SYMBOL_LIMIT = 10_000`](../opendata/pipeline/jobs.py#L93). A supplied
limit must be an exact Python `int`; booleans, floats, and other types are
rejected. Integer values at or below zero retain the existing compatibility
behavior and are treated as a cap of one. A cap at `sys.maxsize` or above is
rejected before querying because the `cap + 1` probe cannot be represented
safely. The public direct-run route and scheduled-task schema require a limit
of at least one, so the nonpositive compatibility behavior applies to direct
runner/helper calls.

The warehouse query reads at most `cap + 1` distinct, ordered codes. That extra
code is a truncation witness: the runner accepts a result of exactly `cap`
codes and raises `ValueError` when it sees the extra code. This happens before
instrument-catalog filtering and before provider fetching or pipeline
construction. The guard never continues with the first `cap` rows of an
oversized implicit result. The query uses a bound `LIMIT` parameter; the table
name comes from the registered domain mapping.

A non-empty explicit `symbols` list takes the caller-supplied path and bypasses
the implicit DWD cap and warehouse derivation. Direct calls to
`derive_universe` still do not bound the size or content of an explicit list;
the public API boundaries enforce their own limits. The manual REST route
accepts at most 10,000 explicit symbols, while the scheduled-task schema
accepts at most 5,000. These list-size limits are independent of `limit`, which
only caps an implicit DWD-derived universe. Use explicit symbols for a
deliberate bounded repair or batch, not as evidence of broad-market coverage.

One existing error-handling detail affects diagnosis: `symbol_universe`
currently converts warehouse query exceptions to an empty list, and the
incremental runner then raises an empty-universe error. Follow-up work should
preserve the original query failure separately from a genuinely empty DWD
table, so operators can distinguish a warehouse outage from missing data.

## Current job, API, and scheduled-task behavior

The `stock_daily` incremental job uses a default derived-universe limit of
10,000 when the caller omits `limit` or passes `None`. A caller-supplied limit
still controls the DWD-derived selection, up to the bound accepted by that
entry point.

The asynchronous `POST /api/v1/pipeline/run` route defaults `limit` to 200 and
accepts values from 1 through 10,000. It accepts at most 10,000 explicit
`symbols`. This is a count of repeated query values, not a per-symbol
character-length limit or a guarantee that the request can be transported.
The observed local HTTPX 0.28.1 parser defines `MAX_URL_LENGTH = 65,536` and
checks the URL string length before sending. The repository declares
`httpx>=0.27.2` without an exact runtime pin, and a deployed HTTPX version may
differ; reverse proxies and servers can impose lower request-target/request
line limits. A query containing 10,000 six-character ASCII symbols repeated as
`symbols=...` is roughly 150,000 URL characters before other parameters, so it
exceeds the observed HTTPX limit. The 10,000-item schema bound therefore does
not mean a 10,000-item manual request will reach the server. The API currently
returns an accepted run before deriving the universe, so an over-cap DWD result
is reported on the run-status endpoint after acceptance. Explicit-list count
validation occurs at the request boundary. See
[`pipeline_run`](../opendata/api/pipeline.py#L90).

The manual route still carries explicit symbols in the URL query. Follow-up
work should move this list to a POST JSON body with an explicit body-size limit,
or introduce bounded client batches with clear progress and idempotent resume
semantics. Verify the deployed client's URL limit and ingress request-target
limit before relying on large query lists.

Scheduled pipeline task parameters use a default derived-universe limit of
10,000 when `limit` is omitted and allow explicit values from 1 through
50,000; their explicit `symbols` field remains limited to 5,000 entries. The
normalized task payload preserves omission, so the job supplies the 10,000
default at execution time. Existing persisted pipeline tasks with an
explicit `limit: 5000` retain that value. Persisted pipeline tasks that omit
`limit` now run with the new 10,000 default instead of the previous 5,000
default; the built-in incremental cron `p0-stock-daily-incremental` also uses
10,000 while its payload omits `limit`. This is an intentional runtime change.
The separate weekly `full_check` cron uses another executor and is unaffected.
Before running affected tasks at the higher default, inventory their execution
scope, source gate, concurrency, and resource budget; set an explicit 5,000
value where the old bound should be retained. For the built-in incremental
cron, a payload `limit` override is effective only after the corresponding
passthrough code is merged and deployed. It must be an exact integer from 1
through 50,000; earlier executor versions ignore that field. The default change does not rewrite stored task JSON.
All limits are request bounds, not evidence that the warehouse has a complete universe
or that any source can safely process that many symbols. See
[`PipelineTaskParameters`](../opendata/api/schemas.py#L224) and
[`operations-pipeline-tasks.md`](operations-pipeline-tasks.md).

Scheduled executions use the existing generic retry policy when
`retry_on_failure` is enabled. A cap-overflow error is deterministic for an
unchanged DWD snapshot and task configuration; retrying it after the existing
backoff will not make the universe fit and can create repeated failed
executions. The direct run route does not use that scheduler retry loop. The
current retry behavior can still retry deterministic scope failures; classify
validation and cap-overflow errors as non-retryable in follow-up work while
preserving bounded retries for transient warehouse or provider failures.

## Capacity and remaining limits

The focused offline capacity acceptance passed, as detailed below, but the
10,000 and 50,000 values remain request ceilings; they do not establish that a
provider or deployment can finish a run of that size. No large-scale provider
or deployment-capacity claim has been established. Before enabling
10,000-symbol scheduled runs broadly, measure a bounded synthetic run through
the actual job path and record peak memory, queue depth, database load, provider
request count, payload sizes, and end-to-end duration. Inventory each task's source
gate and effective execution budget; the 50,000 ceiling may exceed available
worker, provider, or timeout budgets.

Set explicit worker concurrency and per-provider rate limits; enforce request
timeouts, run deadlines, cancellation, and bounded response sizes. A provider
that requires pagination needs an explicit cursor and snapshot-consistency
contract, checkpointing, idempotent page writes, and bounded page retries.
Raising `limit` does not implement safe pagination or source-side rate
limiting. Record run-level requested, selected, fetched, rejected, and
published counts so operators can identify partial work. Validation and scope
errors should not enter transient retry loops; retry only classified
transient failures with a bounded attempt count and backoff, and make retries
safe against duplicate publication.

The DWD-derived universe remains only the set of codes currently observed in
the relevant DWD table. A successful run at 10,000 or 50,000 does not establish
all-A-share coverage. Full coverage still requires a separately verified
security master, exchange and instrument-class scope, effective dates, and
per-instrument head/interior/tail history reconciliation. Keep any unverified
or unavailable instruments visible as gaps instead of treating the DWD count
as the expected universe.

## Acceptance evidence and limits

The earlier DWD truncation-guard run is historical baseline evidence for the
5,000-symbol default: 78 passed, 15 warnings, and 97.05% combined
statement-plus-branch coverage for `jobs.py` with zero excluded lines. Its
archive is `var/reports/afi204/universe-truncation-acceptance-20261029/`; that
run does not cover the later capacity changes.

The focused capacity-change run recorded 103 passed and 15 warnings. For
`opendata/pipeline/jobs.py`, coverage was 354/363 statements plus 84/88
branches, 438/451 combined (97.12%), with zero excluded lines. The measured
API modules were `opendata/api/pipeline.py` at 49/90 combined (54.44%) and
`opendata/api/schemas.py` at 303/369 combined (82.11%), also with zero
excluded lines. Those API figures are measurements from the focused selection;
they are not a repository-wide A2 coverage or static-gate result. The isolated
run used a frozen candidate and an offline synthetic test scope; it did not
measure real provider throughput or production-scale execution.

The first capacity-test attempt recorded 101 passed and 2 failed because HTTPX
0.28.1 rejected the 10,000- and 10,001-symbol query URLs before they reached the
application (`InvalidURL: URL component 'query' too long`). That failure is
preserved. The successful revised run constructs synthetic ASGI scopes for the
large query-count boundary, so it verifies FastAPI-side parsing and validation
without proving a real client's URL transport or acceptance by a proxy or
server. This is why the 10,000-item REST schema bound must not be treated as a
transport guarantee.

The capacity evidence is archived at
[`universe-capacity-acceptance-20261029/README.md`](../var/reports/afi204/universe-capacity-acceptance-20261029/README.md).
The archive includes the first failed attempt, the successful run, and static
check records; every entry in its `SHA256SUMS` manifest verified. The frozen
run used synthetic warehouse and provider fixtures, local test data, and
disabled network access; it did not read production data, call a live provider,
or use production credentials. Its Python and pytest tool versions differ
from the repository pins, as recorded in the archived run report.

The 10,000 and 50,000 values are request ceilings, not a provider-capacity
benchmark. They do not establish that a deployment can process a run of that
size within its memory, database, concurrency, or timeout budgets. The
transport, pagination, rate-limit, retry, and per-task resource limits above
remain operational constraints. Neither the focused tests nor a successful run
proves that DWD contains the full A-share universe. Verified security-master
scope and per-instrument history reconciliation remain separate requirements.

The guard describes only the codes already observed in the queried DWD table
at execution time. It cannot detect securities missing from that table,
establish all-A-share or Beijing Stock Exchange coverage, reconstruct
instrument membership as of a historical date, or prove a security's actual
listing/delisting boundary. It also cannot prove per-symbol historical head,
interior, or tail completeness. A verified security master with effective
dates, source lineage, and per-instrument history reconciliation remain
separate requirements for the full all-A-share collection objective.
