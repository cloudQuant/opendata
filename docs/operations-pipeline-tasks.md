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
| `limit` | `10,000` | 1–50,000 |
| `lookback_days` | `0` | 0–3,650 |
| `shard_size` | `200` | 1–1,000 |

When a task omits `limit`, its normalized payload preserves that omission and
the job uses the 10,000 default. Persisted pipeline tasks with an explicit
`"limit": 5000` continue to use 5,000; the default change does not rewrite
stored task parameters. Persisted pipeline tasks that omit `limit` now inherit
10,000 instead of the former 5,000 default. The built-in incremental cron
`p0-stock-daily-incremental` also inherits 10,000 while its payload omits the
field; the weekly `full_check` cron uses another executor and is unaffected.
This is an intentional runtime change. Before running affected work at the
higher default, inventory its source gate, execution scope, concurrency, and
resource budget; set an explicit 5,000 limit where the previous bound must be
retained. For the built-in incremental cron, a payload `limit` override works
only after the corresponding passthrough change has been merged and deployed.
It must be an exact integer from 1 through 50,000; earlier executor versions
ignore it.

The task-create API places `parameters` in a JSON request body, so its
`symbols` list is subject to the deployment's request-body limit rather than
the HTTP URL-length limit. The schema's 5,000-item bound does not establish that
every deployment accepts a body of that size; check the configured ingress and
application body limits. The manual `POST /api/v1/pipeline/run` endpoint uses
repeated `symbols` query parameters instead. Its 10,000-item schema bound can
exceed HTTPX's 65,536-character URL limit and lower proxy request-target
limits; see the transport note in
[`operations-universe-truncation-guard.md`](operations-universe-truncation-guard.md).
Large manual symbol sets need a POST-body API or bounded batches in follow-up
work.

Unknown parameter names are rejected. Put provider keys in the configured
provider credential store; task creation will reject credential fields and
never stores them in `scheduled_tasks.parameters`.

## Capacity acceptance status

The focused offline capacity-change run recorded 103 passed and 15 warnings.
`jobs.py` coverage was 438/451 combined (97.12%, zero excluded lines);
`api/pipeline.py` measured 54.44% and `api/schemas.py` 82.11% in the focused
selection. These API figures are file-level measurements, not a repository
A2 aggregate or static-gate result. The first attempt, with 101 passed and 2
failed, is preserved; HTTPX 0.28.1 rejected the large repeated-symbol query
URLs with `InvalidURL: URL component 'query' too long` before they reached the
application. The successful rerun builds synthetic ASGI scopes and does not
verify real client, proxy, or server URL acceptance.
See the detailed coverage and scope notes in
[`operations-universe-truncation-guard.md`](operations-universe-truncation-guard.md).

The successful-run evidence is archived at
[`universe-capacity-acceptance-20261029/README.md`](../var/reports/afi204/universe-capacity-acceptance-20261029/README.md);
all entries in the archive's `SHA256SUMS` manifest verified. The tests use
synthetic/local data, disable network access, and do not establish provider
capacity, production timeout headroom, or all-A-share coverage. The 50,000
schema maximum remains an input ceiling, not a workload guarantee.

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
    "limit": 10000,
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
existing retry count and retry policy apply to both executors. The 50,000
`limit` ceiling is only an input bound: it does not guarantee completion
within worker, provider, database, or configured timeout budgets. Scheduled
pipeline execution still uses the generic retry policy, which may retry a
deterministic DWD cap-overflow failure; classify scope and validation failures
as non-retryable in follow-up work, retaining bounded retries for transient
failures. Inventory the task's effective source gate and execution budget
before using a high limit.

## Inspect and export failures

The Executions page displays the failed-shard list returned by
`GET /api/v1/pipeline/failures`. **Export JSON** downloads the currently loaded
response as `pipeline-failures.json`, including an empty `failures` array when
there are no failures. A load or browser-download error is shown in the page.
The export is a point-in-time client download; the endpoint remains the source
of truth for a fresh list.
