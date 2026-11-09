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
1. **Port the plane beside OpenData's own.** Target namespace `opendata/afi_port/**`, so OpenData's
   existing `api/` and `models/` are not overwritten by an unverified copy.
2. **Adapt the eight `adapt` modules** to OpenData's models/session/service conventions, and add
   tests for each. Nothing is wired into OpenData's routers yet.
3. **Port the frontend** into `frontend/src/views/afi-data/` and `frontend/src/api/afi-data/`,
   pointed at the ported endpoints.
4. **Data hand-over.** Move/duplicate `ak_data_scripts`, `ak_scheduled_tasks`, `ak_task_executions`,
   `ak_data_interfaces`, `ak_data_tables` so OpenData reads them; verify counts against AFI.
5. **Cutover.** Enable OpenData's scheduler for the migrated task set, stop AFI's scheduler, and
   prove one natural cycle end-to-end in OpenData.
6. **Delete from AFI.** Only after stage 5 passes: remove `app/api/akshare`, `app/services/akshare`
   (except anything the worker still needs), `app/models/akshare_mgmt.py`, `views/data`,
   `api/akshare.ts`, `api/dataGovernance.ts`, the `config/data` routes and their tests.

## Not claimed

- Stage 0 moves no code and changes no runtime. `--apply` has not been run.
- The 8 `keep-in-afi` modules remain in AFI by design; "delete from AFI" is stage 6, not stage 0.
