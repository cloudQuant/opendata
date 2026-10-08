# Single-source DWD backfill

`scripts/ops/backfill_dwd.py` rebuilds a bounded date window in one DWD table
from one explicitly selected, registered ODS source. It uses the source's
declared field mapping, the regular single-source DWD merge for trace columns,
and the DWD key-upsert writer. It does not call a provider, create a table,
choose `source=auto`, or promote a capability into automatic routing.

Apply the warehouse migration first. Revision `0006_dwd_daily_legs` creates
`dwd_index_daily`, `dwd_futures_daily`, and `dwd_option_daily`; the DWD table
for other domains must already exist. ODS tables are also required and are
never created by this command.

Every target and both inclusive date bounds are required. For example:

```bash
/Users/yunjinqi/opt/anaconda3/bin/conda run --no-capture-output -n base python \
  scripts/ops/backfill_dwd.py \
  --domain index_daily --source ths --start 2026-01-01 --end 2026-01-31
```

Use the same explicit form for another registered mapped leg, such as
`--domain stock_daily --source ths` or `--domain stock_action --source ths`.
`--page-size` sets the maximum source rows read into one merge/write page and
defaults to 2000.

Before writing, the command scans the requested ODS window in stable source
primary-key order and stores normalized keys in a temporary, disk-backed
uniqueness index. This catches aliases that normalize to the same DWD key even
when other source keys sort between them. Preflight and execution share one
repeatable-read source transaction, so the rows written are the same snapshot
that passed collision checks. This is material for corporate actions: ODS can
distinguish two same-symbol, same-ex-date events with `event_key`, while the
current DWD contract key is `(symbol, ex_date)`. The command reports that
collision and writes no rows for the selected window rather than choosing
either event.

The `_as_of` trace date is the UTC date of the backfill run. It records when
this historical ODS snapshot was materialized into DWD; it does not reuse the
earlier end date of the requested data window as though the DWD row had been
available then.

Successful writes are idempotent key upserts. If a database or writer error
occurs after earlier pages committed, the failure output includes the committed
row count and says that rerunning the identical selection is safe. Review the
JSON counters and collision list before deciding whether the source rows need
correction.
