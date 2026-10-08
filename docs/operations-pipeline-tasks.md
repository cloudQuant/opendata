# Scheduled pipeline tasks

Scheduled tasks use one of two fixed executors: `script` (the default for
existing requests) or `pipeline`. Pipeline tasks call the existing six-step
incremental runner. They do not accept Python callables, SQL, provider
credentials, engines, or session factories as task parameters.

## Current support

The currently built pipeline domain is `stock_daily`. The supported domain set
comes from `opendata.pipeline.jobs.SUPPORTED_DOMAINS`; it is not a promise that
every catalog domain has a pipeline builder. Sources are checked against the
registered provider capabilities for the selected domain. If `source` is
omitted, the domain mapping's default source is used.

Pipeline arguments are stored in the existing `parameters` JSON column:

| Field | Default | Bounds |
| --- | --- | --- |
| `domain` | `stock_daily` | Must have a six-step builder |
| `source` | Domain mapping default | Must be registered for the domain |
| `second_source` | omitted | Must be a different registered source |
| `symbols` | derived by the runner | 1–5,000 unique nonblank identifiers when supplied |
| `limit` | `5000` | 1–50,000 |
| `lookback_days` | `0` | 0–3,650 |
| `shard_size` | `200` | 1–1,000 |

Unknown parameter names are rejected. Put provider keys in the configured
provider credential store; task creation will reject credential fields and
never stores them in `scheduled_tasks.parameters`.

## Create a pipeline task

Use an administrator token to create one:

```http
POST /api/tasks/
Content-Type: application/json
Authorization: Bearer <administrator-token>
```

```json
{
  "name": "stock-daily-incremental",
  "task_kind": "pipeline",
  "script_id": null,
  "schedule_type": "cron",
  "schedule_expression": "0 2 * * *",
  "parameters": {
    "domain": "stock_daily",
    "source": "ths",
    "second_source": "akshare",
    "limit": 5000,
    "lookback_days": 1,
    "shard_size": 200
  }
}
```

`script_id` must be absent or `null` for a pipeline task. Script tasks retain
their previous shape and default to `task_kind: "script"`; they require an
active `script_id`. The two shapes cannot be combined, and an existing task
cannot be changed from one executor kind to the other.

## Permissions and lifecycle

Only an administrator can create, update, deactivate, delete, or manually
trigger a pipeline task. An owner may read their task; administrators retain
the existing cross-owner access.
The task owner must still be an active administrator when the scheduled
executor begins. The scheduler reloads that role from the database before
calling the runner; a revoked/inactive owner produces a failed execution record
without starting provider or warehouse work.

Startup reloads active tasks from `scheduled_tasks`. The normal execution
recording path tracks pipeline runs as well: `script_id` is `null`, successful
runs are `completed`, exceptions and partial shard failure are `failed`,
deadline expiry is `timeout`, and user cancellation is `cancelled`. The
existing retry count and retry policy apply to both executors.

## Inspect and export failures

The Executions page displays the failed-shard list returned by
`GET /api/v1/pipeline/failures`. **Export JSON** downloads the currently loaded
response as `pipeline-failures.json`, including an empty `failures` array when
there are no failures. A load or browser-download error is shown in the page.
The export is a point-in-time client download; the endpoint remains the source
of truth for a fresh list.
