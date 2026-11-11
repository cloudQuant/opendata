# AFI data-management → OpenData port (stage 0)

Owner ruling (2026-11-09): **port first, then delete from AFI.** This directory holds the
inventory that every later batch is executed and reviewed against.

## Why the order matters

`src/backend/app/services/akshare/` contains the scheduler and the subprocess worker that run
AFI's **1048 armed tasks**. Deleting the plane from AFI before OpenData can execute those tasks
removes the only control surface for a running collection system. The manifest therefore marks
eight modules `keep-in-afi`: they are not part of "delete today", they are the part that must
survive until OpenData owns scheduling.

## Classification (41 files / 18,850 lines) — includes the governance half

| class | files | lines | meaning |
| --- | --- | --- | --- |
| `adapt` | 8 | 3,018 | OpenData already owns an equivalent module; reconcile by hand |
| `rewrite` | 4 | 2,199 | mechanical copy after rewriting `app.*` imports |
| `copy` | 15 | 10,515 | frontend pages / API wrappers, copied as-is |
| `keep-in-afi` | 8 | 2,018 | scheduler, worker, fence, coordinator, clock — stay until cutover |

## Stages

0. **Inventory (this PR).** `scripts/port/port_afi_data_management.py` emits the manifest; dry-run
   by default, `--apply` performs the classified copy with import rewrites.
1. **Stage the copy outside the gate.** `--apply` writes to `port_staging/afi_data_management/**`.
   `PY_SELFDEV`/`PY_MYPY` cover `opendata scripts tests alembic …`, so an unadapted copy under
   `opendata/` would turn OpenData's own lint/typecheck red before stage 2 adapts it.
2. **Adapt module by module and move it in.** Each module is adapted to OpenData's
   models/session/service conventions, moved from `port_staging/` into `opendata/afi_port/**`,
   and only then wired to a router — one green increment at a time.
3. **Port the frontend** from `port_staging/…/frontend/` into `frontend/src/views/afi-data/` and
   `frontend/src/api/afi-data/`, pointed at the ported endpoints, after the backend it calls is in.
4. **Data hand-over.** Move/duplicate `ak_data_scripts`, `ak_scheduled_tasks`, `ak_task_executions`,
   `ak_data_interfaces`, `ak_data_tables` so OpenData reads them; verify counts against AFI.
5. **Cutover.** Enable OpenData's scheduler for the migrated task set, stop AFI's scheduler, and
   prove one natural cycle end-to-end in OpenData.
6. **Delete from AFI.** Only after stage 5 passes: remove `app/api/akshare`, `app/services/akshare`
   (except anything the worker still needs), `app/models/akshare_mgmt.py`, `views/data`,
   `api/akshare.ts`, `api/dataGovernance.ts`, the `config/data` routes and their tests.

## Not claimed

- `--apply` has been run **into `port_staging/` only**; nothing was written under `opendata/`,
  no router/model changed, and no AFI file was removed.
- The 8 `keep-in-afi` modules remain in AFI by design; "delete from AFI" is stage 6, not stage 0.

## Stage 2 adaptation surface (measured after the first `--apply`)

The staging copy (27 files, 824 KB) rewrites the six known AFI-internal prefixes. What remains
are **18 `app.*` imports across 6 modules** — this is the entire adaptation surface:

| AFI module | imports | maps onto |
| --- | --- | --- |
| `app.db.database` | 6 | `opendata/core/database.py` (session/engine) |
| `app.api.data.deps` | 5 | OpenData's FastAPI dependencies (`get_db`, auth) |
| `app.schemas.akshare_mgmt` | 4 | OpenData's pydantic schemas for scripts/tasks/tables |
| AFI's `app.db` warehouse-session module | 1 | OpenData's warehouse session factory |
| `app.config` | 1 | `opendata/core/config.py` settings |
| `app.models.data_governance` | 1 | OpenData's data-governance model (or drop) |

The staged copy compiles (`python -m compileall port_staging` → exit 0). It is **not** importable
until those six are mapped, which is stage 2's work — done one module at a time, each moved into
`opendata/afi_port/` only after it passes OpenData's lint/typecheck.

## Stage 2 schema decision (measured by `scripts/port/schema_diff.py`)

OpenData's models are **not** a different schema that needs replacing; they are the same schema
under different table names (`ak_*` vs bare) with **13 AFI-only columns and 1 OpenData-only
column** across the seven table pairs:

| AFI table | OpenData table | AFI-only columns | verdict |
| --- | --- | --- | --- |
| `ak_data_scripts` | `data_scripts` | `created_by`, `updated_by` | afi adds 2 |
| `ak_data_tables` | `data_tables` | `asset_type`, `dataset_storage_id`, `market`, `metadata_json`, `symbol_normalized`, `symbol_raw` | afi adds 6 |
| `ak_data_interfaces` | `data_interfaces` | `extra_config`, `updated_at` | afi adds 2 |
| `ak_interface_categories` | `interface_categories` | — | identical |
| `ak_interface_parameters` | `interface_parameters` | — | identical |
| `ak_scheduled_tasks` | `scheduled_tasks` | — | afi is a subset (OD adds `task_kind`) |
| `ak_task_executions` | `task_executions` | `airflow_dag_id`, `airflow_run_id`, `airflow_task_id` | afi adds 3 (legacy Airflow) |

**Consequence:** stage 2 does not move models. It either adds the 13 columns to OpenData's models
(or consciously drops the 3 legacy Airflow columns, leaving 10) and binds the ported services to
OpenData's models. The *rows* are stage 4 (copy `ak_*` data into the `data_*` tables, verified by
count), not stage 2.

## Scope correction: the governance half was under-counted

`ak_data_tables.dataset_storage_id` is a foreign key into `dg_dataset_storages`, and the config
centre's 数据治理 page calls `/data-governance/*`. That half of the data-management plane is **not**
in the 35-file manifest — it was visible only as one `app.models.data_governance` import. Measured:

| AFI piece | lines | note |
| --- | --- | --- |
| `app/models/data_governance.py` | 267 | eight `dg_*` tables (providers, datasets, storage_targets, dataset_storages, endpoints, endpoint_params, ingest_jobs, quality_rules) |
| `app/api/data/governance.py` | 85 | `/data-governance/*` routes |
| `app/services/data_connectors/` | 621 | `DataGovernanceService`, executor, registry |
| `app/api/data/deps.py` | 127 | the shared dependency module; pulls `app.api.deps`, `models.permission`, `models.user`, `schemas.auth` |

So the real port is **≈1,100 lines larger** than the manifest, and it pulls the auth/permission
models in as a dependency. `dataset_storage_id` cannot be added to OpenData's `data_tables` with its
FK until the `dg_*` tables exist there.

**Open decision for the owner:** either (a) include the governance plane in the port (it needs the
`dg_*` models + `data_connectors` + an auth mapping), or (b) drop `dataset_storage_id` and the
governance page from the migrated surface and record that as a deliberate reduction. The 9 remaining
simple columns (2 audit on scripts, 4 symbol/market/asset on tables, `metadata`, `extra_config`, and
`updated_at` on interfaces) are unblocked either way.

## Owner ruling 2026-11-09: the governance half is included

The governance plane is part of the port, not a reduction:

| AFI piece | lines | class |
| --- | --- | --- |
| `app/models/data_governance.py` | 267 | rewrite (8 `dg_*` tables) |
| `app/api/data/governance.py` | 85 | rewrite |
| `app/services/data_connectors/{__init__,executor,registry}.py` | 621 | rewrite |
| `app/api/data/deps.py` | 127 | **keep-in-afi** — it binds `models.permission`, `models.user`, `schemas.auth`; AFI keeps it, stage 2 maps the ported routes onto OpenData's dependencies instead |

Manifest after the ruling: **41 files / 18,850 lines** (`adapt` 8, `rewrite` 9, `copy` 15,
`keep-in-afi` 9). `dataset_storage_id` stays a real foreign key because `dg_dataset_storages` is now
in the port.

## Stage 2 progress

- **`opendata/models/data_governance.py` — moved in and green.** The eight `dg_*` tables now live in
  OpenData's model plane with `from opendata.core.database import Base`; `ruff check`, `ruff format
  --check`, `mypy` and `import opendata.models.data_governance` all pass. Porting it required three
  OpenData-specific shapes: module/class docstrings (D100/D101), SQLAlchemy `Mapper`/`Connection`
  annotations on the `before_insert` listener (ANN001/ANN401), and the repo's
  `__table_args__ = (  # type: ignore[assignment]` convention for tuple constraints.
- **Still required before this can run:** an Alembic revision creating the eight `dg_*` tables.
  OpenData's `make gate` does not compare models with migrations, but a deployment needs the DDL.
  The intended command, where an OpenData database is reachable, is
  `alembic revision --autogenerate -m "dg governance tables"` followed by a reviewed upgrade.

### Measured adaptation cost of the service layer

A trial move of the two dependency-free service modules (`table_pagination.py` 401 lines,
`param_bind_audit.py` 995 lines) into `opendata/afi_port/services/` produced **26 ruff findings**,
all docstring rules (D102 public methods, D105 magic methods, plus D100/D101). OpenData's lint is
stricter than AFI's, so a service module cannot be moved by copy: each needs its docstrings added
before it can leave `port_staging/`.

That trial was reverted, so `opendata/afi_port` stays green; the two modules remain staged. This is
the real per-module cost of stage 2 and it is mechanical, not semantic — which is why the plan moves
one module at a time rather than the whole 6k-line layer at once.

### Stage 2 progress: four service modules and the model binding

Moved and green (`ruff check`, `ruff format --check`, `mypy`, import):

| Module | Lines | Adaptation |
| --- | --- | --- |
| `services/table_pagination.py` | 401 | 5 docstrings, TC002/TC003 relocation, D107 |
| `services/param_bind_audit.py` | 995 | 15 findings: 9 D102, 1 D107, TC003, 3 SIM collapses with identical truth tables |
| `services/interface.py` | 142 | rebound to OpenData's own models; 1 `Any`→`object` |
| `services/interface_loader.py` | 156 | rebound to OpenData's own models |
| `services/data_connectors/` (3 files) | 675 | 33 findings; 3 justified `ANN401` noqa; SQL literal split (same value) |

**The model plane is not duplicated.** A first attempt moved `app/models/akshare_mgmt.py` into
`opendata/afi_port/models/`, which made `configure_mappers()` fail with `Multiple classes found for
path "DataInterface"`: seven ported class names (`DataScript`, `DataTable`, `DataInterface`,
`InterfaceCategory`, `InterfaceParameter`, `ScheduledTask`, `TaskExecution`) already exist on the
same declarative `Base`. That copy was removed; the ported services now import
`opendata.models.interface`, and the ten AFI-only columns were added to OpenData's models instead
(`data_scripts` 2, `data_tables` 6, `data_interfaces` 2; the three legacy `airflow_*` columns are
deliberately dropped). `opendata.models` now imports `data_governance` so the eight `dg_*` classes
join the same registry, and `configure_mappers()` passes.

Risks registered for stage 4 (row hand-over), not changed here:

- **Enum storage differs.** AFI's `ScriptFrequency`/`ParameterType` use `values_callable`, so MySQL
  holds values (`"daily"`); OpenData's models hold names (`"DAILY"`). Copying rows without
  translating them would fail the enum lookup. Changing OpenData's storage format needs its own
  migration.
- **`data_tables.last_update_status`** is `Enum("success","failed")` in OpenData but `String(20)` in
  AFI; any other stored status would fail on read.
- **`created_by`/`updated_by`** are AFI `String(36)` UUIDs pointing at `users.id`, while OpenData's
  `users.id` is `Integer`; they are added as indexed strings without a foreign key.
- **`data_tables.dataset_storage_id`** now FKs `dg_dataset_storages`, so the `dg_*` tables must live
  in the same database as `data_tables`.

### Stage 2: warehouse adapter moved; frontend measured and left staged

`services/data.py` (648 lines) is in `opendata/afi_port/services/` and green. It binds to
`opendata.core.database.data_engine` instead of AFI's lazy warehouse-engine helper, so the four
"warehouse not configured" branches are gone (they raised `RuntimeError`); every query path is
unchanged. The five dynamic-SQL sites carry `# noqa: S608` naming the `_quote_identifier`
validation that makes identifiers safe. One behavioural rename is required by the ORM binding: the
metadata lookup now reads `data_tables` instead of `ak_data_tables` — consistent with the stage-4
row hand-over. `pytest tests/test_api_interfaces_full.py` (15 tests) passes against the changed
model plane.

The frontend was copied into a trial tree and **reverted**: `vue-tsc` went from 0 errors on the
baseline to **123** errors, all in the 15 added files (10,515 lines). The causes are stack
differences, not typos:

- OpenData's axios instance types methods as `Promise<AxiosResponse<T>>`; AFI's client returns
  `Promise<T>` (25 unwrapping errors).
- No OpenData equivalents for `@/api/airflow`, `@/api/sync`, `@/api/dataTopics`,
  `@/views/data/utils`, several shared types in `@/types`, and three `DataSync` child components.
- element-plus 2.9.1 rejects the `@vue-generic` comments AFI's 2.14.5 accepted (9 errors).
- Auth shape differs (`is_admin` vs `authStore.isAdmin`).
- Missing model fields the pages read (e.g. `DataTable.market/symbol_*`, `DataScript.dependencies`).

Committing that copy would turn OpenData's frontend gate red, so it stays in `port_staging/` until
stage 3 adapts the client wrapper, types and the missing sub-modules.

## Stage 3 decision input: the ported routers are not duplicates

Measured (route path suffixes normalised by dropping the leading resource segment; prefixes differ
between the two routers, so treat this as a capability delta, not an exact path diff):

| Ported router | OpenData counterpart | Ported-only capabilities | OpenData-only capabilities |
| --- | --- | --- | --- |
| executions (6) | `api/executions.py` | `POST /{id}/retry` | `GET /failed`, `DELETE /` |
| interfaces (7) | `api/interfaces.py` | create/update/delete, `GET /categories`, `GET /{id}`, `POST /bootstrap` | bulk `PUT /`, `DELETE /` |
| scripts (10) | `api/scripts.py` | categories, stats, detail, run, toggle, scan, admin CRUD | `POST /`, `PUT /toggle`, `POST /scripts` |
| tasks (9) | `api/tasks.py` | detail, executions, run, toggle, templates, CRUD | `POST /trigger`, `POST /cancel` |
| tables (4) | `api/data.py` | detail, schema, paged data | execution result/status |
| governance (5) | none | the whole governance API | — |

So stage 3 cannot just register the ported routers at the same prefixes: the path shapes differ and
several capabilities overlap with different semantics. Two coherent options:

1. **Additive namespace (recommended given the migrate-first ruling):** register the ported routers
   under a distinct prefix (e.g. `/api/v1/afi-data`) so every function becomes reachable in OpenData
   without changing OpenData's existing contracts; dedupe later by folding callers onto one namespace.
2. **Merge into the existing routers:** add only the missing capabilities (retry, bootstrap, run,
   toggle, scan, detail, templates, categories, stats) to OpenData's routers and drop the rest. This
   touches OpenData's public contracts and needs its own API review.

Open governance API has no counterpart and ports as new routes in either option.

## Stage 4 readiness: read-only baseline (2026-11-10)

`scripts/port/handover_check.py` (tests: 3) compares each ak_* / data_* pair by row count without
writing. Baseline against the live AFI control DB and an unprovisioned target:
ak_data_scripts=1066, ak_data_tables=1061, ak_scheduled_tasks=1103, ak_task_executions=35454;
every target table `missing_table`. Stage 4 cannot proceed until the OpenData database is provisioned
and the owner approves the counted copy; the checker exits non-zero until all pairs match.

## Regression check (2026-11-10 04:14Z)

`pytest tests/test_api_{interfaces,scripts,tasks,executions}_full.py tests/test_interface_loader.py`
→ **66 passed** on the branch with the added columns and registered dg_* models, confirming the model
changes do not regress the existing OpenData API behaviour.

## Destructive port wave (2026-11-10/11): runtime, routers, migration, hand-over tooling

Under the owner's "port first, allow destructive early fixes" ruling, the whole AFI collection
runtime and control plane was moved into `opendata.afi_port` rather than rebuilt provider by
provider. Every new module is ruff/mypy clean (the only remaining mypy findings in the package are
the two pre-existing baseline pandas findings in the earlier-ported `services/data.py`).

### Runtime plane — `opendata/afi_port/runtime/` (was `keep-in-afi`, now ported)

`clock`, `write_fence`, `persistence` (AFI `db/persistence_boundaries`), `network_proxy`
(AFI `data_fetch/utils/akshare_network_proxy`), the killable child `script_worker` and its parent
supervisor `script_worker_runtime`, `execution`, the 1823-line `services/script.py`, the
APScheduler wrapper `scheduler`, `scheduler_service`, and the 601-line durable `run_coordinator`
all now import only OpenData models/session/settings. New models `TaskRunClaim` /
`SchedulerControl` (physical tables `task_run_claims`, `scheduler_control`; the OpenData naming
drops the `ak_` prefix) live in `opendata/models/run_claim.py`. Six scheduler/coordinator settings
joined `core/config.py`, including `akshare_script_root` which resolves under the OpenData repo
layout and still tolerates a legacy `app/...` value.

### Identity alignment (a deliberate destructive change)

AFI used string UUID owners; OpenData's `task_executions.operator_id` and
`scheduled_tasks.user_id` are integer FKs. The ported execution/scheduler/script-service
signatures now take `int | None`; the `data_scripts` audit columns stay `String(36)` without an FK
and receive `str(id)` only at the router boundary.

### Routers — `opendata/afi_port/api/`, mounted at `/api/v1/afi-data`

All six routers (scripts, tasks, executions, tables, interfaces, data-governance) are online in an
additive namespace that keeps AFI path suffixes, auth boundary (any user reads, admin writes) and
the bare `{items,total,page,page_size}` envelopes without changing any OpenData contract.
`afi_port/api/deps.py` adapts the two AFI auth dependencies onto OpenData's `get_current_user` /
`get_current_admin_user`. ANN401 is scoped off only for this ported router package (mirrors the
existing legacy-router convention); D-rules are fully satisfied. 43 routes registered.

### Frontend bridge

`frontend/src/api/afiData.ts` is a fully-typed wrapper over the shared axios instance covering all
six resource groups; the response interceptor passes the bare AFI envelopes through unchanged.
`vue-tsc` stays at zero errors. The 15 large AFI Vue pages remain a separate stage (their measured
vue-tsc delta is stack-level, not typo-level).

### Migration and stage-4 tooling

- Alembic `0024_run_coordinator_claims` creates the two coordination tables (utf8mb4/InnoDB);
  renders offline for both MySQL and SQLite; single revision head preserved.
- `scripts/port/handover_copy.py` performs the counted, enum-aware, idempotent row hand-over.
  Dry-run by default, `--apply` to write. It translates AFI's lower-case enum *values* to OpenData
  member *names* through the real enum classes (unknown values go NULL and are reported), drops
  the legacy `airflow_*` columns, fills `task_kind` and the NOT NULL `user_id` from an explicit
  `--default-user-id`, and projects only intersecting columns. The actual cross-DB copy still needs
  a provisioned target and owner approval; no live row was copied in this wave.

### Tests added (all green; no regression in the 66-case API suite)

`test_afi_port_routers.py` (7: namespace, 401/403 auth, envelope, admin boundary), the ported
19-case `test_afi_port_run_coordinator.py`, `test_afi_port_scheduler.py`,
`test_afi_port_scheduler_clock.py`, 8-case `test_afi_port_handover_copy.py`, 2-case
`test_afi_port_scheduler_lifecycle.py`, and the 4 frontend wire-contract tests. Combined targeted
run: **269 passed** across the port, API, alembic, task-lifecycle, contract and auth suites, with
zero failures.

### Stage-5 ownership switch and a circular-import fix

- `runtime/lifecycle.py` plus the `akshare_scheduler_enabled` setting (default `False`) make the
  ported scheduler fail-closed: OpenData never starts a second scheduler beside AFI until cutover
  flips `AKSHARE_SCHEDULER_ENABLED=true` from the composition root.
- The router is mounted at the composition root (`main.py`), not inside `opendata.api`'s package
  initialiser. Importing `opendata.afi_port.api.router` first otherwise re-entered a partially
  initialised `opendata.api` (the auth adapter reaches `opendata.api.dependencies`). `opendata.api`
  now stays native-modules-only and isort-green; both import entry orders resolve, and the plane is
  served under both `/api/v1/afi-data` and `/api/afi-data` (72 distinct paths).
- The `requests` import in `runtime/network_proxy.py` matches the baseline `data/http_client.py`
  bare import; the missing `types-requests` stub is the same ratchet-managed environment finding.

## Collection-script tree port (2026-11-11): the 1108-file B layer

The ported runtime resolves scripts as `opendata.data_fetch.scripts.*`, but OpenData previously
held only package skeletons and one reference script while AFI owned 1108 collection files. This
wave closes that gap so the ported scheduler/worker can actually drive the armed task set.

- `scripts/port/port_afi_collection_scripts.py` is the reproducible one-shot codemod: it copies
  AFI's `app/data_fetch` (the 1044 `AkshareToMysql` leaf scripts, their `common` helpers and the
  `core`/`configs`/`providers`/`utils` infrastructure) into `opendata/data_fetch` and rewrites
  every internal `app.*` reference (including the one string-form `importlib.import_module`) to
  the OpenData package.
- **Coexistence, not overwrite.** The OpenData-native `AkshareProvider` base (used by the A2
  scheduler and tests), `utils/retry.py`, `configs/__init__.py` and the package re-exports are
  protected. The colliding AFI provider lands as
  `providers/legacy_akshare_provider.LegacyAkshareProvider`, and the AFI production
  `stock_zh_a_hist` as `scripts/stocks/daily/stock_zh_a_hist_legacy.py`, next to the native
  reference script. Both planes import side by side.
- `configs/db_config.py` is rebound onto OpenData's `data_mysql_*` settings /
  `data_database_url`, keeping the `DB_CONFIG` mapping shape (`host/user/password/database/port`)
  that the 1044 scripts and the `mysql_base` cursor read.
- **Verification.** `compileall` is clean; **all 1108 script modules import with zero failures**;
  the B-layer gate `ruff --select E,F` is clean (no undefined-name/import errors — the only frozen
  debt is 1440 E501 line-length findings, mirroring the historical `_vendor` convention and pinned
  via per-file ignores). Scanner-style module resolution and the killable-worker entrypoint
  descriptor both resolve against the real tree; leaf counts reconcile (705 OD vs 704 AFI, the +1
  being the preserved legacy script).
- **Quality-gate boundary.** The byte-faithful port keeps stdlib `logging` %-style calls and
  predecessor identifiers (the real `akshare_data` warehouse, `akshare_network_proxy`).
  `loguru_render_check` and `check_brand` gain an explicit `PORTED_SKIP_PREFIXES` boundary that
  exempts the ported subtree while OpenData-native files remain fully gated; new tests pin that
  native files are never skipped.

## Stage-4 live read-only baseline (2026-11-11)

The live AFI control database is reachable with the source credentials and was measured read-only;
no row was written. `handover_check` and a `handover_copy` dry run agree on the pending hand-over:

| AFI source table | live rows | OpenData target | status |
| --- | ---: | --- | --- |
| ak_data_scripts | 1066 | data_scripts | target_missing |
| ak_data_tables | 1061 | data_tables | target_missing |
| ak_data_interfaces | 0 | data_interfaces | target_missing |
| ak_interface_categories | 0 | interface_categories | target_missing |
| ak_interface_parameters | 0 | interface_parameters | target_missing |
| ak_scheduled_tasks | 1103 | scheduled_tasks | target_missing |
| ak_task_executions | 37429 | task_executions | target_missing |
| ak_task_run_claims | 29126 | task_run_claims | target_missing |
| ak_scheduler_control | 1 | scheduler_control | target_missing |

Total pending: **69786 rows**. The 29126 claim rows match the iteration's documented terminal-claim
count, corroborating the measurement. The source DB user holds rights only on the three AFI
databases (`ai_for_investor`, `akshare_data`, `backtrader_web`) and lacks both `CREATE DATABASE`
and any grant on `opendata`/`opendata_data`, so the physical `--apply` copy is gated on a
deployment step outside this code change: provision the OpenData databases, grant the hand-over
user, then run `handover_copy.py --apply --default-user-id <int>` (the enum-aware, idempotent
copier). `handover_copy` now runs standalone and reports source counts even while the target is
unprovisioned; the read-only baseline is archived under `var/reports/afi204/` (git-ignored).

## Remaining cutover decisions (owner/deployment, not code)

1. Provision `opendata`/`opendata_data` and authorise the hand-over user, then run the counted copy.
2. Translate the `scheduled_tasks.user_id` string owners to OpenData integer users via
   `--default-user-id` or a reviewed mapping.
3. Flip `AKSHARE_SCHEDULER_ENABLED=true` only after stopping AFI's scheduler, so the two never
   double-run the 1048 armed tasks (the fail-closed lifecycle switch already exists).
4. Stage-6 deletion from AFI follows a verified natural-cycle run on OpenData.

## Runtime behaviour verification (2026-11-11): the ported worker actually runs

Importability alone does not prove the collection tree is schedulable, so the execution contract was
verified and then pinned with tests:

- **Entrypoint contract.** All 1043 `AkshareToMySql` leaf modules resolve a callable through the
  same `_resolve_module_callable` helper the runtime uses, with zero unresolved leaves. Both AFI
  shapes are covered: module-level `main()` dispatching to `fetch_data()`, and no-main pure classes
  exposing `fetch_data`/`run`. Every leaf exposes a `table_name`.
- **Worker descriptors.** Real production classes (including `crypto_hourly_market`, the documented
  incident run) build killable-worker descriptors carrying the rewritten `opendata.*` module path.
- **Real subprocess round trip.** A live `_execute_callable` spawns a distinct interpreter and
  returns the result across the process boundary.
- **New test files (99 runtime tests, all green):** `test_afi_port_collection_contract.py` (6:
  import + both entrypoint shapes + descriptors + parametrised production describability),
  `test_afi_port_write_fence.py` (9: attribution boundary, thread/context isolation, receipt
  merging), `test_afi_port_script_worker_unit.py` (40: in-process descriptor/recorder/fence), and
  `test_afi_port_killable_worker_process.py` (15: real interpreter subprocesses - process-group
  kill/reap on timeout and cancellation, cross-process error propagation, DataFrame round trips,
  parent-fence handoff, child-receipt merge). The three DB-session ledger E2E tests remain in AFI
  until the OpenData control database is provisioned.
- `test_afi_port_script_scanner.py` (5: the DB-free half of `scan_and_register_scripts` over the
  real tree, asserting every one of the 1100+ derived module paths imports, the metadata shape is
  correct, dunder inits are excluded, and extracted metadata resolves a runnable entrypoint). This
  locks the file-to-schedulable-task bridge.

The full iteration-204 port test set (`test_afi_port_*`, plus the brand/loguru gate tests) runs
green: **142 port tests pass** in a single collection, and the wider API/alembic/scope regression
run reported 276 passed with no new failures (the only red tests are pre-existing environment
issues: missing `bandit`/`types-PyYAML`/`types-requests` stubs, identical on the pre-session
baseline).

