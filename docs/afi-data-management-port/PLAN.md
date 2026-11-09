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
