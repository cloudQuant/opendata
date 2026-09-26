# C32 轮：把 e2e 面的 7 条具名缺口按页补夹具退掉，并给判定面换一条能红的规则（task #40）

> 版本：本轮收口 v5.3 验收回填（AC-11 / AC-13 / AC-17 / AC-18）
> 日期：2026-09-26
> 前置：C30 量清了这个面「一行绿」说的是什么的四种烂形状（`test.skip`／恒真断言／`if` 守卫断言／
> 只断言自己导航过的路径），并把 7 条缺口钉成带原因的双向清单；C31 让 `frontend-typecheck` 真读树、
> 删掉 4 条假端点。本轮是 C30 §6 那句「退路时不能带回来的形状」的兑现：7 条缺口全部退掉，
> 2 条坐在 `if` 里的断言搬到 `if` 外面，且退掉之后判定面仍然必须能红。

---

## 0. 一页结论

| 项 | 读数 |
| --- | --- |
| `make gate` | `GATE_EXIT=0`，`docs/evidence/C32/gate.txt` **6,421 行完整未裁剪**，读数取自文件内部（头部含两行出处更正：把第 11 项步骤名由 `test` 更正为 `frontend-test`，未触碰任何读数） |
| 判定面 `scripts/quality/frontend_e2e_plane.py` | `LEAVES 18` / `ASSERTS 18` / `named gaps: 0` / `pages covered: /data /executions /scripts /tables /tasks` / `findings: 0` / `static vs runner: MATCH` |
| 运行时 playwright | `18 tests in 3 files`（`--list`）⇒ 真跑 **`18 passed (6.6s)`**（C30/C31 是 `10 passed / 7 skipped`） |
| 新增面 | `frontend/e2e/fixtures.ts`（13 条端点桩，逐条按后端真实形状量出来抄的，不是编的） |
| 产品缺陷 | 修掉 4 条真机缺陷（成功率双乘、状态词表用后端从不写的 `success`、读不存在的 `rows_processed`、「每小时」提交后端必 422）＋ 1 条本轮尾巴（状态词不在词表时整表崩） |
| 形状对账 | 新增 `docs/evidence/C32/payload_shape_census.py`：74 条路由 × 13 条夹具 ⇒ 三条计数器全 0，且三条都能红（§5） |
| 判据未放宽 | 未改任何一条期望值去迁就实现；`GAPS` 退空之后**新增**了一条更严的规则（§4），而不是把判定面关掉 |

---

## 1. 先量，再写夹具（三份测量档）

C30 §6 说过为什么不能直接补：`/scripts`、`/tables`、表详情、`/tasks` 各读**不同形状**的端点，
一份通用 `{items, total}` 桩只能渲染四个页面里的三个 ⇒ 半实现＝对着 mock 断言。所以本轮第一手
材料是「页面到底请求了什么」和「那些端点到底答了什么」：

| 档 | 量的问题 | 关键读数 |
| --- | --- | --- |
| `frontend-request-census.txt` | 四个页面 + 表详情 + /settings 在浏览器里真的发出哪些请求（playwright 探针，marker 只记录不判定） | **12 条端点 / 15 次请求**；`/tasks` 页面在挂载时就请求 `/scripts/`（新建对话框的接口下拉），`/tables/3201/data` 只在点开「预览数据」标签时才发 |
| `endpoint-payload-shapes.txt` | 每条被桩的端点，其后端 `data` 的键集合与信封形状 | 74 条路由／13 条夹具；`APIResponse` 与夹具**逐条同形**；14 条裸对象处理器，其中只有 `/tables/{id}/schema` 在本面被桩，且按裸形状桩 |
| `contract-and-fiction-measurements.txt` | 旧 skip 里的形状与「页面有没有行点击处理器」这类假设是不是 fiction | `TablesView` 没有行点击处理器，`button:has-text("执行")` 子串命中的是「执行记录」（只做跳转）——两条旧用例的前提本身是假的 |

`payload_shape_census.py` 是**可复算**的第三档（不是手抄结论）：静态 AST 读 `opendata/api/**` ＋
`include_router` 前缀 ⇒ 挂起后的绝对路径，再读 `fixtures.ts` 的 13 条键，逐条配对。它跑在
`make gate` 之外（判定面唯一属 C27：浏览器判决只有 `frontend_e2e_plane.py` 一处，这脚本只测量）。

---

## 2. 夹具面 `frontend/e2e/fixtures.ts`

三条设计决定，都被 §1 的读数钉住：

1. **`envelope()` 显式套**：拦截器 `return res.data ?? res` 会剥一层，所以「忘了套信封」的用例
   照样绿——只是不证明任何契约。套与不套写成代码里的形状，裸对象（schema）就按裸的桩。
2. **`stubApi` 只答给过它的**：一条 catch-all 路由，没桩过的请求记下来并按 `success:false` 答，
   每条用例末尾 `expect(plane.unstubbed()).toEqual([])`。页面一旦开始问一条夹具没建模的端点，
   用例红，而不是静默渲染空白。这条守卫在 §5 的 F 植入里被证明咬得住。
3. **`postBodies()` 读请求体**：新建任务那条用例断言的是**提交出去的 body**
   （`schedule_type: 'cron'` ＋ `schedule_expression: '0 * * * *'`），并额外拿后端那条
   `^(once|daily|weekly|monthly|cron|interval)$`（`api/schemas.py:229`）在测试里复算一遍。
   断言 toast 文案只能证明 toast 出现过；断 body 才证明这个按钮真的能创建东西。

`seedSession()` 从 `auth.spec.ts` 提到夹具里（七条缺口都要登录态），注释保留 C30 的口径：
它是「登录成功后留下的会话形状」这个**输入**，没有任何用例声称页面的数据是从它渲染出来的。

## 3. 七条缺口逐条退掉，四条产品缺陷随退随现

| C30 缺口 | 退成 | 退出来才看得见的事 |
| --- | --- | --- |
| `scripts list shows data` | `/scripts`：列表行 + 「共 2 个接口」 + 分类 chip | 分类端点的 `data` 是**裸 list[str]**，不是对象 |
| `tables list shows data` | `/tables`：注册表行 + 数仓 ods/dwd 两层 | dwd 行的 `source` 是 `null` ⇒ 渲染成 `—`，与空单元格不同 |
| `table detail shows schema and preview` | 点「查看详情」→ `/tables/3201` → schema 列 + 点开预览标签后的行 | 旧体假设「点行会跳转」＝ fiction（`TablesView` 无行点击处理器）；schema 端点答**裸对象** |
| `executions list shows history` | `/executions`：统计卡 + 失败分片面板 + 表格 + `85.7%` + `成功`/`失败` + `11000 → 12345` | **三条真机缺陷**：①`success_rate` 后端已乘 100（`execution_service.py:259`），前端再乘一次 ⇒ 页面读 **8570.0%**；②状态词表里写的是 `success`，后端写的是 `completed` ⇒ 中文标签「成功」永远不可达；③列读 `rows_processed`，这个键只在下载进度载荷上有，执行记录是 `rows_before`/`rows_after`（`models/task.py:272-273`）⇒ 永远 `-` |
| `/data` 目录页（C30 未列为缺口，本轮按 `REQUIRED_PAGES` 补进来） | `/data`：域行 + `已验证`/`未验证` + `滞后 1 天` | 新鲜度文案要能区分「验证过但滞后」与「没验证」 |
| `tasks list page loads` | `/tasks`：任务行 + cron 表达式 + `plane.served()` 含 `GET /api/v1/scripts/` | 下拉数据在挂载时就取，属页面契约的一部分 |
| `can create a new task` | 填名字 → 选接口 → 选「每小时」→ 提交 → 断 body | **第四条缺陷**：`每小时` 的 `value` 是 `'hourly'`，后端正则不收 ⇒ 这个按钮在此之前**点了必然 422**，永远创建不出东西 |
| `can trigger task execution` | 改名成 `the 执行记录 action opens that task's executions`，断 `toHaveURL('/executions?task_id=71')` | 「触发执行」这个按钮前端没有（`TasksView.vue:220-243` 只有 编辑／执行记录／删除），保留旧名就是保留 fiction |

两条 `if (await …isVisible())` 守卫断言（表详情、触发执行）随上面的改写搬出了 `if`——这正是
C30 §6 结尾登记的那两条形同「查到了才断言」的形状，退路时不许带回来。

尾巴上一条：给 `ExecutionsView.getStatusInfo` 加了 `?? { text: status, type: 'info' }`。
`Record<TaskStatusType, …>` 让编译期穷举后端 6 个状态词（这是②的修法），但词是从 HTTP 来的，
类型只是断言不是保证；后端哪天加一个词，页面该显示那个词，而不是在 `.text` 上崩掉整张表。

## 4. 缺口退空之后，判定面换了一条能红的规则

`GAPS` 退成空集后，`GAP-DRIFT` 变成一条关于空表的规则：它仍然双向（新增 `skip` 必须登记、
登记的缺口不再 `skip` 也红），但**删掉一条用例**它看不见——而删掉用例正是这个面此后最可能的
腐烂方式。所以本轮加的是页级下限，不是把判定面关掉：

```
REQUIRED_PAGES = {/scripts, /tables, /executions, /tasks, /data}
covered_pages() = 只在 Authenticated describe 下、只在 verdict == ASSERTS 的叶子上，
                  统计它 page.goto() 过的路径
judge()         = REQUIRED_PAGES - covered_pages() 非空 ⇒ PAGE-COVERAGE finding ⇒ gate 红
```

三条刻意的收窄，都是为了不让规则自己变恒真：匿名（登录跳转）叶子也 `goto('/tables')`，但它断的是
守卫不是页面 ⇒ 只有 `Authenticated` 下的算数；GUARDED 叶子按定义可能什么都不断 ⇒ 不算覆盖；
`ASSERTS` 之外的四种判决（VACUOUS/NO-ASSERTION/GUARDED/PARTLY-GUARDED）都另有 finding 路径。

## 5. 证明它能红

| 面 | 反例跑法 | 结果 |
| --- | --- | --- |
| e2e 用例本身 | `falsification.txt`：六条植入（把 D1/D2/D3/D4 四条缺陷装回去、给裸对象套上信封、让夹具漏掉一个端点） | **五红一不红**，驱动 `DRIVER_EXIT=1`；E（裸对象套信封）**没被打红**——`res.data ?? res` 对两种形状都 tolerate，浏览器层无可观测差异。本轮因此**不声称** e2e 面能证伪裸/信封之别，这条限制记在 §7 与夹具头注释里；驱动没有删掉 E 再跑一遍凑 rc=0 |
| 判定面规则 | `tests/test_frontend_e2e_guard.py` 23 例（合成 spec 植入 + 真树对账） | 其中 5 例是本轮新增：`PAGE-COVERAGE` 正向、匿名叶子不算覆盖、GUARDED 叶子不算覆盖、真树覆盖五条必需页、**删掉全部 `/data` 叶子必须红且其余叶子仍为 `ASSERTS`** |
| 形状对账 | `endpoint-payload-shapes.txt` 末尾的三条植入（去掉一个 `envelope(`、把 `categories` 拼成 `categoires`、把夹具指向一条不存在的深层路径） | 三条计数器各红一次，`rc=1/1/1`。第二条是本轮**发现自己判据恒真**的地方：段匹配会把拼写错的键吸进 `/scripts/{script_id}`，「stubs with no route」在那种输入下永远是 0 ⇒ 新增 `STUB-TYPO` 规则（同位置存在只差 1~2 字符的真实字面路由即红），未放宽任何既有判据 |

## 6. 门禁读数（逐项，取自 `gate.txt` 内部）

| 门禁项 | 读数 | 与 C31 比 |
| --- | --- | --- |
| test-cov | **2,821 passed / 6 skipped**，覆盖率 **86.75%**（TOTAL 10,942） | +5 例（恰为守卫用例 18→23）；**不声称覆盖率改善**——新增判定住在 `tests/`，不在 `--cov` 分母内（与 C29 §9-3、C30、C31 同口径）；6 skip 与 C27–C31 逐字同批（`tests/test_port_fidelity.py` em 通道，em 仍拒） |
| a2-check | **A2 files: 293**（+1 = `payload_shape_census.py`），`ok ruff check`／`ok ruff format --check`／`ok mypy`／`ok bandit` | 本轮新增脚本一开始就是 A2 标准，无「先红后改」过程 |
| public-api | 487/487，docstring 与 annotation 各 **100.0%** | +1（判定面新增的 `covered_pages()`） |
| quality-ratchet | `ruff_selfdev` 274（快照 277）／`mypy_selfdev` 21／`bandit_selfdev` 4／`ruff_ported` 2144（2145）／`direct_http_ported` 1044（1045），三条 improved 未 `--update` | 持平（只降不升） |
| frontend-lint | `npx eslint .` **0 errors／33 warnings** | 未新增告警、未加 `eslint-disable`。**同步更正**：v5.2 那一行写的「46 warnings」是从 C30 档里抄来的旧数，`docs/evidence/C31/gate.txt:6345` 的读数是 `✖ 33 problems (0 errors, 33 warnings)`——C31 修 `void`/浮动 promise 时已把 46 降到 33。本轮读数为 33，与 C31 档位逐字相等 |
| frontend-typecheck | `vue-tsc -b --force` rc=0，跑后无 `*.tsbuildinfo` 残留 | 持平（C31 修好的项目制） |
| frontend-collection | 14/14 文件 | 持平 |
| test（python 侧收集守卫等） | 见 test-cov | — |
| frontend-test | **14 文件 / 106 例全绿** | 持平（本轮没有新增前端单测：新增的判决住在 e2e 与 `tests/`） |
| frontend-e2e | 判定面 `findings: 0` + `MATCH` ⇒ 真跑 **`18 passed (6.6s)`** | `10 passed / 7 skipped` ⇒ **18 passed**，跳过数归零 |

## 7. 登记不改的债与本轮的边界

* **信封层数不在 e2e 判定面上**（§5 的 E）：拦截器 `res.data ?? res` 对套与不套都 tolerate，
  任何只读页面渲染结果的面都分不出信封套了几层；能分出的只有类型面与源码量档。统一对外契约
  （去掉这个双解包形状）属接口决策，与 C31 登记的同一件事合流。
* **13 条裸对象端点在本面之外**：`/settings/database{,/warehouse}`、`/keys` ×3、`/users` ×3、
  `/data/{capabilities,sources,download,download/{id}/status}` —— 全库 14 条裸，本面只覆盖 1 条。
  它们今天既没被 e2e 页面请求，也没有别的判定面读得到它们的形状。补页面是产品决策，本轮只做
  了「量全」（`endpoint-payload-shapes.txt` 逐条列了）。
* `GET /api/v1/tables/3201/data` 的 `data.rows` 元素键是**数据库列名**，静态读不出来，census
  显式打印「this census cannot read」而不是编一份键表——这是本面唯一一处没读出的形状。
* census 是静态对账：它说形状一致，不说值正确；本轮它一次也没启动过后端、连过 MySQL、开过浏览器。
* `if` 守卫这一判决仍有固有代价：判定面靠 `if` 块的括号深度数守卫，C30 已登记「宁可多报也不漏报」
  的取向（`else if` 链不算覆盖），本轮没改这个取向。
* 401 分支整页重载销毁 toast（「REST 报错在页面上看得见」当前不可观测）继续在 §7 清单里，
  没有被写成一条会闪的断言。
* **本轮唯一动过的生产代码**是 `frontend/src/views/` 两个 `.vue`；`opendata/` 与数仓
  在这条 diff 里为空（AC-15 旧表仍**只读**，未执行任何 DROP），全程未读取或打印任何密钥值。

## 8. 档案清单

| 档 | 作用 |
| --- | --- |
| `gate.txt` | 与提交树一致的完整门禁日志（provenance 头写在运行前；本轮前两次绿跑的说明在头部） |
| `frontend-request-census.txt` | 页面真实请求集（12 端点／15 请求），探针跑完即删 |
| `endpoint-payload-shapes.txt` | 形状对账完整输出 + 三条植入反例 + findings/限制段 |
| `payload_shape_census.py` | 上面那份读数的可复算脚本（A2 标准，`make gate` 不收它，收它的是 a2-check） |
| `contract-and-fiction-measurements.txt` | 旧用例假设的逐条真伪（行点击处理器、「执行」按钮、状态词表…） |
| `falsification.txt` | 六条植入的完整 playwright 输出（五红一不红）＋ 运行后记（驱动器是 scratch，跑完即删） |
