# C30 轮：前端 e2e 面进 `make gate`——先量清「10 passed / 7 skipped」这一行绿说的是什么（task #38）

> 一句话：playwright 对「从未跑的用例」「断言只在 `if` 里跑」「断言恒真」给的是同一个 `✓` 或 `-`，
> 所以这一行绿不是判定。本轮先把 e2e 面从三个独立方向量清（守卫静态读 spec、独立量面重读一遍、
> playwright 自己的 `--list` 与真跑），修掉两条没有判决的用例，把 7 条缺口钉成**双向**清单，
> 然后才把 `frontend-e2e` 接成 gate 的第 12 项。判定面同时住在 `tests/`（18 例），因为下面 §8 证明
> **7 种把守卫弄烂的改法里 6 种 gate 自己看不见**。

数据来源全部在 `docs/evidence/C30/`（每份档案抬头都有运行前采集的时间、`branch/HEAD`、`git status --porcelain`、
解释器版本与完整命令；日志未裁剪，`GATE_EXIT=0` 读自 `gate.txt` 文件内部第 6371 行前一行）。

---

## 1. 为什么 e2e 的绿不能当判定

本轮前置状态（`playwright-run-before.txt`、`guard-before-fix.txt`，同一棵树）：

| 读数面 | 前置状态读数 |
| --- | --- |
| `npx playwright test` | `9 passed / 7 skipped`，`PLAYWRIGHT_RC=0`（绿） |
| 守卫 `frontend_e2e_plane.py` | 16 leaves：`ASSERTS 7` + `SKIPPED 7` + `GUARDED 1` + `VACUOUS 1`，`findings: 2`，`GUARD_RC=1` |

那两条 finding 是本轮要修的（§3）：

- `GUARDED: e2e/auth.spec.ts:22 … login with invalid credentials shows error — 1/1 assertion(s) sit inside an if with no else`
- `VACUOUS: e2e/auth.spec.ts:46 … 404 page for unknown routes — includes('nonexistent') is already true of the path the test navigated to`

一条用例的**全部**断言都在 `if (await …isVisible())` 里，等于「元素不出现就算过」；一条用例的断言
对自己 `goto` 过的路径恒真，等于没有断言。两种在 playwright 的 `✓` 里都不留痕迹。

## 2. 三路独立读数，外加对 C29 那句「e2e 3 文件 / 21 例」的口径更正

| 读数面 | 实现 | 本轮（修复后）读数 |
| --- | --- | --- |
| 判定面（唯一） | `scripts/quality/frontend_e2e_plane.py` | 17 leaves＝`ASSERTS 10` + `SKIPPED 7`，findings 0，`static vs runner: MATCH` |
| 独立测量面 | `docs/evidence/C30/e2e-plane-census.py`（**故意不 import 守卫**） | 同树 17 例，标签映射后与守卫逐条一致；`CENSUS_RC=0`（只表「量到了」，不表通过） |
| 运行时 | `npx playwright test --list` + 真跑 | `Total: 17 tests in 3 files`；`10 passed / 7 skipped (4.1s)` |

**决策反转（如实记）**：本轮原计划让 census 复用守卫的分类器以「消除重复实现」。做完对账后按 C29 已确立的
先例收回——**测量面可以独立，判定面必须唯一**：census 保留自己的实现，只加一张标签映射表
（`DECLARED-SKIPPED → SKIPPED`、`GUARDED-BY-IF → GUARDED`），并把它原先「读到 VACUOUS 就 `exit 1`」这个
**夹带的判定**改成 0/2（读得出=0，读不出=2）。

**口径更正（不改写历史行档，只在此登记）**：C29 行档写的「e2e 面 3 文件 **21 例**」来自普查的静态
call-site 计数，正则 `^\s*(?:it|test)(?:\.\w+)*\s*\(` 会把 `test.describe(` 一起算进去。实测分解：

| 文件 | 静态 call site | playwright 用例 | 差 |
| --- | --- | --- | --- |
| `auth.spec.ts` | 7 | 6 | 1 个 `describe` |
| `scripts.spec.ts` | 9 | 7 | 2 个（外层 + `Authenticated`） |
| `tasks.spec.ts` | 6 | 4 | 2 个（同上） |
| 合计 | **22**（修复前 21） | **17**（修复前 16） | 5 个容器 |

即：修复前 21＝16 例 + 5 个 `describe` 容器；本轮把两条坏用例改写为 3 条（净 +1 例）后是 17 例。
`118 = 79 + 18 + 21` / `120 = 99 + 21` 那两式对账的是**静态用例位**，不是 playwright 用例数，本轮把这点钉清；
守卫与 gate 用的是运行时口径（17）。

## 3. 两条坏判决用例的修法（先量再改，改的是断言对象）

**a. 「被拒绝的登录」——把不可观测的断言换成能红的两条。**
探针 `login-reaction-probe.txt`（临时 spec，跑完即删，不进判定面）实测：401 时
`request.ts` 走 `authStore.logout(); window.location.href = '/login'`，整页重载把 Element Plus 的
toast 销毁 ⇒ P1/P3 两条 `toast visible within 5s` 各 30s 超时（`PROBE_RC=1`）。所以
「用户读得到失败原因」在当前产品里**不是稳定断言**，不该写进用例。留下的两条都能红：

- 断言**提交出去的请求体**：`await expect.poll(() => attempts.at(-1)).toEqual({email, password})`；
- 断言**没留下会话**：`expect(async () => {…localStorage.getItem('auth')…accessToken).toBeNull()` 用 `toPass`。

顺带两条判据实现层的实测（不是产品事实）：`expect.poll(fn)` **不**重试 `fn` 抛出的错误（导航销毁上下文时
直接失败），重试要用 `expect(async () => {…}).toPass({timeout})`；`expect.async` 不存在。

**b. 恒真的 404 ——拆成两条各自能红的断言。**
原来 `goto('/nonexistent-page-xyz')` 之后断言 `url.includes('nonexistent')`。改成：

- 匿名访问未知路由：停在 `/login`，且 `searchParams.get('redirect') === '/nonexistent-page-xyz'`（守卫参数）；
- 登录态访问未知路由：渲染 404 视图（`heading '404'` + 正文文案）。登录态用 `seedSession()` 经
  `page.addInitScript` 预置 localStorage（store id `auth`，`paths: ['user','accessToken','refreshToken']`），
  而不是「为了测 404 再走一遍登录 UI」。

**c. 同一次探针还量到产品缺陷（本轮只登记，见 §7）**：`.el-message--error` strict-mode 命中 **3** 个节点
（`message_1/2/3`），即一次被拒登录叠了三条同源错误 toast。

## 4. 判定面：四条规则 + 退出码契约 + 规模交叉核对

`scripts/quality/frontend_e2e_plane.py`（ruff / format / mypy strict / bandit 全清，a2-check 已扫）：

| 规则 | 抓什么 | 为什么别的规则看不见 |
| --- | --- | --- |
| `GUARDED` | 断言全在（`N/N`）或部分在（`1/2`）无 `else` 的 `if` 里 | 元素消失时用例仍绿，运行时零痕迹 |
| `VACUOUS` | `includes('X')` 的 `X` 就在本用例自己 `goto` 的路径里 | 恒真断言与真断言在 `✓` 里同形 |
| `NO-ASSERTION` | 声明了 `test(...)` 而体内没有 `expect(` | 「跑了但什么都没判」也是 passed |
| `GAP-DRIFT` | 双向：新 `skip` 未登记 ⇒ 红；登记的缺口不再 skip 或已消失 ⇒ 红 | 注释会腐烂，清单不会（对不上就红） |

退出码：`0` 健全；`1` 有 finding 或规模对不上；`2` 无可读面（没有 spec / playwright 不答）——
**读不出来一律不是通过**（C28 口径）。规模核对用 `--list`（编译 spec、不起 webServer，约 1 秒）。

## 5. 造证伪集时抓到并修掉的、守卫自己的两处缺陷

这是 C27/C29 口径的正用：判据必须能被喂进它该红的形态。

- **缺陷 A｜一条永远不会触发的判据**。`GAP-DRIFT` 最初写的是「登记的缺口名在树里找不到 ⇒ 红」，
  而比较对象是**所有标题**的集合。一条被取消 `skip` 的缺口仍在树里、名字仍对 ⇒ 该规则**一次都不可能被触发**。
  改为与 `skipped` 集合比，并分两种措辞（「不再声明 skipped」/「没有这个用例」）。
  用例：`test_a_listed_gap_that_started_running_is_a_finding_too`。
- **缺陷 B｜误报健全用例**。`if (a) { expect(x) } else { expect(y) }` 每条路径都有断言，但 `} else {`
  不改变括号深度 ⇒ if 帧从不闭合 ⇒ 两条断言都记成 guarded ⇒ 给健全用例判 `GUARDED`。
  修法：读到 `else`（非 `else if`）时，若该 `if` 分支已有断言就**释放**这一对；`}` 与 `else {` 分写的写法
  同样释放（`continues_with_else()` 前瞻下一个非空行，判据不依赖作者怎么排版）。
  `else if` **明确不算释放**：链上可能没有兜底分支 ⇒ 宁可过报（过报=多审一条健全用例，漏报=用例假绿）。
- **记录在案的盲区两条**（`guard-falsification.txt` 里标 `BLIND`，写的是「现在读成什么」，不是认可）：
  ① `if (!visible) return` 之后的断言读成 `ASSERTS`（早退路径上一条断言都不跑）；
  ② 嵌在「会被 `else` 释放的分支」里的单行 `if (b) { expect(x) }` 也一起被释放。
  两者都属行级正则而非 AST 的固有代价，钉在档案与用例里，等下次真撞到再决定是否升级解析。

## 6. 缺口清单：7 例，具名、带原因、双向对账

> 状态指针（C32 追加，未改动本节任何读数）：下面 7 条已在 C32 按页补夹具后退空，`GAPS` 现为空集，
> 判定面改用「必覆盖页」下限来保持可红 —— 见 `docs/evidence/C32/README.md`。本节保留 C30 当时的原样测量。

`GAPS` 里每一条都带 2026-09-26 实测的原因（写在模块注释里，不是散在 spec 注释中）：

| 页面 | 用例 | 需要的前置 |
| --- | --- | --- |
| `/scripts` | scripts list shows data | `/api/v1/scripts/?page=…` 的响应夹具 |
| `/tables` | tables list shows data | `/api/v1/tables/` |
| `/tables/<name>` | table detail shows schema and preview | 详情端点 + preview 端点 |
| `/executions` | executions list shows history | 执行历史列表 |
| `/tasks` | tasks list page loads / can create a new task / can trigger task execution | `/api/v1/tasks/` + 写操作桩 |

为什么不在本轮补：实测 `/scripts`、`/tables`、详情、`/tasks` 各读**不同形状**的端点，一份通用
`{items, total}` 桩只能渲染 4 个页面里的 3 个 ⇒ 半实现＝对着 mock 断言，正是本轮在删的那类绿。
清单靠 `GAP-DRIFT` 双向钉住：新增 `skip` 必须登记，登记的缺口若不再 `skip` 或已消失就红。

**退路时不能带回来的形状**：`e2e-plane-census-after.txt` 逐叶记了 skip 里的断言形状，7 条缺口里有 **2 条**
自己就坐着 `if (await …isVisible())`——`scripts.spec.ts:33 table detail shows schema and preview`（`expects=1 guarded=1`）
与 `tasks.spec.ts:35 can trigger task execution`（同形）。它们今天因为 `SKIPPED` 优先而不被单独判 `GUARDED`，
所以 C31 取消 `test.skip` 时必须把这两条的断言搬到 `if` 外面：否则「缺口补掉了」与「§1 那两类坏判决又回来了」
是同一个 diff，而运行时读数两边都是 `✓`。

## 7. 本轮登记的产品缺陷（只登记，不在本轮改产品）

- **`src/mocks` 是死面**（探针 `msw-dead-plane-probe.txt`，`PROBE2_RC=0`）：`package.json` 装 `msw ^2.0.0`，
  `handlers.ts` 用 v0/v1 的 `rest` + `ctx` 写法 ⇒ 浏览器里 `pageerror: The requested module '/node_modules/.vite/deps/msw.js' does not provide an export named 'rest'`（每次页面加载都触发）；
  `mockServiceWorker.js` 取到 200 但 `navigator.serviceWorker` 的 `count=0 / controller=false`（没注册）；
  handler 路径写 `/api/auth/login`、`/api/scripts` 而 axios baseURL 是 `/api/v1` ⇒ 真实请求落到 vite 代理，
  `ECONNREFUSED` → 500。处置属 C31（修活或删掉），本轮不把它塞进 e2e 判定面，因为它不是断言。
- **一次被拒登录叠 3 条同源 toast**，三个发射点已代码级归因：`utils/request.ts:125`（拦截器 `default:` 分支）
  → `composables/useStoreAction.ts:41`（store 动作包装，`authStore.login` 经 `actionHelper.execute`）
  → `views/LoginView.vue:47`（视图 catch），实测 DOM 里 `message_1/2/3`。
- **401 分支硬 `window.location.href='/login'`** ⇒ 「告知用户失败原因」在产品层当前不可观测（§3a 的依据）。
- **撤回一条先前记录**：早前笔记写「`src/api/request.ts` 与 `src/utils/request.ts` 逐字重复」。本轮复核不成立：
  `grep -rln axios.create frontend/src` 只命中 1 个文件（`utils/request.ts`），`src/api/` 下没有 `request.ts`，
  `git log -- frontend/src/api/request.ts` 无历史。故这条**不进验收文档**，登记在此以免以讹传讹。

## 8. 证明它能红：14 个注入形态 + 7 种把守卫弄烂的改法

`guard-falsification.py`（`FALSIFY_RC=0`）：4 条规则各有「只有它能抓」的形态、4 个「必须不报」的形态、
2 个记录在案的盲区，外加 `main()` 的退出码契约三读（干净树 0 / 无可读面 2 / 植入 guarded 叶 1）；
四条规则若无一被触发则 `HARNESS_VACUOUS ⇒ exit 2`。

`guard-teeth-mutation.py`（`TEETH_RC=0`，`PROBLEMS: 0`）：把 7 种「让守卫不再报告」的改法逐个写进真模块，
两面各跑一次，再按字节还原并复核 sha256。

| 变异 | `tests/` 面 | `make frontend-e2e` 的守卫 |
| --- | --- | --- |
| `guarded_rule_dead` | 红（4 例失败） | **rc=0 绿** |
| `vacuity_rule_dead` | 红（1 例） | **rc=0 绿** |
| `no_assertion_rule_dead` | 红（1 例） | **rc=0 绿** |
| `unnamed_skip_rule_dead` | 红（1 例） | **rc=0 绿** |
| `listed_gap_rule_dead` | 红（2 例） | **rc=0 绿** |
| `runner_crosscheck_dead` | 红（1 例） | **rc=0 绿** |
| `classifier_dead` | 红（16 例） | rc=2（`NOTHING-TO-JUDGE`，唯一被 gate 看见的） |

`gate-blind mutations: 6/7`——与 C29 同一结论：**接进 gate 只解决「没人跑它」，不解决「它自己烂掉」**，
所以判定规则同时钉在 `tests/test_frontend_e2e_guard.py`（18 例，全部离线、含 shipped-tree 两端）。
一处期望被反向证伪：`guarded_rule_dead` 的 must_fail 里列了
`test_an_assertion_that_only_lives_in_the_else_branch_is_a_finding`，而那条用例只断言计数与 verdict，
没断言 `judge()` 真出了 finding ⇒ 变异逃过它。修的是**用例**（补 `judge([leaf], NO_GAPS)[0].startswith("GUARDED:")`），
不是把期望调小。

## 9. 明确拒绝的「让它过去」

- 不用 `--fail-on-skipped` 一类现成开关代替判定：它会把 7 条**有名**缺口与「无判决」压成同一种红，
  判据就分不开两类（本轮未实测该 flag 的组合行为，故按设计选择不采用，不声称实测不可用）。
- 不把 7 例缺口半实现成对着通用桩的断言（§6），也不把 `test.skip` 改成 `test.fixme`/注释掉来消掉红。
- 不把守卫的判定逻辑挪进 `docs/evidence/`：判定面唯一在 `scripts/quality/`，evidence 只做量与证伪。
- 不为了让 gate 绿而把 `frontend-e2e` 摘出去（那是本轮起点），也不给它加 `|| true`。
- 不依赖后端或数仓：登录/接口响应一律在浏览器层 `page.route` 拦截；数仓全程只读；未打印任何密钥。
- 不把 `test-results/`、`playwright-report/` 留进版本库：`frontend/.gitignore` 补两条，
  曾入库的 `frontend/test-results/.last-run.json` 用 `git rm --cached` 摘出（不删磁盘文件）。
- 没有放宽任何断言：`toPass({timeout: 10_000})` 等的是整页重载稳定，不是把断言改成恒真。

## 10. 证据清单（`docs/evidence/C30/`）

| 档案 | 内容 | 为什么存在 |
| --- | --- | --- |
| `playwright-run-before.txt` / `guard-before-fix.txt` | 前置状态：9 passed / 7 skipped 与 2 条 finding（rc=1） | 证明守卫在修树之前就能红 |
| `e2e-plane-census-before.txt` / `-after.txt` | 独立量面两次读数 + 标签映射表 | 与判定面交叉核对 |
| `login-reaction-probe.txt` + `.spec.txt` | 401/400 屏幕上到底留下什么（超时、3 条 toast） | §3a 与 §7 的依据；探针跑完即删 |
| `msw-dead-plane-probe.txt` + `.spec.txt` | msw 死面三条实测 | §7 的依据 |
| `guard-after-fix.txt` | 修复后守卫：17 leaves / findings 0 / MATCH | 接线前的最终读数 |
| `guard-falsification.py` + `.txt` | 14 形态 + 退出码契约 | 证明规则能红且不冤枉 |
| `guard-teeth-mutation.py` + `.txt` | 7 变异 × 两面 | 证明 gate 自己看不见 6/7 |
| `playwright-run-after.txt` | `make frontend-e2e` 一次完整跑（守卫 + 10 passed / 7 skipped） | 新 gate 项的独立留档 |
| `gate.txt` | 完整 `make gate`，6,371 行未裁剪，`GATE_EXIT=0` | 第 12 项接入后门禁全绿 |

## 11. 门禁与测试数字，以及遗留

`make gate` **GATE_EXIT=0**（`docs/evidence/C30/gate.txt`，6,371 行完整未裁剪；gate 项 11 → **12**，
`frontend-e2e` 列末位）：

- 后端 **2,773 → 2,791 passed**（+18 恰为 `tests/test_frontend_e2e_guard.py` 的用例数），`6 skipped` 与 C29 逐字同批；
  覆盖率 **86.72% → 86.75%**（阈值 84%）
- a2-check **283 → 288 文件**（+5＝守卫模块 + `tests/` 用例 + 三份证据脚本），ruff/format/mypy/bandit 全 ok
- public API **473 → 486**，docstring/annotation 双 100%
- quality-ratchet 债务未增：`ruff_selfdev 277→274`、`ruff_ported 2145→2144`、`direct_http_ported 1045→1044`（均改进），
  `mypy_selfdev 21`、`bandit_selfdev 4` 持平
- 前端单元面 13 files / 99 tests、collection 13/13；eslint 46 problems（**0 errors**, 46 warnings）；
  typecheck 无输出
- e2e：守卫 17 leaves（10 ASSERTS / 7 SKIPPED）/ findings 0 / `static vs runner: MATCH`；playwright 10 passed / 7 skipped

遗留（明确不做或做不了）：

1. 7 条 e2e 缺口需 per-endpoint 夹具（§6）——C31 的主体，连同 msw 死面的处置一起决定。
2. 重复 toast（3 条）与 401 硬跳转是产品侧缺陷，本轮只登记。
3. 守卫是行级正则不是 AST，两个盲区（§5）记录在案。
4. 前端覆盖率阈值仍未接进 gate（`frontend-test-cov` 仍是独立 make 项）。
5. `make frontend-e2e` 需一次性 `npx playwright install chromium`，CI 里要作为前置步骤；
   本轮的绿是在本机已有 browser 的前提下取的。
6. `e2e` 用例仍带 msw 的 `pageerror` 噪声（§7），不影响判定，但会在 e2e 报告里持续出现。
