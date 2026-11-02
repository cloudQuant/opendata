/**
 * Whether this build fronts a composed reviewed runtime.
 *
 * The four `/reviewed/*` operations are served by their own ASGI app: the
 * router in `opendata/api/reviewed_runs.py` (`APIRouter(prefix="/reviewed")`)
 * is mounted only by `create_reviewed_app` (`opendata/reviewed_app.py`), and no
 * entrypoint calls that factory — `Dockerfile`, `opendata.service` and
 * `start_app.sh` all serve `opendata.main:app`, and nginx proxies `/api/` to
 * that single process. `deploy/afi204-reviewed/README.md` records what is still
 * open before it can run: the composition root has to build the task catalog,
 * engine, dispatcher, worker SPI and role plan, and the deployment must declare
 * the sink IDs it owns and stop legacy writers for those sinks first.
 *
 * So the reviewed runtime is absent from every deployment this repo ships, and
 * the honest default is "not enabled": an unset `VITE_REVIEWED_RUNTIME_ENABLED`,
 * or any value other than exactly `true`, issues no `/reviewed` request and
 * renders the not-enabled state instead. The comparison is deliberately
 * fail-closed rather than truthy so a stray value cannot light the page up.
 *
 * Cutover is one build-time variable plus the process that hosts the runtime;
 * the transport in `./api.ts` and the store's enabled path stay unchanged.
 */

export const REVIEWED_RUNTIME_NOT_ENABLED = 'reviewed_runtime_not_enabled'

export const REVIEWED_RUNTIME_NOT_ENABLED_TITLE = '审核运行时未装配'

export const REVIEWED_RUNTIME_NOT_ENABLED_MESSAGE =
  '当前部署没有装配审核运行时（reviewed runtime 未启用），本页不会调用任何 /reviewed 接口，' +
  '也不能提交、查询或取消运行。运行时上线由部署方负责：先在代码里静态装配 create_reviewed_app 的' +
  '任务目录、引擎、分发器、worker SPI 与角色计划，声明该应用拥有的 sink 并停止这些 sink 上的旧写入者，' +
  '再由构建设置 VITE_REVIEWED_RUNTIME_ENABLED=true 打开本页。'

export function isReviewedRuntimeEnabled(): boolean {
  return import.meta.env.VITE_REVIEWED_RUNTIME_ENABLED === 'true'
}
