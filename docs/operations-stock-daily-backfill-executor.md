# Stock-Daily Backfill Executor (Offline Control Plane)

This document describes `opendata.pipeline.stock_daily_backfill_executor` and
`scripts/ops/execute_stock_daily_backfill.py`. The executor turns one immutable
`StockDailyBackfillPlan` into a bounded, resumable sequence of ledger-backed
runs. It is a control-plane component, not a scheduler and not a data-quality
certificate.

## Execution-only contract

The planner requires its task to stay disabled, and the existing ordinary
dispatcher still rejects disabled jobs. The executor therefore requires a
separate reviewed job:

- `ReviewedJob.task` must equal the exact task used to build the plan,
  including `enabled=False` and `schedule=None`.
- `ReviewedJob.reviewed` must be `True`.
- `ReviewedJob.execution_only` must be `True`.
- `one_shot_only` and `execution_only` are mutually exclusive.
- The registry rejects execution-only jobs that are enabled, scheduled, or
  unreviewed.

The new `RunDispatcher.dispatch_backfill_execution` and
`RunService.submit_execution` paths accept only a reviewed execution-only job
and only a `backfill` request. Ordinary `submit`, `dispatch_backfill`,
scheduled dispatch, and the APScheduler bridge remain unchanged and continue to
reject the disabled task. An execution-only job is never auto-enabled and is
never placed on a schedule.

Execution also requires the explicit token
`execute-reviewed-stock-daily-backfill-v1`. The executor constructor rejects a
wrong token; `execute_once` rejects a missing token when `dry_run=False`.
Dry-run does not require the token.

## Plan verification

Before touching the ledger the executor calls
`verify_stock_daily_backfill_plan`, which independently:

- recomputes `plan_sha256` over the complete plan, including blockers;
- verifies every chunk belongs to its containing instrument result with
  contiguous indices and no duplicate request identity;
- recomputes the chunk's `bf-<sha256>` idempotency key from the pinned plan,
  lifecycle, alias, window, and query fields;
- verifies `request_payload_json` is canonical, its SHA-256 matches, and the
  request is a backfill request whose query, shard, and range match the chunk;
- verifies the plan's task digest and job identity match the executor's task.

The digest is an accidental-corruption and wiring check, not a signature. A
caller that can recompute it can still construct a self-consistent plan, so the
plan must remain inside a trusted review boundary.

## Dry-run default

`StockDailyBackfillExecutor(..., dry_run=True)` is the default. It reads the
ledger only. It does not dispatch, call the worker, write ledger rows, write
publication facts, or read a response source. It reports each chunk as
`missing`/`would_dispatch`, `queued`/`preview`, or an existing terminal state.
The CLI is also dry-run by default; `--execute` is the explicit acknowledgement.

## Ledger and progress semantics

Each chunk's run key is derived from the exact task and request via
`build_run_identity`. The planner's request key is stable, so rerunning the same
plan produces the same run keys. Progress lives only in the existing
`od_run_ledger`:

- `succeeded`: skipped; counted as `chunks_skipped`.
- `queued`: executed (or previewed in dry-run); a queued row from an interrupted
  invocation is reused, not replaced.
- `failed`: terminal; the invocation stops and the chunk is never re-dispatched
  under a new identity.
- `cancelled`: terminal; the invocation stops.
- `running` with a live lease: the invocation stops as leased by another worker.
- `running` with an expired or missing lease: the invocation stops as
  uncertain. It does not reclaim or retry the row.
- Any other ledger state: the invocation stops as invalid.

The executor calls `worker.run_once()` once per executed chunk and verifies that
the returned record is for the chunk's run key. A different claimed run stops
the invocation without promoting the chunk. Therefore the ledger and worker
must be dedicated to this executor; other queued work in the same ledger can
make an invocation stop.

`chunks_considered` counts inspected chunks. `chunks_executed` counts runs that
this invocation observed reach `succeeded` after execution. `chunks_skipped`
counts already-succeeded runs. `chunks_failed` counts terminal failed rows.
`chunks_remaining` is `chunks_total - chunks_considered`.

## Stop conditions and limits

The invocation stops on the first failed, cancelled, leased, uncertain, or
invalid chunk. `max_failures` is a hard cap fixed at `1`; other values are
rejected rather than silently loosening the fail-closed contract.
`max_chunks_per_invocation` is between 1 and 1000. Reaching it yields
`stop_reason=max_chunks_reached` with deterministic remaining work for the next
invocation.

No code path retries a terminal failure, creates a replacement identity for a
failed chunk, or reports a partial prefix as complete. The receipt always has
`execution_authorized=false` and `full_history_ready=false`.

## CLI

```bash
python scripts/ops/execute_stock_daily_backfill.py \
  --snapshot ./snapshot.json \
  --snapshot-sha256 <64-hex-digest> \
  --as-of 2026-10-27 \
  --max-window-days 3660 \
  --max-chunks 1 \
  --max-failures 1 \
  --database-path /absolute/local/ledger.sqlite
```

Dry-run is the default. It may omit the database, in which case an in-memory
ledger is used and no durable row is written. `--ledger-url` accepts only a
credential-free absolute local SQLite file URL; `--database-path` accepts only
an existing canonical regular SQLite file. Remote schemes, relative paths,
in-memory URLs for execution, query parameters, credentials, and symlinks are
rejected. The CLI never reads `.env`.

`--execute` additionally requires:

- a local SQLite database that already contains the run-ledger and publication
  tables (the CLI does not create schema in a caller-owned file);
- a private absolute `--artifact-root`;
- an explicit timezone-aware `--observed-at`;
- exactly one of `--raw-response` (one fixture, therefore one plan chunk) or
  `--raw-response-dir` containing
  `<provider-symbol>_<YYYYMMDD>_<YYYYMMDD>.json` fixtures.

These fixtures are local bytes. The CLI sends no provider request. The receipt
is compact canonical JSON on stdout; errors are compact JSON on stderr with a
stable code and no local paths or secrets. The CLI's local fixture mode uses
the generic `RunService` as the run-once worker; a production integration would
inject a `PersistentRunWorker` with a reviewed process-backed fetch handler.

## What is NOT proven

This component does not prove or authorize:

- a real provider response, source rights, or source authenticity;
- production MySQL behavior or multi-host lease timing;
- full universe or full instrument history;
- coverage reconciliation of head, interior, and tail gaps;
- the source's earliest queryable date, retention, or adjustment semantics;
- a natural scheduling cycle, automatic retries, or unattended operation;
- that the CLI's local fixtures represent real market data.

The executor executes the plan's candidate chunks; it does not filter them by
unrelated blockers and does not convert a blocker-free run into a completeness
claim. The executor is not a scheduler. It never registers with APScheduler,
never enables the existing disabled stock-daily task, and cannot authorize live
sends. A production rollout would require a separately reviewed process-backed
response source, production database/lease evidence, and explicit operational
authorization outside this offline control plane.
