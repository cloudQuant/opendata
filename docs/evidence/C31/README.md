# C31 轮：前端「读得到」这件事的三个面——信封双解包、类型门禁恒绿、46 条声明里 4 条后端从未挂载（task #39）

> 一句话：本轮把前端三处「看起来在工作、实际上不可能给出判决」的面各自量清再修——
> ① `frontend/src/api/*.ts` 有 5 条读路把信封拆了两层（`res.data?.data`：`catalog.ts` 2、`data.ts` 2、`tables.ts` 1），拿到的永远是 `undefined`；
> ② `make frontend-typecheck` 跑的是 `vue-tsc --noEmit` 且没有 `-p/-b`，读的是 solution 文件（`files: []`），
> **零个文件被检查**，植入 `const n: number = 'x'` 依旧绿；③ 前端声明的 46 个端点里有 4 个后端从未挂载
> （`PATCH /tasks/{id}/toggle`、`GET /tasks/{id}/executions`、`POST /executions/{id}/retry`、`PUT /users/{id}/role`），
> 前 3 条后面各站着绿的单测，第 4 条站在「设置角色」按钮上。三件事各自的修法不同，但同一口径：
> **判定面必须能红**，所以①的修法是把读路改对并让单测先红后绿，②③的修法是**把规则钉进 `tests/`**。

数据来源全部在 `docs/evidence/C31/`（每份档案抬头都有运行前采集的时间、`branch/HEAD`、`git status --porcelain`、
解释器版本与完整命令；日志未裁剪；`GATE_EXIT=0` 读自 `gate.txt` 文件内部）。

---

## 1. 三件事的入口读数与修法

| 面 | 入口读数（修复前） | 独立测量面 | 判定面（修复后住的地方） |
| --- | --- | --- | --- |
| 信封读路 | `catalog.ts`（2）／`data.ts`（2）／`tables.ts`（1）共 5 处 `res.data?.data`，而 `make frontend-test` 以 13 文件／99 例全绿；真机三页各喂一份真实信封应答，marker 出现次数 `RENDERED: NO` ×3 | `envelope-shape-probe.txt`、`envelope-unwrap-probe.txt`（真实后端 `APIResponse` 形状 + 各 api 模块解包次数） | `frontend/src/__tests__/api/envelope.test.ts`（5 例，走真实 `@/utils/request`，只 stub `defaults.adapter`）＋ 重写的 `utils/request.test.ts`（2 例 → 5 例）＋ 重写的 `catalog.test.ts` |
| 类型检查 | `vue-tsc --noEmit` → 读 0 文件、rc=0；`e2e/` 4 个 spec 不在任何 project 里 | `typecheck-vacuity.txt`（植入类型错误仍绿的完整过程） | `tests/test_frontend_typecheck_guard.py`（11 例，6 类 reason）＋ `Makefile` 的 `-b --force` ＋ `frontend/tsconfig.e2e.json` |
| 端点契约 | 46 条声明里 4 条 404；没有任何一面把「声明」和「注册」比过 | `endpoint-contract-census.py` → `endpoint-contract-census.txt`（段列表比对 + `app.routes` 挂载后绝对路径） | `tests/test_frontend_endpoint_contract.py`（14 例，4 类 reason + 3 道下限） |

三处修复都是**前端侧**的：后端 surface 本轮零改动（`git diff` 里 `opendata/` 为空），没有新增路由、没有生产写入。

## 2. 信封双解包：先红后绿是唯一可接受的证明顺序

axios 响应拦截器已经把信封剥掉（`return res.data ?? res`），所以 api 模块里的 `res.data?.data` 是**第二层**解包。
`read-path-red-green.txt` 的五段：

| 段 | 内容 | 读数 |
| --- | --- | --- |
| A | 5 条 `envelope.test.ts` 用例对着 **HEAD 版**的 api 模块跑 | `5 failed`，`RED_RC=1` |
| B | 16 例（含重写后的 `utils/request.test.ts`、`catalog.test.ts`）对 HEAD 跑 | `10 failed / 6 passed` |
| C | 同 16 例对修复后的树跑 | `16 passed`，`RC=0` |
| D | 可达性裁决：`query()` 里 `?? emptyDataPage()` 那条兜底分支 | 用 `sed -n '248,264p' opendata/api/data_query.py` + `grep -rn "success=False" opendata/api/*.py` 证明后端**不会**用 `success=True, data=null` 回答 ⇒ 该断言不可达，**删断言**而不是给死码加固 |
| E | 还原校验 | `RESTORED_OK` ×3，`grep -c "response.data?.data"` = 0／0／0 |

原计划里还有第 6 条断言（`data` 缺失时回落到 `emptyDataPage()`），D 段量出它不可达后删掉了——
**按 C27 的口径，一条永远不会失败的断言与没有断言等价**，留着它只会让这一面看起来比实际大。

## 3. 类型门禁面：三种烂法都要能红

`typecheck-plane-restored.txt` 记修复本身，`typecheck-guard-falsifiable.txt` 记「守卫对着**真实** Makefile/tsconfig
能红」，而不是只认植入字典：

| 段 | 改坏 | 期望并实测到的 reason |
| --- | --- | --- |
| A | 未改坏基线 | `RC=0` |
| B | 命令退回 `--noEmit`（无 `-p/-b`） | `NO_PROJECT_SELECTED`，`RC=1` |
| C | `-b` 但去掉 `--force` | `STALE_BUILD_INFO`（`*.tsbuildinfo` 会让这次构建变成 no-op），`RC=1` |
| D | 从 solution 里去掉 `tsconfig.e2e.json` 引用 | `UNREFERENCED_PROJECT`，`RC=1` |
| E | 还原 | `RC=0` |

`frontend/.gitignore` 补 `*.tsbuildinfo`（`-b` 会写构建态，跑完门禁不该留脏文件），守卫里也钉了这条。

### 3.1 把面接上之后第一手读到的 55 例（`typecheck-vacuity.txt` 计数、`typecheck-plane-restored.txt` §E 逐类处置）

`vue-tsc -b` 第一次真读这棵树时报 55 例——这些不是本轮新引入的债，而是**门禁恒绿期间攒下的**。
逐类处置里没有一处是「改期望值让它过去」，但有五类值得单独看：

| 类别（例数） | 处置 | 为什么值得记 |
| --- | --- | --- |
| `src/mocks/*`（11） | 整面删除 | msw 2.x 里没有 `rest`/`setupWorker` 导出，`public/` 下也没有 `mockServiceWorker.js` ⇒ DEV 每次启动在 `main.ts` 的 async IIFE 里抛未捕获 rejection |
| `RegisterView`（2） | 补 `FormRules` 注解 ＋ **补 `password_confirm`** | 后端 `RegisterRequest` 必填该字段 ⇒ **注册按钮点了必然 422**，这是类型面接上后揪出的真机产品缺陷 |
| 分页不刷新（2） | 模板补 `@current-change` | `handlePageChange` 声明了却从未被模板接线（TS6133）⇒ **翻页只改 `currentPage`，列表不重载** |
| `TableDetailView`（1） | 删掉那一格统计 | `schema.data_size` 端点从未返回；AC-15 的 legacy 面只读 ⇒ 不加字段来让断言成立 |
| `SettingsView`（2） | 直接用负载字段 | `mainRes.data` 在**已解包**的负载上不存在 ⇒ 与 §2 是同一症状的另一种表现 |

另两处口径需要显式记账：`PAGINATION` 去掉了外层 `as const`（literal `20` 让 `handleSizeChange` 无法回写，
`TablesView`/`ExecutionsView` 各一例），以及 `useAsync` 返回时把 ref 又包了一层 `shallowRef`（`.value` 取到的是内层 ref）
——前者是**放宽类型而不是放宽判据**，代价由 §2/§4 的用例面兜住；后者是真缺陷修复。

`logger.error(…, e)`（`e` 是 `unknown`）5 处改走 `logger.apiError(endpoint, e)`；`i18n` 的 `createI18n` 第二泛形位
原本是 `DateTimeFormats` 却被当成 locale 传了 ⇒ 去掉误用泛形并双向补齐 8 个漂移 key。

## 4. 端点契约面：46 条 → 43 条，MISSING 4 → 0

`endpoint-contract-census.py` 是**故意不 import 守卫**的第二套实现：它按段列表逐段比对，并且拿 `app.routes` 的
**挂载后绝对路径**（把 axios `baseURL=/api/v1` 这一跳算进判定），而守卫比的是 `api_router` 的相对模板 + 正则。两栏读数：

```
HEAD（修复前）  8 模块 / 46 条 → MISSING=4  VERB=0
工作区（修复后） 8 模块 / 43 条 → MISSING=0  VERB=0
```

那 4 条的处理（都是「删调用方」而不是「补后端」，因为补后端等于新增对外 surface）：

| 假端点 | 调用方处置 |
| --- | --- |
| `PATCH /tasks/{id}/toggle` | 删 `tasksApi.toggle`、`store.toggleTask` 及其 1 例单测（`should toggle task active status`，`tasks.test.ts` 13→12）；`TasksView` 的开关本来走的就是已注册的 `PUT /tasks/{id}`（`TasksView.vue:110`），不受影响 |
| `GET /tasks/{id}/executions` | 删 `tasksApi.getExecutions`（无调用方） |
| `POST /executions/{id}/retry` | 删 `dataApi.retry`、`store.retryExecution` 与 `ExecutionsView` 的「重试」列（`vue-tsc -b` 在删 api 后以 TS2339 揪出了第三个消费方 `stores/executions.ts`——zsh 未加引号的 `--include=*.vue` 让第一次 grep 漏了 `.ts` 消费方，这类漏读记在 C29 的口径里） |
| `PUT /users/{id}/role` | 改指已注册的 `PUT /users/{id}`（`UserUpdateRequest` 收 `role`，部分 body 只改角色），管理员改角色这一动作从「必 404」变成真能做事 |

另外把 `tasksApi.list` 的过滤参数名从 `enabled` 改成后端的 `is_active`：FastAPI 丢弃未知 query 参数，
拼错的过滤名**永远不会过滤**，而它原来的单测断言的正是 `enabled` 这个不可能生效的名字。

## 5. 守卫自己也被改坏过一次：M4 第一遍逃掉

`endpoint-guard-falsifiable.txt` 三遍：

| 遍次 | M1 假端点回填 | M2 新增 `http.get()` 模块 | M3 `baseURL` 漂成 `/api/v0` | M4 `scripts.ts` 的 9 条里 5 条改成 `path:` |
| --- | --- | --- | --- | --- |
| 第一遍（12 例） | `NOT_REGISTERED` 红 | `NO_CALLS_READ` 红 | 前缀挂载例红 | **绿——没抓到** |
| 第二、三遍（14 例） | 红 | 红 | 红 | `PLANE_SHRANK: api/scripts.ts 只读到 4 条声明，下限 9` 红 |

M4 逃掉的原因可量化：43−5=38 仍过全局下限 36，而逐模块为 0 的 `NO_CALLS_READ` 看不见「一部分隐形」。
补的是 `MIN_CALLS_PER_FILE`（8 个模块的下限按本轮修复后的读数钉，删端点必须同时改这里），
census 侧同步补 `MIN_PER_FILE`。第三遍只是把两份脚本改写到 A2 标准（ruff 全规则 + bandit 0 findings）后复跑，行为等价。

同一档案里还记了本量面自己两次假绿：`git show <path>`（少了 `HEAD:`）安静输出 0 字节 → 现在空读直接 raise；
不拼 `baseURL` 则 43 条全 MISSING → 前缀这一跳被钉进判定面。

## 6. 被否决的两项扩面（都以测量收口）

| 候选 | 否决依据 | 档案 |
| --- | --- | --- |
| 用 `msw` 替掉手写的 adapter stub | `npm uninstall msw` 产 43 增 14 删的 lock 抖动、`node_modules/msw` 仍在（`@vitest/mocker` 的 optional peer），代价是依赖面变大而判定不变 | `msw-dep-removal-rejected.txt`（含档尾对丢失 `UNINSTALL_RC` 那行的如实补记） |
| 给不可达的 `?? emptyDataPage()` 兜底加断言 | 见 §2 D 段：后端不可能给出该形状 | `read-path-red-green.txt` §D |

`frontend/src/mocks/{browser,node,handlers}.ts` 一并删除：msw 装不上且从没被任何用例 import，是彻底的死面。

## 7. 本轮读出来、没有修的（登记给 C32 或用户决策）

1. **`return res.data ?? res` 这个拦截器形状本身是陷阱**：带信封时给内层、不带信封时给整个 body，
   调用方只能按端点逐个猜。本轮只把读路改对（判定面钉住），统一契约需要后端把所有端点都包进信封或前端改显式解包，属接口决策。
   `/settings/database`、`/tables/{id}/schema`、`PUT /users/{id}` 三处今天就真的不带信封（`utils/request.test.ts` 钉了 passthrough 形状）。
2. **三个 store 没有消费方**：`useTaskStore`（125 行，12 例单测）、`useExecutionStore`（149 行，**0 例单测**）、
   `useTableStore`（134 行，**0 例单测**）——`frontend/src/views/**` 只 import `useAuthStore`／`useThemeStore`，
   页面直接调 api 模块。要么让页面改走 store，要么删掉这三份并行状态面；这是产品/架构决策，本轮不动。
3. **叠 Toast + 把后端 `detail`／axios message 原文回显给用户**（`page-request-census.txt` 里的读数量）。
4. **`LoginView.vue:42` 的 `route.query.redirect as string`**：`?redirect=` 可以是任意字符串，登录后直接跳，是开放重定向面。
5. **`vitest.config.ts` 的 coverage 阈值 60/60/50/60 与实际读数的关系**没有独立量面（本轮未动）。

## 8. 门禁与下一步

`make gate` 12 项全跑（brand / zero-dep / js-points / a2 / ratchet / public-api / test-cov / frontend-lint /
frontend-typecheck / frontend-collection / frontend-test / frontend-e2e），完整未裁剪日志
`gate.txt` 共 **6445 行**，`GATE_EXIT=0` 与 `===== gate: PASSED =====` 都读自文件内部（后台任务通知的 exit 不作数）。
运行前采集：`captured_at: 2026-09-26T08:18:59+02:00`、`branch/HEAD: dev 857d79f`、`Python 3.13.5 / node v25.1.0 / npm 11.6.2`、
以及运行前的全量 `git status --porcelain`。

| 面 | 本跑读数 |
| --- | --- |
| test-cov | `2816 passed, 6 skipped in 60.01s`，`Required test coverage of 84% reached. Total coverage: 86.75%`（TOTAL 10942 stmts） |
| 6 条 skip 归因 | 逐条读自 `gate.txt` 内的 `SKIPPED tests/...` 行：全部是 `tests/test_port_fidelity.py` 的 em 通道保真例（`stock_daily_raw`／`stock_daily_qfq`／`index_daily_em`／`fund_etf_daily_em`／`qfq_factor_steps[em]`／`qfq_synthesis[em]`），em 渠道仍拒 ⇒ 与本轮改动无关，不是新增静默 |
| frontend-typecheck | `cd frontend && npx vue-tsc -b --force`（§1 第 2 行的修法）， rc=0；产物 `tsconfig.{app,e2e,node}.tsbuildinfo` 由 `frontend/.gitignore` 的 `*.tsbuildinfo` 兜住，跑后 `git status --porcelain` 52 项里无 tsbuildinfo／htmlcov／.coverage |
| frontend-collection | on disk (src) 14 文件 == collected 14 文件 |
| frontend-test | **14 文件／106 例全绿**（§2 的 16 例是本轮针对读路那三个文件的子集） |
| frontend-e2e | 量面 `named gaps: 7`、`findings: 0`；playwright `17 tests`：`10 passed / 7 skipped`，7 条 skip 与 `GAPS` 一一对应（`scripts.spec.ts:23/28/33/44` + `tasks.spec.ts:19/26/35`），即本段下一步 C32 的待办，不是新增静默 |
| a2-check | `A2 files: 292` → `ok ruff check` / `ok ruff format --check` / `ok mypy` / `ok bandit` → `OK: A2 files meet the full A2 standard`，本轮两份 `docs/evidence/C31/*.py` 与两份 `tests/test_frontend_*.py` 都在集合内；改到 A2 标准之前的那次独立 `make a2-check` 曾因 B603/B101 与 format 红，过程记在 §5 末段 |
| quality-ratchet | `OK: quality debt did not increase.`，且 `ruff_selfdev 274（快照 277）`、`ruff_ported 2144（2145）`、`direct_http_ported 1044（1045）` 三条改善，`mypy_selfdev 21`、`bandit_selfdev 4` 持平（快照未 `--update`，只降不升） |
| public-api-quality | 486 个公共可调用对象，docstring 与注解覆盖各 100.0% |

下一步 **C32**（已钉成任务）：C30 留下的 7 条 e2e 缺口按页补夹具退掉、把 2 条
`if (await …isVisible())` 里的断言移出 `if`（`e2e/scripts.spec.ts:33`、`e2e/tasks.spec.ts:35`），
补 warehouse 分层／failures 面板／catalog 的页面覆盖，`scripts/quality/frontend_e2e_plane.py` 的 `GAPS`
与 `tests/test_frontend_e2e_guard.py` 同步改。§7 第 1、2 条留给用户决策。
