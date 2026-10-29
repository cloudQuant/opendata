# DWD-derived universe truncation guard

The incremental pipeline derives its implicit symbol list from the distinct
codes currently present in `dwd_<domain>`. The guard in
[`derive_universe`](../opendata/pipeline/jobs.py#L404) prevents that observed
list from being silently cut off when it exceeds the configured cap. It does
not build or validate a complete exchange-wide instrument universe.

## Selection and cap behavior

For `symbols=None` or an empty symbol list, the runner validates `limit` before
reading the derived universe. `None` uses
[`DEFAULT_SYMBOL_LIMIT = 5000`](../opendata/pipeline/jobs.py#L93). A supplied
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
the implicit DWD cap and warehouse derivation. The API schemas impose their
own request bounds where declared; direct calls to `derive_universe` do not
bound the size or content of an explicit list. In particular, the direct-run
route declares repeatable `symbols` parameters without a list-length bound,
and its `limit` does not constrain that explicit list. The scheduled-task
schema does cap explicit lists at 5,000. Add a route-level symbol-count bound
before accepting background work so a large explicit list cannot evade the
derived-universe limit. Use explicit symbols for a deliberate repair or
reviewed bounded batch, not as a way to claim broad-market coverage.

One existing error-handling detail affects diagnosis: `symbol_universe`
currently converts warehouse query exceptions to an empty list, and the
incremental runner then raises an empty-universe error. Follow-up work should
preserve the original query failure separately from a genuinely empty DWD
table, so operators can distinguish a warehouse outage from missing data.

## API and scheduled-task effects

The asynchronous `POST /api/v1/pipeline/run` route defaults `limit` to 200 and
accepts values from 1 through 5,000. It returns an accepted run before the
background job derives the universe, so an over-cap DWD result is reported on
the run-status endpoint after acceptance. See
[`pipeline_run`](../opendata/api/pipeline.py#L90).

Scheduled pipeline task parameters default to 5,000 and allow `limit` values
through 50,000; their explicit `symbols` field is limited to 5,000 entries.
This means the scheduling API currently exposes a wider derived-universe cap
than the direct run route. These are request bounds, not evidence that the
warehouse has a complete universe or that any source can safely process that
many symbols. See
[`PipelineTaskParameters`](../opendata/api/schemas.py#L224) and
[`operations-pipeline-tasks.md`](operations-pipeline-tasks.md).

Scheduled executions use the existing generic retry policy when
`retry_on_failure` is enabled. A cap-overflow error is deterministic for an
unchanged DWD snapshot and task configuration; retrying it after the existing
backoff will not make the universe fit and can create repeated failed
executions. The direct run route does not use that scheduler retry loop. The
failure classification and retry policy should be updated so validation and
scope errors stop for operator action while genuinely transient warehouse or
provider errors retain an appropriate retry policy. The route defaults,
scheduled-task maximum, per-source capacity, execution budget, and any future
large-universe batching contract should also be reconciled before raising
limits.

## Acceptance evidence and limits

The focused guard cases are in
[`tests/test_pipeline_universe_truncation.py`](../tests/test_pipeline_universe_truncation.py).
The offline acceptance archive is
[`var/reports/afi204/universe-truncation-acceptance-20261029/README.md`](../var/reports/afi204/universe-truncation-acceptance-20261029/README.md).
Its successful run records 78 passed tests and 15 warnings. For
`opendata/pipeline/jobs.py`, coverage is 348/357 statements plus 80/84 branches,
428/441 combined (97.05%), with zero excluded lines. The archive also retains
the successful pytest output, coverage JSON, frozen test selection, runtime
and dependency hashes, and two earlier failed-run records; only the successful
run counts as acceptance. The selected tests ran in an isolated offline
environment with synthetic warehouse/provider fixtures and test-local SQLite
where existing tests require it. They did not read production data, connect to
a production database, or call a live market-data provider.

The guard describes only the codes already observed in the queried DWD table
at execution time. It cannot detect securities missing from that table,
establish all-A-share or Beijing Stock Exchange coverage, reconstruct
instrument membership as of a historical date, or prove a security's actual
listing/delisting boundary. It also cannot prove per-symbol historical head,
interior, or tail completeness. A verified security master with effective
dates, source lineage, and per-instrument history reconciliation remain
separate requirements for the full all-A-share collection objective.
