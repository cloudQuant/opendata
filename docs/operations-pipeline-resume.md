# Pipeline watermarks, resume, and metadata

The incremental `stock_daily` runner derives its ODS fetch windows from the
selected source tables. It reads the latest mapped raw date for each requested
symbol, capped at the requested window end. DWD, caches, and symbols outside the
requested universe do not determine that watermark. Missing ODS tables are
treated as a first load; mapping errors and actual query failures stop the run.

For a symbol with no ODS watermark, the runner keeps its normal incremental
window. A stale watermark starts at the next calendar date, then applies the
configured lookback overlap. A symbol already covered through the requested end
has no fetch window when `lookback_days` is zero. A requested future end is
never extended. Window selection is saved per pipeline, source, and symbol so
that a restarted run uses the same plan even if some shard writes already
advanced the ODS watermark.

For two-source runs, cross-check and merge readers receive the union of both
sources' effective windows for each requested symbol. This lets the comparison
read an already-caught-up source over the same range as the source being
caught up. Reader queries bind dates and symbols as parameters and process the
requested symbol list in bounded chunks.

## Resume behavior

The control database migration `0006` adds
`pipeline_step_checkpoints` and `pipeline_symbol_windows`. The stable
`pipeline_id` includes the domain, source, date window, business key, sorted
symbol universe, shard size, request variant, and configured hooks. A
different requested universe or run variant therefore has a separate
checkpoint series.

Shard progress remains in `pipeline_progress`. A shard with one or more fetch
failures is marked failed even when its good rows were written; retry fetches
the shard again and the warehouse writer applies its existing idempotent ODS
upsert. Before any new ODS write, completed downstream hook checkpoints are
invalidated, so an ODS commit followed by a process interruption cannot leave
old cross-check, merge, notification, or metadata state marked reusable.

After shard processing, hooks run in this order: cross-check, DWD merge,
notification, and metadata refresh. With resume enabled, completed hooks are
skipped and the first pending or failed hook runs again. A run with every shard
already complete still reconstructs its affected keys from the scoped ODS
window and can finish an incomplete hook. `resume=False` clears saved hook and
window plans and re-runs all shards for that pipeline identity.

Notification delivery is at least once. If a process exits after a message is
sent but before its completed checkpoint commits, a resumed run can send it
again; consumers should use their existing deduplication key where available.

## Metadata refresh and cost

The final metadata hook upserts one `data_tables` row for every selected ODS
source table and the domain's DWD table. It records exact `COUNT(*)`, `MIN`
date, and `MAX` date aggregates, the current refresh timestamp, and success
status. The aggregates execute in the warehouse and return one summary row per
table; they do not materialize the data in application memory. The aggregate
queries run in a worker thread so a large warehouse scan does not block the
async event loop. The full-table aggregates scan the selected ODS and DWD
tables, so their warehouse query cost grows with table size. All metadata changes commit together in one main
database transaction. A missing required warehouse table or query error fails
the metadata step instead of publishing invented values.

Scheduler `task_executions` records remain the task-level execution history.
`pipeline_progress` is shard state, `pipeline_step_checkpoints` is hook state,
and `data_tables` is warehouse table metadata; these records have separate
purposes.
