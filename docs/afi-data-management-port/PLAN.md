# AFI data-management → OpenData port (stage 0)

Owner ruling (2026-11-09): **port first, then delete from AFI.** This directory holds the
inventory that every later batch is executed and reviewed against.

## Why the order matters

`src/backend/app/services/akshare/` contains the scheduler and the subprocess worker that run
AFI's **1048 armed tasks**. Deleting the plane from AFI before OpenData can execute those tasks
removes the only control surface for a running collection system. The manifest therefore marks
eight modules `keep-in-afi`: they are not part of "delete today", they are the part that must
survive until OpenData owns scheduling.

## Classification (35 files / 17,750 lines)

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
| `app.db.akshare_data_database` | 1 | OpenData's warehouse session factory |
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
